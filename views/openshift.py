"""
OpenShift Deployment — Red Hat Assisted Installer integration
─────────────────────────────────────────────────────────────
Supports:
  • Single Node OpenShift (SNO)
  • Multi-node: N control-plane + M workers

Flow:
  1. Validate pull secret, obtain Red Hat SSO access token
  2. POST /v2/clusters  → Assisted Installer creates cluster record
  3. POST /v2/infra-envs → generates discovery ISO
  4. Download minimal ISO (~100 MB) to server
  5. Create KVM VMs via libvirt, boot from ISO
  6. Poll until all expected hosts register
  7. POST /v2/clusters/<id>/actions/install
  8. Poll progress until complete
  9. Retrieve kubeconfig + kubeadmin password
"""

import base64
import hashlib
import json
import os
import subprocess
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

import requests as _req

try:
    import libvirt
    _LIBVIRT = True
except ImportError:
    _LIBVIRT = False

try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False

from flask import Blueprint, jsonify, request, session

ocp_bp = Blueprint('openshift', __name__)

# ── constants ─────────────────────────────────────────────────────────────────

AI_BASE  = 'https://api.openshift.com/api/assisted-install/v2'
SSO_URL  = ('https://sso.redhat.com/auth/realms/redhat-external'
            '/protocol/openid-connect/token')
WORK_DIR  = Path.home() / 'hypercloud' / 'openshift'
_JOBS_FILE = WORK_DIR / 'jobs.json'

# per-job dict: job_id → { status, logs, progress, phase, result, config }
_jobs: dict = {}
_token_cache: dict = {}  # ps_hash → { token, expires_at }
_lock = threading.Lock()


# ── job persistence ───────────────────────────────────────────────────────────

def _load_jobs():
    """Load persisted jobs from disk on startup."""
    global _jobs
    try:
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        if _JOBS_FILE.exists():
            with open(_JOBS_FILE) as f:
                _jobs = json.load(f)
    except Exception:
        _jobs = {}

def _save_jobs():
    """Persist current jobs dict to disk (called under _lock)."""
    try:
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _JOBS_FILE.with_suffix('.tmp')
        with open(tmp, 'w') as f:
            json.dump(_jobs, f)
        tmp.replace(_JOBS_FILE)
    except Exception:
        pass

# Load on import
_load_jobs()


# ── helpers ───────────────────────────────────────────────────────────────────

def _auth():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    return None


def _job_log(job_id: str, msg: str, level: str = 'info'):
    ts = time.strftime('%H:%M:%S')
    with _lock:
        if job_id in _jobs:
            _jobs[job_id]['logs'].append({'ts': ts, 'msg': msg, 'level': level})
            _save_jobs()


def _job_set(job_id: str, **kw):
    with _lock:
        if job_id in _jobs:
            _jobs[job_id].update(kw)
            _save_jobs()


# ── Assisted Installer API ────────────────────────────────────────────────────

def _get_access_token(offline_token: str) -> str:
    """Exchange a Red Hat offline token (from console.redhat.com/openshift/token)
    for a short-lived access token via RH SSO.

    NOTE: This is NOT the same as the pull-secret registry credential.
    The offline token must be obtained separately from:
      https://console.redhat.com/openshift/token
    """
    tok_hash = hashlib.sha256(offline_token.encode()).hexdigest()[:16]
    now = time.time()
    cached = _token_cache.get(tok_hash)
    if cached and cached['expires_at'] > now + 60:
        return cached['token']

    resp = _req.post(
        SSO_URL,
        data={
            'grant_type':    'refresh_token',
            'client_id':     'cloud-services',
            'refresh_token': offline_token.strip(),
        },
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        timeout=20,
    )
    if not resp.ok:
        body = resp.text[:400]
        raise RuntimeError(
            f'SSO returned HTTP {resp.status_code}: {body}'
        )
    data = resp.json()
    token = data['access_token']
    _token_cache[tok_hash] = {'token': token, 'expires_at': now + data.get('expires_in', 900)}
    return token


def _ai(method: str, path: str, token: str, body=None, stream=False, timeout=30):
    """Make a request to the Assisted Installer API."""
    url = f'{AI_BASE}{path}'
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type':  'application/json',
    }
    resp = _req.request(method, url, headers=headers,
                        json=body, stream=stream, timeout=timeout)
    if not resp.ok:
        try:
            detail = resp.json()
            msg = detail.get('message') or detail.get('reason') or str(detail)
        except Exception:
            msg = resp.text[:500]
        raise Exception(f'{resp.status_code} {resp.reason}: {msg}')
    return resp


# ── OCP version list ──────────────────────────────────────────────────────────

# Hardcoded fallback — updated to latest as of 2026-04; regenerated at runtime
_FALLBACK_VERSIONS = ['4.21', '4.20', '4.19', '4.18', '4.17', '4.16', '4.15', '4.14']
_versions_cache: dict = {}   # { versions: [...], fetched_at: float }


