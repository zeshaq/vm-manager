"""
Kubernetes deployment via kubeadm on KVM VMs.

Flow:
  1.  Generate SSH keypair for the cluster
  2.  Create a dedicated libvirt NAT network  (10.0.<idx>.0/24)
  3.  Create N VMs from an Ubuntu 22.04 cloud image + cloud-init
  4.  Wait for SSH on every node
  5.  Install containerd + kubeadm/kubelet/kubectl on every node
  6.  kubeadm init on the control plane
  7.  Install chosen CNI
  8.  kubeadm join on each worker
  9.  Fetch kubeconfig

Prerequisites on the host:
  - Ubuntu 22.04 cloud image at /var/lib/libvirt/images/ubuntu-22.04-cloudimg.img
    (wget -O /var/lib/libvirt/images/ubuntu-22.04-cloudimg.img \\
       https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img)
  - cloud-image-utils: sudo apt install cloud-image-utils
  - libvirt / qemu-kvm already running (they are, since this is a VM manager)
"""
import os, json, time, threading, subprocess, shutil, random, traceback
from pathlib import Path
from datetime import datetime
from flask import Blueprint, jsonify, request, session, Response, stream_with_context

try:
    import libvirt
    _LIBVIRT = True
except ImportError:
    _LIBVIRT = False

k8s_bp = Blueprint('kubernetes', __name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
APP_DIR      = Path(__file__).parent.parent
CLUSTERS_DIR = APP_DIR / 'k8s_clusters'
CLUSTERS_DIR.mkdir(exist_ok=True)

STORAGE_PATH = Path('/var/lib/libvirt/images')
BASE_IMAGE   = STORAGE_PATH / 'ubuntu-22.04-cloudimg.img'

# ── Constants ─────────────────────────────────────────────────────────────────
K8S_VERSIONS = ['1.30', '1.29', '1.28']

CNI_OPTIONS = {
    'flannel': {'name': 'Flannel',  'pod_cidr': '10.244.0.0/16'},
    'calico':  {'name': 'Calico',   'pod_cidr': '192.168.0.0/16'},
}

NODE_SIZES = {
    'small':  {'label': 'Small  (2 vCPU / 2 GB)',  'cpu': 2, 'ram_mb': 2048,  'disk_gb': 20},
    'medium': {'label': 'Medium (2 vCPU / 4 GB)',  'cpu': 2, 'ram_mb': 4096,  'disk_gb': 30},
    'large':  {'label': 'Large  (4 vCPU / 8 GB)',  'cpu': 4, 'ram_mb': 8192,  'disk_gb': 40},
}

# In-memory job state: { job_id: {logs, status, cluster_id} }
_JOBS: dict = {}
_JOBS_LOCK = threading.Lock()


# ── Auth ──────────────────────────────────────────────────────────────────────
def _auth():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    return None


# ── Cluster JSON I/O ──────────────────────────────────────────────────────────
def _save_cluster(data):
    path = CLUSTERS_DIR / f"{data['id']}.json"
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)


def _load_cluster(cid):
    path = CLUSTERS_DIR / f'{cid}.json'
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def _list_clusters():
    result = []
    for p in CLUSTERS_DIR.glob('*.json'):
        try:
            with open(p) as f:
                result.append(json.load(f))
        except Exception:
            pass
    return sorted(result, key=lambda c: c.get('created_at', 0), reverse=True)


def _next_subnet_index():
    used = {c.get('subnet_index', 0)
            for c in _list_clusters()
            if c.get('status') not in ('deleted',)}
    for i in range(1, 200):
        if i not in used:
            return i
    raise RuntimeError('No available subnets')


# ── SSH helpers ───────────────────────────────────────────────────────────────
_SSH_OPTS = [
    '-o', 'StrictHostKeyChecking=no',
    '-o', 'UserKnownHostsFile=/dev/null',
    '-o', 'BatchMode=yes',
    '-o', 'ConnectTimeout=10',
    '-o', 'ServerAliveInterval=15',
]

def _ssh(host, key, cmd, timeout=300):
    r = subprocess.run(
        ['ssh'] + _SSH_OPTS + ['-i', key, f'ubuntu@{host}', cmd],
        capture_output=True, text=True, timeout=timeout,
    )
    return r.stdout, r.stderr, r.returncode