def _fetch_versions_from_mirror() -> list[str]:
    """
    Scrape available stable-X.Y channels from the OCP public mirror.
    No auth required. Returns newest-first list like ['4.21', '4.20', ...].
    """
    import re
    resp = _req.get(
        'https://mirror.openshift.com/pub/openshift-v4/clients/ocp/',
        timeout=10,
    )
    resp.raise_for_status()
    channels = re.findall(r'stable-(\d+\.\d+)', resp.text)
    unique = sorted(set(channels), key=lambda v: tuple(int(x) for x in v.split('.')), reverse=True)
    return unique


@ocp_bp.route('/api/openshift/versions')
def ocp_versions():
    err = _auth()
    if err:
        return err

    now = time.time()
    cached = _versions_cache.get('data')
    # Refresh at most once per hour
    if cached and now - _versions_cache.get('fetched_at', 0) < 3600:
        return jsonify({'versions': cached})

    try:
        versions = _fetch_versions_from_mirror()
        if versions:
            _versions_cache['data']       = versions
            _versions_cache['fetched_at'] = now
            return jsonify({'versions': versions})
    except Exception:
        pass

    # Fallback if mirror unreachable
    return jsonify({'versions': _FALLBACK_VERSIONS, 'cached': True})


# ── Pull secret validation ────────────────────────────────────────────────────

@ocp_bp.route('/api/openshift/validate-pull-secret', methods=['POST'])
def validate_pull_secret():
    data = request.get_json(silent=True) or {}
    secret = data.get('pull_secret', '').strip()
    if not secret:
        return jsonify({'valid': False, 'error': 'Empty pull secret'})
    try:
        ps = json.loads(secret)
        auths = ps.get('auths', {})
        required = ['cloud.openshift.com', 'quay.io', 'registry.redhat.io']
        missing = [r for r in required if r not in auths]
        return jsonify({
            'valid':       len(missing) == 0,
            'missing':     missing,
            'registries':  list(auths.keys()),
        })
    except json.JSONDecodeError as e:
        return jsonify({'valid': False, 'error': f'Invalid JSON: {e}'})


# ── libvirt networks ─────────────────────────────────────────────────────────

def _host_bridges():
    """Return host-level Linux bridges not managed by libvirt."""
    import socket, struct, json as _json
    bridges = []
    try:
        # Use 'ip -j addr' to get all interfaces with addresses
        result = subprocess.run(['ip', '-j', 'addr'], capture_output=True, text=True, timeout=5)
        ifaces = _json.loads(result.stdout) if result.returncode == 0 else []
    except Exception:
        ifaces = []

    for iface in ifaces:
        ifname = iface.get('ifname', '')
        # Only include actual bridge devices (check /sys/class/net/<name>/bridge)
        if not os.path.isdir(f'/sys/class/net/{ifname}/bridge'):
            continue
        cidr = ''
        for addr_info in iface.get('addr_info', []):
            if addr_info.get('family') == 'inet':
                local  = addr_info.get('local', '')
                prefix = addr_info.get('prefixlen', '')
                if local and prefix != '':
                    import ipaddress
                    net = ipaddress.ip_interface(f'{local}/{prefix}').network
                    cidr = str(net)
                    break
        bridges.append({
            'name':    ifname,
            'bridge':  ifname,
            'cidr':    cidr,
            'active':  iface.get('operstate', '').upper() != 'DOWN',
            'forward': 'bridge',
            'host_bridge': True,
        })
    return bridges


@ocp_bp.route('/api/openshift/networks')
def list_networks():
    err = _auth()
    if err:
        return err

    nets = []

    # ── libvirt-managed networks ───────────────────────────────────────────────
    if _LIBVIRT:
        try:
            conn = libvirt.open('qemu:///system')
            libvirt_bridges = set()  # track bridge device names to avoid duplicates
            for net in conn.listAllNetworks(0):
                try:
                    xml_str = net.XMLDesc(0)
                    root    = ET.fromstring(xml_str)
                    name    = net.name()
                    active  = net.isActive() == 1

                    # Pull bridge device name
                    bridge_el = root.find('bridge')
                    bridge    = bridge_el.get('name', '') if bridge_el is not None else ''
                    if bridge:
                        libvirt_bridges.add(bridge)

                    # Pull IP / CIDR from <ip address= prefix= or netmask=>
                    cidr = ''
                    ip_el = root.find('ip')
                    if ip_el is not None:
                        addr = ip_el.get('address', '')
                        prefix = ip_el.get('prefix', '')
                        netmask = ip_el.get('netmask', '')
                        if addr:
                            if prefix:
                                cidr = f'{addr}/{prefix}'
                            elif netmask:
                                import socket, struct
                                packed   = socket.inet_aton(netmask)
                                bits     = bin(struct.unpack('!I', packed)[0]).count('1')
                                ip_int   = struct.unpack('!I', socket.inet_aton(addr))[0]
                                mask_int = struct.unpack('!I', packed)[0]
                                net_int  = ip_int & mask_int
                                net_addr = socket.inet_ntoa(struct.pack('!I', net_int))
                                cidr = f'{net_addr}/{bits}'

                    # forward mode (nat / bridge / none)
                    fwd_el   = root.find('forward')
                    fwd_mode = fwd_el.get('mode', 'isolated') if fwd_el is not None else 'isolated'

                    nets.append({
                        'name':    name,
                        'bridge':  bridge,
                        'cidr':    cidr,
                        'active':  active,
                        'forward': fwd_mode,
                    })
                except Exception:
                    pass
            conn.close()

            # ── host bridges (br-real, br0, etc.) not managed by libvirt ──────
            for hb in _host_bridges():
                if hb['bridge'] not in libvirt_bridges:
                    nets.append(hb)

        except Exception as e:
            return jsonify({'networks': nets, 'error': str(e)})
    else:
        # libvirt unavailable — still expose host bridges
        nets = _host_bridges()

    return jsonify({'networks': nets})


# ── Preflight checks ──────────────────────────────────────────────────────────

@ocp_bp.route('/api/openshift/preflight')
def preflight():
    err = _auth()
    if err:
        return err

    checks = {}

    # libvirt
    if _LIBVIRT:
        try:
            conn = libvirt.open('qemu:///system')
            checks['libvirt'] = conn is not None
            if conn:
                conn.close()
        except Exception:
            checks['libvirt'] = False
    else:
        checks['libvirt'] = False

    # disk space
    if _PSUTIL:
        disk = psutil.disk_usage('/')
        checks['disk_free_gb'] = round(disk.free / 1024 ** 3, 1)
        checks['disk_ok'] = disk.free > 50 * 1024 ** 3   # 50 GB minimum
        mem = psutil.virtual_memory()
        checks['ram_free_gb']  = round(mem.available / 1024 ** 3, 1)
        checks['ram_ok']       = mem.available > 16 * 1024 ** 3
    else:
        checks['disk_free_gb'] = None
        checks['disk_ok']      = None
        checks['ram_free_gb']  = None
        checks['ram_ok']       = None

    # internet
    try:
        _req.head('https://api.openshift.com', timeout=5)
        checks['internet'] = True
    except Exception:
        checks['internet'] = False

    return jsonify(checks)


# ── Deployment ────────────────────────────────────────────────────────────────

@ocp_bp.route('/api/openshift/deploy', methods=['POST'])
def deploy():
    err = _auth()
    if err:
        return err

    cfg = request.get_json(silent=True) or {}
    required = ['cluster_name', 'base_domain', 'pull_secret', 'offline_token',
                'ocp_version', 'deployment_type', 'machine_cidr']
    missing = [f for f in required if not cfg.get(f)]
    if missing:
        return jsonify({'error': f'Missing fields: {", ".join(missing)}'}), 400

    job_id = uuid.uuid4().hex[:8]
    # Omit secrets from the stored config summary shown in the UI
    safe_cfg = {k: v for k, v in cfg.items() if k not in ('pull_secret', 'offline_token')}
    with _lock:
        _jobs[job_id] = {
            'id':       job_id,
            'status':   'pending',
            'phase':    'Starting',
            'progress': 0,
            'logs':     [],
            'config':   safe_cfg,
            'result':   None,
            'created':  time.time(),
        }
        _save_jobs()

    t = threading.Thread(target=_run_deploy, args=(job_id, cfg), daemon=True,
                         name=f'ocp-deploy-{job_id}')
    t.start()
    return jsonify({'job_id': job_id})


@ocp_bp.route('/api/openshift/jobs')
def list_jobs():
    err = _auth()
    if err:
        return err
    jobs = sorted(_jobs.values(), key=lambda j: j['created'], reverse=True)
    # Don't return full logs in list view
    return jsonify({'jobs': [{**j, 'logs': j['logs'][-5:]} for j in jobs]})


@ocp_bp.route('/api/openshift/jobs/<job_id>')
def job_detail(job_id):
    err = _auth()
    if err:
        return err
    job = _jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify(job)


@ocp_bp.route('/api/openshift/jobs/<job_id>', methods=['DELETE'])
def delete_job(job_id):
    err = _auth()
    if err:
        return err
    with _lock:
        _jobs.pop(job_id, None)
        _save_jobs()
    return jsonify({'ok': True})


# ── CDROM eject helper ───────────────────────────────────────────────────────

def _eject_cdroms(vm_names: list, log_fn):
    """Eject the discovery ISO from all VMs so the next reboot boots from disk.

    Called right after installation is triggered — the ISO is no longer needed
    and leaving it as boot-order-1 causes the Assisted Installer 'pending user
    action: expected to boot from disk' error.
    """
    try:
        conn = libvirt.open('qemu:///system')
    except Exception as e:
        log_fn(f'  Cannot open libvirt for CDROM eject: {e}', 'warn')
        return

    for vm_name in vm_names:
        try:
            dom = conn.lookupByName(vm_name)
            xml_str = dom.XMLDesc(0)
            root    = ET.fromstring(xml_str)
            for disk in root.findall('.//disk'):
                if disk.get('device') != 'cdrom':
                    continue
                target_el = disk.find('target')
                if target_el is None:
                    continue
                dev = target_el.get('dev', 'sda')
                bus = target_el.get('bus', 'sata')
                # Empty CDROM (no <source>) = ejected; also clear boot order
                empty_xml = (
                    f"<disk type='file' device='cdrom'>"
                    f"<driver name='qemu' type='raw'/>"
                    f"<target dev='{dev}' bus='{bus}'/>"
                    f"<readonly/>"
                    f"</disk>"
                )
                try:
                    dom.updateDeviceFlags(
                        empty_xml,
                        libvirt.VIR_DOMAIN_AFFECT_LIVE |
                        libvirt.VIR_DOMAIN_AFFECT_CONFIG,
                    )
                    log_fn(f'  Ejected ISO from {vm_name} ({dev}) ✓')
                except libvirt.libvirtError as e:
                    log_fn(f'  CDROM eject warning ({vm_name}): {e}', 'warn')
        except libvirt.libvirtError:
            pass  # VM not found — skip

    conn.close()