def _ssh_script(host, key, script, timeout=600):
    """Pipe a shell script to `sudo bash` on the remote host."""
    r = subprocess.run(
        ['ssh'] + _SSH_OPTS + ['-i', key, f'ubuntu@{host}', 'sudo bash -s'],
        input=script.encode(), capture_output=True, timeout=timeout,
    )
    return (r.stdout.decode(errors='replace'),
            r.stderr.decode(errors='replace'),
            r.returncode)


def _wait_ssh(host, key, timeout=360, log=None):
    deadline = time.time() + timeout
    if log:
        log(f'  Waiting for SSH on {host} (up to {timeout}s)...')
    while time.time() < deadline:
        try:
            _, _, rc = _ssh(host, key, 'true', timeout=15)
            if rc == 0:
                if log:
                    log(f'  SSH ready on {host}')
                return True
        except Exception:
            pass
        time.sleep(8)
    return False


# ── Cloud-init ISO ────────────────────────────────────────────────────────────
def _make_cidata_iso(out_path, user_data, meta_data, network_config):
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        for name, content in [('user-data', user_data),
                               ('meta-data', meta_data),
                               ('network-config', network_config)]:
            with open(f'{d}/{name}', 'w') as f:
                f.write(content)

        if shutil.which('cloud-localds'):
            subprocess.run(
                ['cloud-localds', '--network-config', f'{d}/network-config',
                 out_path, f'{d}/user-data', f'{d}/meta-data'],
                check=True, capture_output=True,
            )
        elif shutil.which('genisoimage'):
            subprocess.run(
                ['genisoimage', '-output', out_path, '-V', 'cidata',
                 '-r', '-J', '-input-charset', 'utf-8',
                 f'{d}/user-data', f'{d}/meta-data', f'{d}/network-config'],
                check=True, capture_output=True, cwd=d,
            )
        elif shutil.which('mkisofs'):
            subprocess.run(
                ['mkisofs', '-output', out_path, '-V', 'cidata', '-r', '-J',
                 f'{d}/user-data', f'{d}/meta-data', f'{d}/network-config'],
                check=True, capture_output=True, cwd=d,
            )
        else:
            raise RuntimeError(
                'No ISO tool found. Run: sudo apt install cloud-image-utils')


# ── Libvirt helpers ───────────────────────────────────────────────────────────
def _random_mac():
    return 'fa:16:3e:{:02x}:{:02x}:{:02x}'.format(
        random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))


def _create_libvirt_network(net_name, subnet_idx):
    gw   = f'10.0.{subnet_idx}.1'
    mask = '255.255.255.0'
    xml  = f"""
<network>
  <name>{net_name}</name>
  <forward mode='nat'>
    <nat><port start='1024' end='65535'/></nat>
  </forward>
  <bridge stp='on' delay='0'/>
  <ip address='{gw}' netmask='{mask}'>
    <dhcp>
      <range start='10.0.{subnet_idx}.2' end='10.0.{subnet_idx}.50'/>
    </dhcp>
  </ip>
</network>"""
    conn = libvirt.open('qemu:///system')
    try:
        net = conn.networkDefineXML(xml)
        net.setAutostart(True)
        net.create()
    finally:
        conn.close()


def _create_vm(name, disk, cidata, mac, net_name, cpu, ram_mb):
    xml = f"""
<domain type='kvm'>
  <name>{name}</name>
  <memory unit='MiB'>{ram_mb}</memory>
  <vcpu placement='static'>{cpu}</vcpu>
  <os>
    <type arch='x86_64' machine='pc-q35-6.2'>hvm</type>
    <boot dev='hd'/>
  </os>
  <features><acpi/><apic/></features>
  <cpu mode='host-passthrough' check='none'/>
  <clock offset='utc'/>
  <on_poweroff>destroy</on_poweroff>
  <on_reboot>restart</on_reboot>
  <devices>
    <emulator>/usr/bin/qemu-system-x86_64</emulator>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2' discard='unmap'/>
      <source file='{disk}'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <disk type='file' device='cdrom'>
      <driver name='qemu' type='raw'/>
      <source file='{cidata}'/>
      <target dev='sda' bus='sata'/>
      <readonly/>
    </disk>
    <interface type='network'>
      <mac address='{mac}'/>
      <source network='{net_name}'/>
      <model type='virtio'/>
    </interface>
    <serial type='pty'><target port='0'/></serial>
    <console type='pty'><target type='serial' port='0'/></console>
    <graphics type='vnc' port='-1' autoport='yes' listen='127.0.0.1'>
      <listen type='address' address='127.0.0.1'/>
    </graphics>
    <rng model='virtio'>
      <backend model='random'>/dev/urandom</backend>
    </rng>
  </devices>
</domain>"""
    conn = libvirt.open('qemu:///system')
    try:
        dom = conn.defineXML(xml)
        dom.create()
        return dom.UUIDString()
    finally:
        conn.close()


def _destroy_cluster_vms(cluster):
    if not _LIBVIRT:
        return
    conn = libvirt.open('qemu:///system')
    try:
        # Destroy and undefine VMs
        all_nodes = []
        if cluster.get('nodes', {}).get('control'):
            all_nodes.append(cluster['nodes']['control'])
        all_nodes.extend(cluster.get('nodes', {}).get('workers', []))

        for node in all_nodes:
            try:
                dom = conn.lookupByUUIDString(node['vm_uuid'])
                try:
                    dom.destroy()
                except libvirt.libvirtError:
                    pass
                dom.undefineFlags(
                    libvirt.VIR_DOMAIN_UNDEFINE_MANAGED_SAVE |
                    libvirt.VIR_DOMAIN_UNDEFINE_NVRAM
                )
            except libvirt.libvirtError:
                pass
            # Delete disk files
            for f in [
                str(STORAGE_PATH / f"{node['name']}.qcow2"),
                str(STORAGE_PATH / f"{node['name']}-cidata.iso"),
            ]:
                try:
                    os.remove(f)
                except FileNotFoundError:
                    pass

        # Remove libvirt network
        net_name = cluster.get('network')
        if net_name:
            try:
                net = conn.networkLookupByName(net_name)
                try:
                    net.destroy()
                except libvirt.libvirtError:
                    pass
                net.undefine()
            except libvirt.libvirtError:
                pass
    finally:
        conn.close()

    # Remove SSH key dir
    key_dir = CLUSTERS_DIR / cluster['id']
    if key_dir.exists():
        shutil.rmtree(key_dir)


# ── Installation scripts ──────────────────────────────────────────────────────
def _prereq_script(k8s_ver):
    return f"""#!/bin/bash
set -e

echo '[k8s] Disabling swap'
swapoff -a
sed -i '/[ \\t]swap[ \\t]/s/^/#/' /etc/fstab

echo '[k8s] Loading kernel modules'
cat > /etc/modules-load.d/k8s.conf <<'EOF'
overlay
br_netfilter
EOF
modprobe overlay
modprobe br_netfilter

echo '[k8s] Applying sysctl settings'
cat > /etc/sysctl.d/k8s.conf <<'EOF'
net.bridge.bridge-nf-call-iptables  = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward                 = 1
EOF
sysctl --system > /dev/null

echo '[k8s] Installing containerd'
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -q containerd
mkdir -p /etc/containerd
containerd config default > /etc/containerd/config.toml
sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml
systemctl restart containerd
systemctl enable containerd

echo '[k8s] Installing kubeadm / kubelet / kubectl v{k8s_ver}'
apt-get install -y -q apt-transport-https ca-certificates curl gpg
mkdir -p /etc/apt/keyrings
curl -fsSL "https://pkgs.k8s.io/core:/stable:/v{k8s_ver}/deb/Release.key" \\
  | gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
echo "deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] \\
  https://pkgs.k8s.io/core:/stable:/v{k8s_ver}/deb/ /" \\
  > /etc/apt/sources.list.d/kubernetes.list
apt-get update -qq
apt-get install -y -q kubelet kubeadm kubectl
apt-mark hold kubelet kubeadm kubectl
systemctl enable kubelet
echo '[k8s] Prerequisites complete'
"""