def _reboot_vms(vm_names: list, log_fn):
    """Soft-reboot VMs (used to recover from pending-user-action)."""
    try:
        conn = libvirt.open('qemu:///system')
    except Exception:
        return
    for vm_name in vm_names:
        try:
            dom = conn.lookupByName(vm_name)
            if dom.isActive():
                dom.reboot(0)
                log_fn(f'  Rebooted {vm_name} ✓')
        except libvirt.libvirtError as e:
            log_fn(f'  Reboot warning ({vm_name}): {e}', 'warn')
    conn.close()


# ── MAC address generation ────────────────────────────────────────────────────

def _make_mac(job_id: str, node_idx: int) -> str:
    """Deterministic KVM MAC address (52:54:00:XX:XX:XX) for a deployment node.
    Same job+index always produces the same MAC so nmstate static config
    generated at infra-env creation time matches the actual VM NIC.
    """
    h = hashlib.md5(f'{job_id}:{node_idx}'.encode()).hexdigest()
    return f'52:54:00:{h[0:2]}:{h[2:4]}:{h[4:6]}'


# ── nmstate YAML builder ──────────────────────────────────────────────────────

def _build_nmstate_yaml(mac: str, ip: str, prefix_len: int,
                        gateway: str, dns_list: list) -> str:
    """Build nmstate YAML for one node (Assisted Installer static_network_config)."""
    dns_entries = ''.join(f'\n      - {d}' for d in dns_list)
    routes_section = ''
    if gateway:
        routes_section = (
            f'routes:\n'
            f'  config:\n'
            f'    - destination: 0.0.0.0/0\n'
            f'      next-hop-address: {gateway}\n'
            f'      next-hop-interface: eth0\n'
        )
    return (
        f'interfaces:\n'
        f'  - name: eth0\n'
        f'    type: ethernet\n'
        f'    state: up\n'
        f'    mac-address: "{mac}"\n'
        f'    ipv4:\n'
        f'      enabled: true\n'
        f'      dhcp: false\n'
        f'      address:\n'
        f'        - ip: {ip}\n'
        f'          prefix-length: {prefix_len}\n'
        f'dns-resolver:\n'
        f'  config:\n'
        f'    server:{dns_entries}\n'
        + routes_section
    )


# ── VM XML generation ─────────────────────────────────────────────────────────

def _vm_xml(name: str, vcpus: int, ram_mb: int, disk_path: str,
             iso_path: str, network: str = 'default',
             host_bridge: bool = False, extra_disks: list = None,
             mac_address: str = None) -> str:
    # libvirt-managed network vs host bridge (e.g. br-real) need different XML
    mac_xml = f"\n      <mac address='{mac_address}'/>" if mac_address else ''
    if host_bridge:
        iface_xml = f"""<interface type='bridge'>
      <source bridge='{network}'/>{mac_xml}
      <model type='virtio'/>
    </interface>"""
    else:
        iface_xml = f"""<interface type='network'>
      <source network='{network}'/>{mac_xml}
      <model type='virtio'/>
    </interface>"""

    # Build extra disk XML (vdb, vdc, …)
    extra_disks_xml = ''
    for idx, ep in enumerate(extra_disks or []):
        dev = 'vd' + chr(ord('b') + idx)   # vdb, vdc, vdd …
        extra_disks_xml += f"""
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2' cache='none'/>
      <source file='{ep}'/>
      <target dev='{dev}' bus='virtio'/>
    </disk>"""

    return f"""
<domain type='kvm'>
  <name>{name}</name>
  <uuid>{uuid.uuid4()}</uuid>
  <memory unit='MiB'>{ram_mb}</memory>
  <vcpu>{vcpus}</vcpu>
  <os>
    <type arch='x86_64' machine='q35'>hvm</type>
  </os>
  <features><acpi/><apic/></features>
  <cpu mode='host-passthrough'/>
  <clock offset='utc'/>
  <devices>
    <disk type='file' device='cdrom'>
      <driver name='qemu' type='raw'/>
      <source file='{iso_path}'/>
      <target dev='sda' bus='sata'/>
      <readonly/>
      <boot order='1'/>
    </disk>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2' cache='none'/>
      <source file='{disk_path}'/>
      <target dev='vda' bus='virtio'/>
      <boot order='2'/>
    </disk>{extra_disks_xml}
    {iface_xml}
    <graphics type='vnc' port='-1' autoport='yes' listen='127.0.0.1'>
      <listen type='address' address='127.0.0.1'/>
    </graphics>
    <video><model type='vga' vram='16384' heads='1'/></video>
    <console type='pty'/>
  </devices>
</domain>"""


# ── Background deployment worker ──────────────────────────────────────────────

def _run_deploy(job_id: str, cfg: dict):
    """Full deployment pipeline — runs in a daemon thread."""

    def log(msg, level='info'):
        _job_log(job_id, msg, level)

    def phase(name, pct):
        _job_set(job_id, phase=name, progress=pct)
        log(f'── {name} ──')

    def fail(msg):
        _job_set(job_id, status='failed', phase='Failed')
        log(msg, 'error')

    try:
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        cluster_name = cfg['cluster_name']
        job_dir      = WORK_DIR / job_id
        job_dir.mkdir(exist_ok=True)

        deployment_type = cfg.get('deployment_type', 'sno')
        is_sno          = (deployment_type == 'sno')
        n_control       = 1 if is_sno else int(cfg.get('control_plane_count', 3))
        n_workers       = 0 if is_sno else int(cfg.get('worker_count', 2))
        total_nodes     = n_control + n_workers

        # Pre-compute VM names + deterministic MAC addresses so they can be
        # referenced in both the infra-env (static_network_config) and
        # the libvirt XML (NIC MAC address) and they always match.
        if is_sno:
            vm_names_pre = [f'{cluster_name}-sno']
        else:
            vm_names_pre  = [f'{cluster_name}-master-{i}' for i in range(n_control)]
            vm_names_pre += [f'{cluster_name}-worker-{i}' for i in range(n_workers)]
        mac_map = {name: _make_mac(job_id, i) for i, name in enumerate(vm_names_pre)}

        # ── Step 1: Auth ──────────────────────────────────────────────────────
        phase('Authenticating with Red Hat', 5)
        try:
            token = _get_access_token(cfg['offline_token'])
            log('Access token obtained ✓')
        except Exception as e:
            fail(f'Authentication failed: {e}')
            return

        # ── Step 2: Create cluster ────────────────────────────────────────────
        phase('Creating cluster record', 10)
        cluster_payload = {
            'name':                   cluster_name,
            'openshift_version':      cfg['ocp_version'],
            'base_dns_domain':        cfg['base_domain'],
            'pull_secret':            cfg['pull_secret'],
            'ssh_public_key':         cfg.get('ssh_public_key', ''),
            'high_availability_mode': 'None' if is_sno else 'Full',
            'network_type':           'OVNKubernetes',
            'machine_networks':       [{'cidr': cfg['machine_cidr']}],
            'cluster_networks':       [{'cidr': cfg.get('cluster_cidr', '10.128.0.0/14'),
                                        'host_prefix': 23}],
            'service_networks':       [{'cidr': cfg.get('service_cidr', '172.30.0.0/16')}],
        }
        if not is_sno:
            cluster_payload['api_vips']     = [{'ip': cfg['api_vip']}]
            cluster_payload['ingress_vips'] = [{'ip': cfg['ingress_vip']}]

        try:
            r = _ai('POST', '/clusters', token, cluster_payload)
            cluster_id = r.json()['id']
            log(f'Cluster created: {cluster_id} ✓')
        except Exception as e:
            fail(f'Failed to create cluster: {e}')
            return

        _job_set(job_id, cluster_id=cluster_id)

        # ── Step 3: Create infra-env ──────────────────────────────────────────
        phase('Creating infrastructure environment', 18)
        infra_payload = {
            'name':              f'{cluster_name}-infra',
            'cluster_id':        cluster_id,
            'openshift_version': cfg['ocp_version'],
            'pull_secret':       cfg['pull_secret'],
            'image_type':        'minimal-iso',
            'cpu_architecture':  'x86_64',
        }
        if cfg.get('ssh_public_key', '').strip():
            infra_payload['ssh_authorized_key'] = cfg['ssh_public_key'].strip()

        # Static network configuration — only if the user requested it
        if cfg.get('static_ip_enabled') and cfg.get('node_ips'):
            import ipaddress as _ipaddress
            machine_cidr = cfg.get('machine_cidr', '192.168.122.0/24')
            try:
                prefix_len = int(_ipaddress.ip_network(machine_cidr, strict=False).prefixlen)
            except Exception:
                prefix_len = 24

            gateway  = cfg.get('gateway', '').strip()
            dns_raw  = cfg.get('dns_servers', '8.8.8.8').strip()
            dns_list = [s.strip() for s in dns_raw.split(',') if s.strip()] or ['8.8.8.8']

            # Build per-node nmstate entries; skip nodes with no IP configured
            node_ip_map = {e['name']: e['ip'] for e in cfg['node_ips'] if e.get('ip')}
            static_cfg  = []
            for vm_name in vm_names_pre:
                ip = node_ip_map.get(vm_name, '').strip()
                if not ip:
                    continue
                mac = mac_map[vm_name]
                network_yaml = _build_nmstate_yaml(mac, ip, prefix_len, gateway, dns_list)
                static_cfg.append({
                    'network_yaml':      network_yaml,
                    'mac_interface_map': [{'mac_address': mac, 'logical_nic_name': 'eth0'}],
                })

            if static_cfg:
                infra_payload['static_network_config'] = static_cfg
                log(f'Static IP config prepared for {len(static_cfg)} node(s) ✓')
            else:
                log('Static IP enabled but no node IPs provided — falling back to DHCP', 'warn')

        try:
            r = _ai('POST', '/infra-envs', token, infra_payload)
            infra_env_id   = r.json()['id']
            log(f'Infra-env created: {infra_env_id} ✓')
        except Exception as e:
            fail(f'Failed to create infra-env: {e}')
            return

        # ── Step 4: Download discovery ISO ───────────────────────────────────
        phase('Downloading discovery ISO', 22)
        iso_path = job_dir / 'discovery.iso'
        try:
            r = _ai('GET', f'/infra-envs/{infra_env_id}/downloads/image-url', token)
            iso_url = r.json()['url']
            log(f'ISO URL obtained, downloading (~100 MB)…')
            with _req.get(iso_url, stream=True, timeout=300) as dl:
                dl.raise_for_status()
                total = int(dl.headers.get('content-length', 0))
                done  = 0
                with open(iso_path, 'wb') as f:
                    for chunk in dl.iter_content(chunk_size=1024 * 1024):
                        f.write(chunk)
                        done += len(chunk)
                        if total:
                            pct = 22 + int(done / total * 10)
                            _job_set(job_id, progress=pct)
            log(f'ISO downloaded: {iso_path} ✓')
            # QEMU runs as libvirt-qemu and can't read files in a user home dir
            # unless the file and all parent directories are world-readable.
            os.chmod(iso_path, 0o644)
            # Walk up to WORK_DIR making sure each dir is o+x so qemu can traverse
            p = iso_path.parent
            while p != WORK_DIR.parent:
                try:
                    current = p.stat().st_mode
                    os.chmod(p, current | 0o111)  # add execute/search for all
                except Exception:
                    pass
                p = p.parent
        except Exception as e:
            fail(f'ISO download failed: {e}')
            return

        # ── Step 5: Create VMs ────────────────────────────────────────────────
        phase('Creating KVM virtual machines', 35)
        vm_names = vm_names_pre  # already computed above for MAC generation

        vcpus_cp  = int(cfg.get('cp_vcpus',  8))
        ram_cp    = int(cfg.get('cp_ram_gb',  32)) * 1024
        disk_cp   = int(cfg.get('cp_disk_gb', 120))
        vcpus_w   = int(cfg.get('w_vcpus',   4))
        ram_w     = int(cfg.get('w_ram_gb',   16)) * 1024
        disk_w    = int(cfg.get('w_disk_gb',  100))

        # Extra disks: [{size_gb: N}, …] — same list applied to every VM
        extra_disk_specs = cfg.get('extra_disks', [])   # list of {size_gb}

        try:
            conn = libvirt.open('qemu:///system')
        except Exception as e:
            fail(f'Cannot connect to libvirt: {e}')
            return

        disk_dir = Path(cfg.get('storage_path', '/var/lib/libvirt/images'))
        created_vms = []

        def _make_disk(path: Path, size_gb: int) -> bool:
            """Create a qcow2 at path, chmod it. Returns True on success."""
            if path.exists():
                path.unlink()
            r = subprocess.run(
                ['qemu-img', 'create', '-f', 'qcow2', str(path), f'{size_gb}G'],
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                fail(f'qemu-img failed ({path.name}): {r.stderr}')
                return False
            try:
                os.chmod(path, 0o644)
            except Exception:
                pass
            return True

        try:
            for i, vm_name in enumerate(vm_names):
                is_worker = (not is_sno) and (i >= n_control)
                vcpus   = vcpus_w   if is_worker else vcpus_cp
                ram_mb  = ram_w     if is_worker else ram_cp
                disk_gb = disk_w    if is_worker else disk_cp

                disk_path = disk_dir / f'{vm_name}.qcow2'

                # Clean up any leftover VM from a previous failed attempt
                try:
                    old = conn.lookupByName(vm_name)
                    if old.isActive():
                        old.destroy()
                    old.undefine()
                    log(f'Removed existing VM {vm_name}')
                except libvirt.libvirtError:
                    pass

                # Create primary disk
                if not _make_disk(disk_path, disk_gb):
                    return

                # Create extra disks (vdb, vdc, …)
                extra_paths = []
                for ei, espec in enumerate(extra_disk_specs):
                    esize = int(espec.get('size_gb', 100))
                    epath = disk_dir / f'{vm_name}-extra{ei+1}.qcow2'
                    if not _make_disk(epath, esize):
                        return
                    extra_paths.append(str(epath))
                    log(f'  Extra disk {ei+1}: {epath.name} ({esize} GB)')

                net_name = cfg.get('libvirt_network', 'default')
                is_host_bridge = os.path.isdir(f'/sys/class/net/{net_name}/bridge')
                xml = _vm_xml(
                    name        = vm_name,
                    vcpus       = vcpus,
                    ram_mb      = ram_mb,
                    disk_path   = str(disk_path),
                    iso_path    = str(iso_path),
                    network     = net_name,
                    host_bridge = is_host_bridge,
                    extra_disks = extra_paths,
                    mac_address = mac_map.get(vm_name),   # deterministic MAC
                )
                dom = conn.defineXML(xml)
                dom.create()
                created_vms.append(vm_name)
                role = 'worker' if is_worker else ('SNO' if is_sno else 'control-plane')
                extra_info = f' + {len(extra_paths)} extra disk(s)' if extra_paths else ''
                mac_info   = f', MAC {mac_map[vm_name]}' if vm_name in mac_map else ''
                log(f'VM {vm_name} started ({vcpus} vCPU, {ram_mb//1024} GB RAM, {disk_gb} GB{extra_info}, {role}{mac_info}) ✓')
                # Stagger starts so the host isn't overwhelmed with simultaneous boot I/O
                if i < len(vm_names) - 1:
                    time.sleep(15)

        except Exception as e:
            conn.close()
            fail(f'VM creation failed: {e}')
            return
        finally:
            conn.close()

        _job_set(job_id, vms=created_vms)

        # ── Step 6: Wait for host discovery ──────────────────────────────────
        phase('Waiting for nodes to register', 45)
        log(f'Waiting for {total_nodes} node(s) to boot and register…')
        deadline = time.time() + 45 * 60   # 45 min timeout
        registered = []

        while time.time() < deadline:
            try:
                # Refresh token if needed
                token = _get_access_token(cfg['offline_token'])
                r = _ai('GET', f'/clusters/{cluster_id}/hosts', token)
                hosts = r.json()
                registered = [h for h in hosts if h.get('status') not in ('', None, 'disconnected')]
                known_count = len(registered)
                log(f'  {known_count}/{total_nodes} node(s) discovered')
                _job_set(job_id, progress=45 + min(known_count, total_nodes) * 3)

                # Log which VMs haven't shown up yet
                if known_count < total_nodes:
                    registered_names = {h.get('requested_hostname', '') for h in registered}
                    missing = [n for n in vm_names if not any(n in rn for rn in registered_names)]
                    if missing:
                        # Check libvirt state so we know if they crashed vs. still booting
                        try:
                            lv = libvirt.open('qemu:///system')
                            for vm in missing:
                                try:
                                    d = lv.lookupByName(vm)
                                    state_map = {0: 'nostate', 1: 'running', 2: 'blocked',
                                                 3: 'paused', 4: 'shutdown', 5: 'shutoff', 6: 'crashed'}
                                    st = state_map.get(d.state()[0], 'unknown')
                                    if st not in ('running',):
                                        log(f'  WARNING: {vm} is {st} — attempting restart', 'warn')
                                        try:
                                            if d.isActive(): d.destroy()
                                            d.create()
                                        except Exception:
                                            pass
                                    else:
                                        log(f'  {vm}: running, still booting…')
                                except libvirt.libvirtError:
                                    log(f'  {vm}: not found in libvirt', 'warn')
                            lv.close()
                        except Exception:
                            pass

                if known_count >= total_nodes:
                    # Check all are in a ready-ish state
                    ready = [h for h in registered
                             if h.get('status') in ('known', 'known-unbound')]
                    if len(ready) >= total_nodes:
                        log(f'All {total_nodes} node(s) ready ✓')
                        break
            except Exception as e:
                log(f'  Polling error (retrying): {e}', 'warn')

            time.sleep(20)
        else:
            fail(f'Timeout waiting for {total_nodes} nodes to register. '
                 f'Only {len(registered)} registered.')
            return

        # Set host roles for multi-node
        if not is_sno:
            phase('Assigning node roles', 55)
            token = _get_access_token(cfg['offline_token'])
            r = _ai('GET', f'/clusters/{cluster_id}/hosts', token)
            hosts = r.json()
            for idx, host in enumerate(hosts[:len(vm_names)]):
                role = 'worker' if idx >= n_control else 'master'
                try:
                    _ai('PATCH', f'/infra-envs/{infra_env_id}/hosts/{host["id"]}',
                        token, {'role': role})
                    log(f'  Set {host.get("requested_hostname", host["id"])} → {role}')
                except Exception as e:
                    log(f'  Role assignment warning: {e}', 'warn')

        # ── Step 7: Start installation ────────────────────────────────────────
        phase('Starting OpenShift installation', 60)
        try:
            token = _get_access_token(cfg['offline_token'])
            _ai('POST', f'/clusters/{cluster_id}/actions/install', token)
            log('Installation triggered ✓')
        except Exception as e:
            fail(f'Failed to trigger installation: {e}')
            return

        # Eject discovery ISO from all VMs immediately after install starts.
        # Without this, the VM reboots back into the ISO instead of the disk,
        # causing Assisted Installer "pending user action: boot from disk" error.
        phase('Ejecting discovery ISO', 62)
        log('Ejecting ISO from VMs so next reboot boots from disk…')
        _eject_cdroms(vm_names, log)

        # ── Step 8: Monitor installation ──────────────────────────────────────
        phase('Installing OpenShift', 65)
        log('Installation in progress — this takes 45–90 minutes…')
        deadline = time.time() + 2 * 3600  # 2 hours
        last_status    = ''
        last_pct       = 0
        pending_handled = set()  # track VMs already rebooted for pending-user-action

        PHASE_PCT = {
            'preparing-for-installation': 65,
            'installing':                 70,
            'installing-in-progress':     70,
            'finalizing':                 88,
            'installed':                  100,
        }

        consecutive_errors = 0

        while time.time() < deadline:
            try:
                token = _get_access_token(cfg['offline_token'])
                r = _ai('GET', f'/clusters/{cluster_id}', token)
                cluster_data = r.json()
                status      = cluster_data.get('status', '')
                status_info = cluster_data.get('status_info', '')
                install_pct = cluster_data.get('progress', {}).get('total_percentage', 0)

                consecutive_errors = 0  # reset on success

                if status != last_status or install_pct != last_pct:
                    log(f'  Status: {status} ({install_pct}%) — {status_info}')
                    last_status = status
                    last_pct    = install_pct
                    pct = PHASE_PCT.get(status, 70) + int(install_pct * 0.25)
                    _job_set(job_id, progress=min(pct, 98))

                if status == 'installed':
                    break
                if status in ('error', 'cancelled'):
                    fail(f'Installation {status}: {status_info}')
                    return

                # ── Check for pending-user-action on individual hosts ──────────
                # Happens when a host rebooted back into the ISO instead of disk.
                # Re-eject CDROM and reboot to recover automatically.
                try:
                    hr = _ai('GET', f'/clusters/{cluster_id}/hosts', token)
                    for h in hr.json():
                        h_status = h.get('status', '')
                        h_id     = h.get('id', '')
                        hostname = h.get('requested_hostname') or h_id[:8]
                        if 'pending-user-action' in h_status and h_id not in pending_handled:
                            log(f'  ⚠ Host {hostname} booted ISO instead of disk — auto-recovering…', 'warn')
                            _eject_cdroms(vm_names, log)
                            time.sleep(3)
                            _reboot_vms(vm_names, log)
                            pending_handled.add(h_id)
                except Exception:
                    pass  # don't fail monitoring on host-check errors

            except Exception as e:
                consecutive_errors += 1
                if consecutive_errors == 1:
                    log(f'  Monitoring error (retrying): {e}', 'warn')
                elif consecutive_errors % 5 == 0:
                    log(f'  Still unreachable after {consecutive_errors} attempts — installation continues on VMs', 'warn')
                wait = min(30 * (2 ** min(consecutive_errors - 1, 4)), 300)
                time.sleep(wait)
                continue

            time.sleep(30)
        else:
            fail('Installation timed out after 2 hours.')
            return

        # ── Step 9: Fetch credentials ─────────────────────────────────────────
        phase('Collecting credentials', 98)
        result = {}
        try:
            token = _get_access_token(cfg['offline_token'])
            r = _ai('GET', f'/clusters/{cluster_id}/credentials', token)
            creds = r.json()
            kubeconfig_raw = creds.get('kubeconfig', '')
            result['kubeadmin_password'] = creds.get('password', '')

            kc_path = job_dir / 'kubeconfig'
            kc_path.write_text(kubeconfig_raw)
            result['kubeconfig_path'] = str(kc_path)
            log(f'kubeconfig saved → {kc_path} ✓')

        except Exception as e:
            log(f'Credential fetch warning (cluster is installed): {e}', 'warn')

        api_url = f'https://api.{cluster_name}.{cfg["base_domain"]}:6443'
        console = f'https://console-openshift-console.apps.{cluster_name}.{cfg["base_domain"]}'
        result.update({
            'api_url':     api_url,
            'console_url': console,
            'cluster_id':  cluster_id,
        })
        _job_set(job_id, result=result, status='complete', progress=100, phase='Complete')
        log(f'OpenShift installation complete! 🎉')
        log(f'Console: {console}')
        log(f'API:     {api_url}')
        if result.get('kubeadmin_password'):
            log(f'kubeadmin password saved in job result.')

    except Exception as e:
        _job_set(job_id, status='failed', phase='Failed')
        _job_log(job_id, f'Unexpected error: {e}', 'error')


# ── Kubeconfig download ───────────────────────────────────────────────────────

@ocp_bp.route('/api/openshift/jobs/<job_id>/kubeconfig')
def download_kubeconfig(job_id):
    err = _auth()
    if err:
        return err
    job = _jobs.get(job_id)
    if not job or not job.get('result', {}).get('kubeconfig_path'):
        return jsonify({'error': 'kubeconfig not available'}), 404
    kc_path = Path(job['result']['kubeconfig_path'])
    if not kc_path.exists():
        return jsonify({'error': 'kubeconfig file not found'}), 404
    from flask import send_file
    return send_file(str(kc_path), as_attachment=True,
                     download_name='kubeconfig', mimetype='text/plain')