def _cni_commands(cni, pod_cidr):
    if cni == 'flannel':
        return (
            'KUBECONFIG=/etc/kubernetes/admin.conf '
            'kubectl apply -f '
            'https://github.com/flannel-io/flannel/releases/download/v0.25.2/kube-flannel.yml'
        )
    elif cni == 'calico':
        return f"""bash -s <<'CALICO'
curl -fsSL https://raw.githubusercontent.com/projectcalico/calico/v3.28.0/manifests/calico.yaml \\
  -o /tmp/calico.yaml
sed -i 's|# - name: CALICO_IPV4POOL_CIDR|- name: CALICO_IPV4POOL_CIDR|' /tmp/calico.yaml
sed -i 's|#   value: "192.168.0.0/16"|  value: "{pod_cidr}"|' /tmp/calico.yaml
KUBECONFIG=/etc/kubernetes/admin.conf kubectl apply -f /tmp/calico.yaml
CALICO"""
    return 'echo no CNI'


# ── Background deployment ─────────────────────────────────────────────────────
def _deploy(job_id, cluster_id, cfg):
    job     = _JOBS[job_id]
    cluster = _load_cluster(cluster_id)

    def log(msg):
        ts   = datetime.now().strftime('%H:%M:%S')
        line = f'[{ts}] {msg}'
        with _JOBS_LOCK:
            job['logs'].append(line)
        print(line, flush=True)

    def fail(msg):
        log(f'✗ ERROR: {msg}')
        with _JOBS_LOCK:
            job['status'] = 'error'
        cluster['status'] = 'failed'
        cluster['error']  = msg
        _save_cluster(cluster)

    try:
        sid       = cluster['subnet_index']
        net_name  = f"k8s-{cluster_id[:8]}"
        k8s_ver   = cfg['k8s_version']
        cni       = cfg['cni']
        w_count   = cfg['worker_count']
        size      = NODE_SIZES[cfg['node_size']]
        cpu, ram  = size['cpu'], size['ram_mb']
        disk_gb        = size['disk_gb']
        pod_cidr       = CNI_OPTIONS[cni]['pod_cidr']
        base_img_path  = cfg.get('base_image_path', str(BASE_IMAGE))
        gw        = f'10.0.{sid}.1'
        ctrl_ip   = f'10.0.{sid}.10'
        w_ips     = [f'10.0.{sid}.{11 + i}' for i in range(w_count)]

        # ── 1. SSH keypair ────────────────────────────────────────────────────
        log('━━━ Step 1/7: Generating SSH keypair')
        key_dir  = CLUSTERS_DIR / cluster_id
        key_dir.mkdir(exist_ok=True)
        key_file = str(key_dir / 'id_rsa')
        subprocess.run(
            ['ssh-keygen', '-t', 'rsa', '-b', '2048', '-N', '', '-f', key_file],
            check=True, capture_output=True,
        )
        os.chmod(key_file, 0o600)
        with open(f'{key_file}.pub') as f:
            pub_key = f.read().strip()
        log('  Keypair generated')

        # ── 2. Libvirt network ────────────────────────────────────────────────
        log(f'━━━ Step 2/7: Creating network {net_name} (10.0.{sid}.0/24)')
        _create_libvirt_network(net_name, sid)
        cluster['network'] = net_name
        _save_cluster(cluster)
        log(f'  Network {net_name} active, gateway {gw}')

        # ── 3. Create VMs ─────────────────────────────────────────────────────
        total_nodes = 1 + w_count
        log(f'━━━ Step 3/7: Creating {total_nodes} VM(s) — {cpu} vCPU / {ram}MB RAM / {disk_gb}GB disk each')

        all_nodes = [('control', ctrl_ip)] + [(f'worker{i+1}', w_ips[i]) for i in range(w_count)]

        for role, ip in all_nodes:
            node_name = f'k8s-{cluster_id[:8]}-{role}'
            mac       = _random_mac()
            disk_path = str(STORAGE_PATH / f'{node_name}.qcow2')
            iso_path  = str(STORAGE_PATH / f'{node_name}-cidata.iso')

            log(f'  [{role}] Creating disk ({disk_gb}G thin from {Path(base_img_path).name})')
            subprocess.run(
                ['qemu-img', 'create', '-f', 'qcow2', '-F', 'qcow2',
                 '-b', base_img_path, disk_path, f'{disk_gb}G'],
                check=True, capture_output=True,
            )

            user_data = f"""#cloud-config
hostname: {node_name}
users:
  - name: ubuntu
    ssh_authorized_keys:
      - {pub_key}
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    lock_passwd: false
package_update: true
packages:
  - qemu-guest-agent
runcmd:
  - systemctl enable --now qemu-guest-agent
"""
            meta_data = f"instance-id: {node_name}\nlocal-hostname: {node_name}\n"
            network_config = f"""version: 2
ethernets:
  enp1s0:
    match:
      name: "enp*"
    set-name: enp1s0
    addresses: [{ip}/24]
    gateway4: {gw}
    nameservers:
      addresses: [8.8.8.8, 1.1.1.1]
"""
            log(f'  [{role}] Creating cloud-init ISO')
            _make_cidata_iso(iso_path, user_data, meta_data, network_config)

            log(f'  [{role}] Defining and starting VM ({ip})')
            uuid = _create_vm(node_name, disk_path, iso_path, mac, net_name, cpu, ram)

            node_info = {'name': node_name, 'ip': ip, 'vm_uuid': uuid, 'role': role}
            if role == 'control':
                cluster['nodes']['control'] = node_info
            else:
                cluster['nodes']['workers'].append(node_info)
            _save_cluster(cluster)
            log(f'  [{role}] VM started — uuid={uuid[:8]}…')

        # ── 4. Wait for SSH ───────────────────────────────────────────────────
        log('━━━ Step 4/7: Waiting for VMs to come online (boot + cloud-init ~3 min)')
        for role, ip in all_nodes:
            if not _wait_ssh(ip, key_file, timeout=420, log=log):
                fail(f'SSH timeout on {ip} ({role}) — VM may not have booted')
                return

        # ── 5. Install prerequisites on all nodes ─────────────────────────────
        log(f'━━━ Step 5/7: Installing prerequisites on {total_nodes} node(s)')
        script = _prereq_script(k8s_ver)
        for role, ip in all_nodes:
            log(f'  [{role}] Installing containerd + kubeadm (this takes ~3 min)')
            out, err, rc = _ssh_script(ip, key_file, script, timeout=900)
            if rc != 0:
                log(f'  stdout tail:\n{out[-1500:]}')
                log(f'  stderr tail:\n{err[-500:]}')
                fail(f'Prerequisite install failed on {ip} ({role})')
                return
            log(f'  [{role}] ✓ Done')

        # ── 6. Init control plane ─────────────────────────────────────────────
        log(f'━━━ Step 6/7: Initialising control plane on {ctrl_ip}')
        init_cmd = (
            f'sudo kubeadm init '
            f'--pod-network-cidr={pod_cidr} '
            f'--apiserver-advertise-address={ctrl_ip} '
            f'--kubernetes-version=v{k8s_ver} 2>&1'
        )
        out, err, rc = _ssh(ctrl_ip, key_file, init_cmd, timeout=600)
        if rc != 0:
            log(f'kubeadm init output:\n{out[-3000:]}')
            fail('kubeadm init failed')
            return
        log('  ✓ Control plane initialised')

        # Setup kubeconfig on control node
        _ssh(ctrl_ip, key_file,
             'sudo mkdir -p /home/ubuntu/.kube && '
             'sudo cp /etc/kubernetes/admin.conf /home/ubuntu/.kube/config && '
             'sudo chown ubuntu:ubuntu /home/ubuntu/.kube/config',
             timeout=30)

        # Install CNI
        log(f'  Installing CNI: {cni}')
        cni_cmd = f'sudo bash -c \'{_cni_commands(cni, pod_cidr)}\''
        out, err, rc = _ssh(ctrl_ip, key_file, cni_cmd, timeout=180)
        if rc != 0:
            log(f'  CNI output: {out[-1000:]}')
            log(f'  Warning: CNI may need a moment to stabilise (rc={rc})')
        else:
            log(f'  ✓ CNI {cni} installed')

        # Get join command
        out, err, rc = _ssh(ctrl_ip, key_file,
                             'sudo kubeadm token create --print-join-command 2>/dev/null',
                             timeout=30)
        if rc != 0:
            fail('Failed to obtain join command')
            return
        join_cmd = out.strip()

        # ── 7. Join workers ───────────────────────────────────────────────────
        if w_count == 0:
            log('━━━ Step 7/7: Single-node cluster — untainting control plane')
            _ssh(ctrl_ip, key_file,
                 'sudo KUBECONFIG=/etc/kubernetes/admin.conf '
                 'kubectl taint nodes --all node-role.kubernetes.io/control-plane- 2>&1',
                 timeout=30)
        else:
            log(f'━━━ Step 7/7: Joining {w_count} worker(s)')
            for role, ip in all_nodes[1:]:
                log(f'  [{role}] Joining cluster')
                out, err, rc = _ssh(ip, key_file, f'sudo {join_cmd} 2>&1', timeout=300)
                if rc != 0:
                    log(f'  join output: {out[-2000:]}')
                    fail(f'Worker join failed on {ip} ({role})')
                    return
                log(f'  [{role}] ✓ Joined')

        # ── Fetch kubeconfig ──────────────────────────────────────────────────
        log('  Fetching kubeconfig')
        out, _, rc = _ssh(ctrl_ip, key_file, 'sudo cat /etc/kubernetes/admin.conf', timeout=30)
        if rc == 0 and out.strip():
            cluster['kubeconfig'] = out

        # ── Done ──────────────────────────────────────────────────────────────
        cluster['status']           = 'running'
        cluster['control_plane_ip'] = ctrl_ip
        cluster['deployed_at']      = int(time.time())
        _save_cluster(cluster)

        with _JOBS_LOCK:
            job['status'] = 'done'

        log('')
        log('╔══════════════════════════════════════════════════╗')
        log('║   Kubernetes cluster deployed successfully! 🎉   ║')
        log('╚══════════════════════════════════════════════════╝')
        log(f'  Control plane : {ctrl_ip}')
        if w_ips:
            log(f'  Workers       : {", ".join(w_ips)}')
        log(f'  Version       : v{k8s_ver}')
        log(f'  CNI           : {cni}  ({pod_cidr})')
        log('')
        log('  Download kubeconfig from the cluster detail page,')
        log(f'  or SSH in:  ssh -i <key> ubuntu@{ctrl_ip}')

    except Exception as exc:
        fail(f'{exc}\n{traceback.format_exc()}')


# ── API endpoints ─────────────────────────────────────────────────────────────

@k8s_bp.route('/api/k8s/prereqs')
def prereqs():
    err = _auth()
    if err:
        return err

    checks = {
        'base_image':    BASE_IMAGE.exists(),
        'cloud_localds': bool(shutil.which('cloud-localds')),
        'genisoimage':   bool(shutil.which('genisoimage')),
        'mkisofs':       bool(shutil.which('mkisofs')),
        'qemu_img':      bool(shutil.which('qemu-img')),
        'libvirt':       _LIBVIRT,
        'ssh':           bool(shutil.which('ssh')),
    }
    checks['iso_tool'] = checks['cloud_localds'] or checks['genisoimage'] or checks['mkisofs']
    checks['ready']    = all([checks['base_image'], checks['iso_tool'],
                               checks['qemu_img'], checks['libvirt'], checks['ssh']])
    checks['base_image_path'] = str(BASE_IMAGE)
    return jsonify(checks)


@k8s_bp.route('/api/k8s/clusters')
def list_clusters():
    err = _auth()
    if err:
        return err
    clusters = _list_clusters()
    # Attach live job status
    for c in clusters:
        if c.get('status') == 'deploying':
            jid = c.get('job_id')
            if jid and jid in _JOBS:
                c['job_progress'] = _JOBS[jid]['status']
    return jsonify({'clusters': clusters})


@k8s_bp.route('/api/k8s/clusters', methods=['POST'])
def create_cluster():
    err = _auth()
    if err:
        return err

    data = request.get_json() or {}
    name       = data.get('name', '').strip()
    k8s_ver    = data.get('k8s_version', '1.29')
    cni        = data.get('cni', 'flannel')
    w_count    = int(data.get('worker_count', 1))
    node_size  = data.get('node_size', 'small')

    if not name:
        return jsonify({'error': 'Cluster name required'}), 400
    if k8s_ver not in K8S_VERSIONS:
        return jsonify({'error': f'Invalid k8s version'}), 400
    if cni not in CNI_OPTIONS:
        return jsonify({'error': 'Invalid CNI'}), 400
    if not (0 <= w_count <= 5):
        return jsonify({'error': 'worker_count must be 0-5'}), 400
    if node_size not in NODE_SIZES:
        return jsonify({'error': 'Invalid node_size'}), 400
    # Base image: accept explicit path from registry, fall back to default
    base_image_path = data.get('base_image_path', '').strip() or str(BASE_IMAGE)
    if not Path(base_image_path).exists():
        return jsonify({'error': f'Base image not found: {base_image_path}'}), 422

    import uuid as _uuid
    cluster_id  = _uuid.uuid4().hex[:12]
    job_id      = _uuid.uuid4().hex[:12]
    subnet_idx  = _next_subnet_index()

    cfg = {
        'k8s_version':    k8s_ver,
        'cni':            cni,
        'worker_count':   w_count,
        'node_size':      node_size,
        'node_cpu':       NODE_SIZES[node_size]['cpu'],
        'node_ram_mb':    NODE_SIZES[node_size]['ram_mb'],
        'pod_cidr':       CNI_OPTIONS[cni]['pod_cidr'],
        'base_image_path': base_image_path,
    }

    cluster = {
        'id':           cluster_id,
        'name':         name,
        'status':       'deploying',
        'job_id':       job_id,
        'subnet_index': subnet_idx,
        'config':       cfg,
        'nodes':        {'control': None, 'workers': []},
        'network':      None,
        'kubeconfig':   None,
        'created_at':   int(time.time()),
    }
    _save_cluster(cluster)

    with _JOBS_LOCK:
        _JOBS[job_id] = {
            'cluster_id': cluster_id,
            'logs':       [],
            'status':     'running',
        }

    t = threading.Thread(target=_deploy, args=(job_id, cluster_id, cfg), daemon=True)
    t.start()

    return jsonify({'cluster_id': cluster_id, 'job_id': job_id}), 202


@k8s_bp.route('/api/k8s/clusters/<cid>')
def get_cluster(cid):
    err = _auth()
    if err:
        return err
    c = _load_cluster(cid)
    if not c:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(c)


@k8s_bp.route('/api/k8s/clusters/<cid>', methods=['DELETE'])
def delete_cluster(cid):
    err = _auth()
    if err:
        return err
    c = _load_cluster(cid)
    if not c:
        return jsonify({'error': 'Not found'}), 404
    if c.get('status') == 'deploying':
        return jsonify({'error': 'Cannot delete while deploying'}), 409

    _destroy_cluster_vms(c)
    c['status'] = 'deleted'
    _save_cluster(c)
    return jsonify({'ok': True})


@k8s_bp.route('/api/k8s/jobs/<job_id>/logs')
def job_logs(job_id):
    """SSE stream: yields log lines as they appear."""
    err = _auth()
    if err:
        return err

    def generate():
        sent = 0
        while True:
            job = _JOBS.get(job_id)
            if not job:
                yield f'data: {json.dumps({"error": "Job not found"})}\n\n'
                return
            logs = job['logs']
            while sent < len(logs):
                yield f'data: {json.dumps({"log": logs[sent]})}\n\n'
                sent += 1
            if job['status'] in ('done', 'error'):
                yield f'data: {json.dumps({"status": job["status"]})}\n\n'
                return
            time.sleep(0.4)

    return Response(
        stream_with_context(generate()),
        content_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@k8s_bp.route('/api/k8s/clusters/<cid>/kubeconfig')
def download_kubeconfig(cid):
    err = _auth()
    if err:
        return err
    c = _load_cluster(cid)
    if not c:
        return jsonify({'error': 'Not found'}), 404
    kc = c.get('kubeconfig')
    if not kc:
        return jsonify({'error': 'Kubeconfig not available yet'}), 404
    from flask import Response as R
    return R(kc, mimetype='application/x-yaml',
             headers={'Content-Disposition': f'attachment; filename={c["name"]}-kubeconfig.yaml'})


@k8s_bp.route('/api/k8s/clusters/<cid>/ssh-key')
def download_ssh_key(cid):
    err = _auth()
    if err:
        return err
    c = _load_cluster(cid)
    if not c:
        return jsonify({'error': 'Not found'}), 404
    key_path = CLUSTERS_DIR / cid / 'id_rsa'
    if not key_path.exists():
        return jsonify({'error': 'SSH key not found'}), 404
    from flask import send_file
    return send_file(str(key_path), as_attachment=True,
                     download_name=f'{c["name"]}-id_rsa')


@k8s_bp.route('/api/k8s/options')
def options():
    err = _auth()
    if err:
        return err
    return jsonify({
        'k8s_versions': K8S_VERSIONS,
        'cni_options':  {k: v['name'] for k, v in CNI_OPTIONS.items()},
        'node_sizes':   {k: v['label'] for k, v in NODE_SIZES.items()},
    })
