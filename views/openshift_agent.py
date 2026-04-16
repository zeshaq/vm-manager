"""OpenShift Agent-Based Installer backend.

Uses the `openshift-install` binary (not the Red Hat Assisted Installer API).
Generates install-config.yaml + agent-config.yaml, creates the agent ISO,
boots VMs from it, then monitors installation via openshift-install subcommands.

Key differences from the AI-based installer (views/openshift.py):
  • No Red Hat cloud API — works in air-gapped / disconnected environments
  • Uses `openshift-install agent create image` to build a self-contained ISO
  • Static IPs are embedded in the ISO (NMState network config)
  • Installation monitored via `openshift-install agent wait-for …`
  • Requires the openshift-install binary for the target OCP version
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import textwrap
import threading
import time
import uuid
from pathlib import Path

import libvirt
import requests
from flask import Blueprint, jsonify, request, send_file

# ── Blueprint ─────────────────────────────────────────────────────────────────
agent_bp = Blueprint('openshift_agent', __name__)

# ── Persistent storage ────────────────────────────────────────────────────────
WORK_DIR    = Path.home() / 'hypercloud' / 'ocp-agent'
_JOBS_FILE  = WORK_DIR / 'jobs.json'
_CREDS_FILE = WORK_DIR / 'credentials.json'

_jobs:        dict  = {}
_lock               = threading.Lock()
_running_jobs: set  = set()
_stop_jobs:    set  = set()

# ── Task definitions (order matters — matches deployment pipeline) ─────────────
AGENT_TASKS = [
    {'id': 'binary',    'name': 'Locate / download openshift-install binary'},
    {'id': 'config',    'name': 'Generate configuration files'},
    {'id': 'iso',       'name': 'Build agent ISO'},
    {'id': 'vms',       'name': 'Create virtual machines'},
    {'id': 'bootstrap', 'name': 'Wait for bootstrap complete'},
    {'id': 'install',   'name': 'Install OpenShift'},
    {'id': 'creds',     'name': 'Collect credentials & kubeconfig'},
]

# ── Job persistence ───────────────────────────────────────────────────────────

def _load_jobs():
    global _jobs
    try:
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        if _JOBS_FILE.exists():
            _jobs = json.loads(_JOBS_FILE.read_text())
    except Exception:
        _jobs = {}

def _save_jobs():
    try:
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _JOBS_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps(_jobs))
        tmp.replace(_JOBS_FILE)
    except Exception:
        pass

_load_jobs()

def _job_log(job_id: str, msg: str, level: str = 'info'):
    with _lock:
        if job_id in _jobs:
            _jobs[job_id].setdefault('logs', []).append(
                {'ts': time.strftime('%H:%M:%S'), 'msg': msg, 'level': level}
            )
            _save_jobs()

def _job_set(job_id: str, **kw):
    with _lock:
        if job_id in _jobs:
            _jobs[job_id].update(kw)
            _save_jobs()

# ── Task tracking helpers ─────────────────────────────────────────────────────

def _fresh_tasks() -> list:
    return [
        {'id': t['id'], 'name': t['name'], 'status': 'pending',
         'started_at': None, 'completed_at': None, 'detail': ''}
        for t in AGENT_TASKS
    ]

def _init_tasks(job_id: str):
    with _lock:
        if job_id not in _jobs:
            return
        _jobs[job_id]['tasks'] = _fresh_tasks()
        _save_jobs()

def _task_start(job_id: str, task_id: str, detail: str = ''):
    with _lock:
        if job_id not in _jobs:
            return
        tasks = _jobs[job_id].setdefault('tasks', _fresh_tasks())
        for t in tasks:
            if t['id'] == task_id:
                t['status']     = 'running'
                t['started_at'] = time.strftime('%H:%M:%S')
                if detail:
                    t['detail'] = detail
                break
        _save_jobs()

def _task_done(job_id: str, task_id: str, detail: str = ''):
    with _lock:
        if job_id not in _jobs:
            return
        for t in _jobs[job_id].get('tasks', []):
            if t['id'] == task_id:
                t['status']       = 'done'
                t['completed_at'] = time.strftime('%H:%M:%S')
                if detail:
                    t['detail'] = detail
                break
        _save_jobs()

def _task_fail(job_id: str, task_id: str, detail: str = ''):
    with _lock:
        if job_id not in _jobs:
            return
        for t in _jobs[job_id].get('tasks', []):
            if t['id'] == task_id:
                t['status']       = 'failed'
                t['completed_at'] = time.strftime('%H:%M:%S')
                if detail:
                    t['detail'] = detail
                break
        _save_jobs()

def _task_skip(job_id: str, task_id: str, detail: str = ''):
    with _lock:
        if job_id not in _jobs:
            return
        for t in _jobs[job_id].get('tasks', []):
            if t['id'] == task_id:
                t['status']       = 'skipped'
                t['completed_at'] = time.strftime('%H:%M:%S')
                if detail:
                    t['detail'] = detail
                break
        _save_jobs()

# ── Auth helper ───────────────────────────────────────────────────────────────

def _auth():
    from flask import session
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    return None

# ── openshift-install binary helpers ─────────────────────────────────────────

def _find_oi_binary(version: str | None = None) -> Path | None:
    """Find openshift-install binary.  Looks in:
       1. ~/hypercloud/ocp-agent/bin/<version>/openshift-install
       2. ~/hypercloud/ocp-agent/bin/openshift-install   (unversioned)
       3. PATH
    """
    candidates = []
    if version:
        candidates.append(WORK_DIR / 'bin' / version / 'openshift-install')
    candidates.append(WORK_DIR / 'bin' / 'openshift-install')
    for c in candidates:
        if c.exists() and os.access(c, os.X_OK):
            return c
    found = shutil.which('openshift-install')
    return Path(found) if found else None


def _oi_version(binary: Path) -> str:
    """Return the version string reported by the binary."""
    try:
        r = subprocess.run([str(binary), 'version'], capture_output=True, text=True, timeout=10)
        for line in r.stdout.splitlines():
            if line.startswith('openshift-install'):
                return line.split()[1]
    except Exception:
        pass
    return 'unknown'


def _download_oi_binary(version: str, log_fn) -> Path | None:
    """Download openshift-install (and oc) for the requested version."""
    bin_dir = WORK_DIR / 'bin' / version
    bin_dir.mkdir(parents=True, exist_ok=True)
    dest = bin_dir / 'openshift-install'
    if dest.exists():
        # Also grab oc if it was missed on a previous run
        _ensure_oc(version, bin_dir, log_fn)
        return dest

    url = (f'https://mirror.openshift.com/pub/openshift-v4/clients/ocp/'
           f'{version}/openshift-install-linux.tar.gz')
    log_fn(f'  Downloading openshift-install {version}…')
    try:
        resp = requests.get(url, stream=True, timeout=120)
        resp.raise_for_status()
        tarball = bin_dir / 'openshift-install-linux.tar.gz'
        with open(tarball, 'wb') as f:
            for chunk in resp.iter_content(65536):
                f.write(chunk)
        subprocess.run(['tar', 'xzf', str(tarball), '-C', str(bin_dir),
                        'openshift-install'], check=True, timeout=60)
        tarball.unlink(missing_ok=True)
        dest.chmod(0o755)
        log_fn(f'  openshift-install {version} ready ✓')
        _ensure_oc(version, bin_dir, log_fn)
        return dest
    except Exception as e:
        log_fn(f'  Download failed: {e}', 'error')
        return None


def _ensure_oc(version: str, bin_dir: Path, log_fn) -> None:
    """Download the oc client into bin_dir if not already present.

    openshift-install agent create image requires oc in PATH to extract
    the base ISO from the release payload.
    """
    oc_dest = bin_dir / 'oc'
    if oc_dest.exists():
        return
    url = (f'https://mirror.openshift.com/pub/openshift-v4/clients/ocp/'
           f'{version}/openshift-client-linux.tar.gz')
    log_fn(f'  Downloading oc client {version}…')
    try:
        resp = requests.get(url, stream=True, timeout=120)
        resp.raise_for_status()
        tarball = bin_dir / 'openshift-client-linux.tar.gz'
        with open(tarball, 'wb') as f:
            for chunk in resp.iter_content(65536):
                f.write(chunk)
        subprocess.run(['tar', 'xzf', str(tarball), '-C', str(bin_dir), 'oc'],
                       check=True, timeout=60)
        tarball.unlink(missing_ok=True)
        oc_dest.chmod(0o755)
        log_fn(f'  oc {version} ready ✓')
    except Exception as e:
        log_fn(f'  oc download failed (non-fatal): {e}', 'warn')

# ── VM helpers (reused from openshift.py pattern) ─────────────────────────────

def _make_mac(job_id: str, node_idx: int) -> str:
    h = hashlib.md5(f'agent:{job_id}:{node_idx}'.encode()).hexdigest()
    return f'52:54:00:{h[0:2]}:{h[2:4]}:{h[4:6]}'


def _create_disk(path: Path, size_gb: int) -> None:
    subprocess.run(
        ['qemu-img', 'create', '-f', 'qcow2', str(path), f'{size_gb}G'],
        check=True, capture_output=True, timeout=60,
    )


def _list_host_bridges() -> list[str]:
    """Return bridge interface names visible to the OS."""
    try:
        r = subprocess.run(
            ['ip', '-j', 'link', 'show', 'type', 'bridge'],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            import json as _json
            return [i.get('ifname', '') for i in _json.loads(r.stdout or '[]')]
    except Exception:
        pass
    return []


def _resolve_bridge(network: str) -> str:
    """Resolve a libvirt network name or bridge name to an actual bridge name.

    Tries (in order):
    1. If it's already a known OS bridge interface → use it directly
    2. If it's a libvirt-managed network → look up its bridge name
    3. Fall back to the value as-is (libvirt may resolve it)
    """
    bridges = _list_host_bridges()
    if network in bridges:
        return network

    # Try to resolve as a libvirt network name
    try:
        import xml.etree.ElementTree as ET
        conn = libvirt.open('qemu:///system')
        try:
            net = conn.networkLookupByName(network)
            xml_str = net.XMLDesc()
            br = ET.fromstring(xml_str).findtext('bridge') or \
                 ET.fromstring(xml_str).find('bridge').get('name', '')
            if br:
                return br
        except libvirt.libvirtError:
            pass
        finally:
            conn.close()
    except Exception:
        pass

    return network   # return as-is; libvirt may still handle it


def _create_vm(name: str, vcpus: int, ram_mb: int, disk_path: Path,
               iso_path: Path, bridge: str, mac: str) -> None:
    """Define and start a libvirt domain for an agent-install node."""
    xml = f"""<domain type='kvm'>
  <name>{name}</name>
  <memory unit='MiB'>{ram_mb}</memory>
  <vcpu>{vcpus}</vcpu>
  <os firmware='efi'>
    <type arch='x86_64' machine='q35'>hvm</type>
  </os>
  <features><acpi/><apic/></features>
  <cpu mode='host-passthrough'/>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2' cache='none'/>
      <source file='{disk_path}'/>
      <target dev='vda' bus='virtio'/>
      <boot order='1'/>
    </disk>
    <disk type='file' device='cdrom'>
      <driver name='qemu' type='raw'/>
      <source file='{iso_path}'/>
      <target dev='sda' bus='sata'/>
      <readonly/>
      <boot order='2'/>
    </disk>
    <interface type='bridge'>
      <source bridge='{bridge}'/>
      <mac address='{mac}'/>
      <model type='virtio'/>
    </interface>
    <serial type='pty'><target port='0'/></serial>
    <console type='pty'><target type='serial' port='0'/></console>
    <graphics type='vnc' port='-1' listen='127.0.0.1'/>
    <video><model type='vga' vram='16384' heads='1'/></video>
  </devices>
</domain>"""
    conn = libvirt.open('qemu:///system')
    try:
        dom = conn.defineXML(xml)
        dom.create()
    except Exception:
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass

# ── Config YAML generators ────────────────────────────────────────────────────

def _install_config(cfg: dict) -> str:
    cluster_name = cfg['cluster_name']
    base_domain  = cfg['base_domain']
    pull_secret  = cfg['pull_secret']
    # Accept both key names from frontend
    ssh_key      = (cfg.get('ssh_pub_key') or cfg.get('ssh_public_key') or '').strip()
    machine_cidr = cfg.get('machine_cidr', '192.168.100.0/24')
    cluster_cidr = cfg.get('cluster_cidr', '10.128.0.0/14')
    service_cidr = cfg.get('service_cidr', '172.30.0.0/16')

    # Derive counts from nodes list if provided; otherwise fall back to explicit fields
    nodes    = cfg.get('nodes', [])
    cp_count = sum(1 for n in nodes if n.get('role', 'master') == 'master') or \
               int(cfg.get('control_plane_count', 3))
    wk_count = sum(1 for n in nodes if n.get('role') == 'worker') or \
               int(cfg.get('worker_count', 0))

    is_sno       = cp_count == 1 and wk_count == 0
    api_vip      = cfg.get('api_vip', '')
    ingress_vip  = cfg.get('ingress_vip', '')
    use_baremetal = bool(api_vip and ingress_vip) and not is_sno

    # Build line-by-line to avoid textwrap.dedent + multi-line substitution bugs
    lines = [
        'apiVersion: v1',
        f'baseDomain: {base_domain}',
        'metadata:',
        f'  name: {cluster_name}',
        'compute:',
        '- architecture: amd64',
        '  hyperthreading: Enabled',
        '  name: worker',
        f'  replicas: {wk_count}',
        'controlPlane:',
        '  architecture: amd64',
        '  hyperthreading: Enabled',
        '  name: master',
        f'  replicas: {cp_count}',
        'networking:',
        '  clusterNetwork:',
        f'  - cidr: {cluster_cidr}',
        '    hostPrefix: 23',
        '  machineNetwork:',
        f'  - cidr: {machine_cidr}',
        '  networkType: OVNKubernetes',
        '  serviceNetwork:',
        f'  - {service_cidr}',
    ]

    if use_baremetal:
        lines += [
            'platform:',
            '  baremetal:',
            '    apiVIPs:',
            f'    - {api_vip}',
            '    ingressVIPs:',
            f'    - {ingress_vip}',
        ]
    else:
        # platform: none works for SNO and compact clusters without L2 VIPs
        lines += ['platform:', '  none: {}']

    lines.append(f"pullSecret: '{pull_secret}'")
    if ssh_key:
        lines.append(f"sshKey: '{ssh_key}'")

    return '\n'.join(lines) + '\n'


def _has_nmstatectl() -> bool:
    return bool(shutil.which('nmstatectl'))


def _agent_config(cfg: dict) -> str:
    """Generate agent-config.yaml.

    Static NMState networkConfig blocks are only emitted when nmstatectl is
    present on the host — openshift-install validates them by calling it.
    Without nmstatectl the nodes will obtain IPs via DHCP.

    Uses PyYAML dump to ensure consistent indentation that Go yaml.v2 accepts.
    """
    import yaml as _yaml

    cluster_name  = cfg['cluster_name']
    nodes         = cfg.get('nodes', [])
    gateway       = cfg.get('gateway', '')
    # Accept both 'dns' and 'dns_servers' key names
    dns_raw       = cfg.get('dns') or cfg.get('dns_servers') or '8.8.8.8'
    dns           = str(dns_raw).split(',')[0].strip()
    prefix_len    = int(cfg.get('prefix_len', 24))
    use_static_ip = cfg.get('static_ip', True) and _has_nmstatectl()

    if not nodes:
        return ''

    rendezvous_ip = cfg.get('rendezvous_ip') or nodes[0]['ip']

    hosts = []
    for node in nodes:
        iface    = node.get('interface', 'enp1s0')
        mac      = node.get('mac', '')
        ip       = node['ip']
        hostname = node['hostname']
        role     = node.get('role', 'master')

        host_entry: dict = {
            'hostname': hostname,
            'role':     role,
            # interfaces block always required — provides MAC binding
            'interfaces': [
                ({'name': iface, 'macAddress': mac} if mac
                 else {'name': iface})
            ],
        }

        # Static IP via NMState — only when nmstatectl is installed
        if use_static_ip and ip and gateway:
            nc_iface: dict = {
                'name':  iface,
                'type':  'ethernet',
                'state': 'up',
                'ipv4': {
                    'enabled': True,
                    'dhcp':    False,
                    'address': [{'ip': ip, 'prefix-length': prefix_len}],
                },
            }
            if mac:
                nc_iface['mac-address'] = mac

            host_entry['networkConfig'] = {
                'interfaces': [nc_iface],
                'dns-resolver': {
                    'config': {'server': [dns]},
                },
                'routes': {
                    'config': [{
                        'destination':        '0.0.0.0/0',
                        'next-hop-address':   gateway,
                        'next-hop-interface': iface,
                        'table-id':           254,
                    }],
                },
            }

        hosts.append(host_entry)

    doc = {
        'apiVersion':   'v1alpha1',
        'kind':         'AgentConfig',
        'metadata':     {'name': cluster_name},
        'rendezvousIP': rendezvous_ip,
        'hosts':        hosts,
    }

    # Use default_flow_style=False + explicit indent for clean block YAML
    return _yaml.dump(doc, default_flow_style=False, allow_unicode=True, sort_keys=False)

# ── Deployment pipeline ───────────────────────────────────────────────────────

def _run_agent_deploy(job_id: str, cfg: dict):
    """Full agent-based deployment pipeline — runs in a daemon thread."""
    _running_jobs.add(job_id)

    def log(msg, level='info'):
        _job_log(job_id, msg, level)

    def phase(name, pct):
        current = _jobs.get(job_id, {}).get('progress', 0)
        _job_set(job_id, phase=name, progress=max(current, pct))
        log(f'── {name} ──')

    def fail(msg):
        _job_set(job_id, status='failed', phase='Failed')
        log(msg, 'error')
        _running_jobs.discard(job_id)

    try:
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        cluster_name  = cfg['cluster_name']
        ocp_version   = cfg.get('ocp_version', '')
        is_sno        = cfg.get('deployment_type', 'sno') == 'sno'
        n_control     = 1 if is_sno else int(cfg.get('control_plane_count', 3))
        n_workers     = 0 if is_sno else int(cfg.get('worker_count', 2))
        total_nodes   = n_control + n_workers
        storage_path  = Path(cfg.get('storage_path', '/var/lib/libvirt/images'))
        network       = cfg.get('libvirt_network', 'default')

        job_dir = WORK_DIR / job_id
        install_dir = job_dir / 'install'
        job_dir.mkdir(exist_ok=True)
        install_dir.mkdir(exist_ok=True)

        # ── Resume state ──────────────────────────────────────────────────────
        saved      = _jobs.get(job_id, {})
        saved_vms  = saved.get('vms', [])
        saved_iso  = saved.get('iso_path')
        is_resume  = saved.get('progress', 0) > 0

        if is_resume:
            log('── Resuming interrupted deployment ──', 'warn')
            log(f'  vms     : {saved_vms or "not yet created"}')
            log(f'  iso     : {saved_iso or "not yet generated"}')

        # ── Build node list ───────────────────────────────────────────────────
        nodes_cfg = cfg.get('nodes', [])
        if not nodes_cfg:
            # Auto-build nodes from IP range
            import ipaddress
            net_cfg   = cfg.get('machine_cidr', '192.168.100.0/24')
            ip_start  = cfg.get('ip_start', '')
            if ip_start:
                base_ip = ipaddress.ip_address(ip_start)
            else:
                net     = ipaddress.ip_network(net_cfg, strict=False)
                base_ip = list(net.hosts())[10]  # .11 by default

            node_names = []
            for i in range(n_control):
                node_names.append(f'{cluster_name}-master-{i}')
            for i in range(n_workers):
                node_names.append(f'{cluster_name}-worker-{i}')

            nodes_cfg = []
            for idx, nm in enumerate(node_names):
                role = 'master' if idx < n_control else 'worker'
                nodes_cfg.append({
                    'hostname':  nm,
                    'ip':        str(base_ip + idx),
                    'mac':       _make_mac(job_id, idx),
                    'role':      role,
                    'interface': 'enp1s0',
                })
            cfg = {**cfg, 'nodes': nodes_cfg}
            _job_set(job_id, nodes=nodes_cfg)

        # Normalize node keys: frontend may send 'name'/'iface', backend uses 'hostname'/'interface'
        for idx, n in enumerate(nodes_cfg):
            if 'name' in n and 'hostname' not in n:
                n['hostname'] = n['name']
            if 'iface' in n and 'interface' not in n:
                n['interface'] = n['iface']
            if 'hostname' not in n:
                n['hostname'] = n.get('name', f'node-{idx}')
            if 'interface' not in n:
                n['interface'] = n.get('iface', 'enp1s0')
            # Auto-assign MAC if not provided — deterministic from job_id + node index
            if not n.get('mac'):
                n['mac'] = _make_mac(job_id, idx)
                log(f'  Auto-assigned MAC for {n["hostname"]}: {n["mac"]}')

        vm_names = [n['hostname'] for n in nodes_cfg]

        # ── Step 1: Find or download openshift-install ────────────────────────
        phase('Locating openshift-install binary', 5)
        _task_start(job_id, 'binary')
        binary = _find_oi_binary(ocp_version)
        if not binary:
            if ocp_version:
                log(f'  openshift-install {ocp_version} not found — downloading…')
                binary = _download_oi_binary(ocp_version, log)
            if not binary:
                _task_fail(job_id, 'binary', 'Binary not found and download failed')
                fail('openshift-install binary not found. Place it in '
                     f'{WORK_DIR}/bin/{ocp_version or ""}/openshift-install '
                     'or ensure it is in PATH.')
                return
        ver_str = _oi_version(binary)
        log(f'  Using binary: {binary} ({ver_str})')
        _job_set(job_id, binary=str(binary))
        _task_done(job_id, 'binary', f'{binary.name} {ver_str}')

        # Build a PATH that includes the binary's directory so that
        # openshift-install can find `oc` (required for ISO creation)
        bin_dir    = binary.parent
        extra_path = str(bin_dir)
        oc_in_path = shutil.which('oc') or shutil.which('kubectl')
        oc_local   = bin_dir / 'oc'
        if not oc_in_path and not oc_local.exists() and ocp_version:
            log('  oc not found — downloading alongside openshift-install…')
            _ensure_oc(ocp_version, bin_dir, log)
        elif oc_local.exists():
            log(f'  oc found at {oc_local} ✓')
        elif oc_in_path:
            log(f'  oc found at {oc_in_path} ✓')

        # Subprocess environment with bin_dir prepended to PATH
        sub_env = {**os.environ, 'PATH': f'{extra_path}:{os.environ.get("PATH", "/usr/bin:/bin")}'}

        # ── Step 2: Generate config files ─────────────────────────────────────
        # Note: openshift-install CONSUMES install-config.yaml + agent-config.yaml during
        # ISO creation, so they won't exist after a successful `agent create image`.
        # We only need to write them if the ISO hasn't been built yet.
        # State for wait-for commands is in .openshift_install_state.json (created by ISO build).
        phase('Generating install-config.yaml', 10)
        _task_start(job_id, 'config')
        state_json = install_dir / '.openshift_install_state.json'
        if not saved_iso or not state_json.exists():
            ic_path = install_dir / 'install-config.yaml'
            ac_path = install_dir / 'agent-config.yaml'
            ic_path.write_text(_install_config(cfg))
            ac_path.write_text(_agent_config(cfg))
            log('  install-config.yaml written ✓')
            log('  agent-config.yaml written ✓')
            if _has_nmstatectl():
                log('  nmstatectl found — static IP (NMState) config included ✓')
            else:
                log('  nmstatectl not found — networkConfig omitted, nodes will use DHCP', 'warn')
                log('  To enable static IPs: sudo apt install nmstate  OR  sudo dnf install nmstate', 'warn')
            # If there's no state.json, the ISO must be rebuilt even if the file exists
            if saved_iso and not state_json.exists():
                log('  Install state missing — ISO must be rebuilt', 'warn')
                _job_set(job_id, iso_path=None)
                saved_iso = None
            _task_done(job_id, 'config', f'{len(nodes_cfg)} node(s) configured')
        else:
            log('  Resuming: config files already processed ✓')
            _task_skip(job_id, 'config', 'Resumed — already done')

        # ── Step 3: Build agent ISO ───────────────────────────────────────────
        iso_path = Path(saved_iso) if saved_iso and Path(saved_iso).exists() else None

        # If saved ISO exists but is NOT in a QEMU/AppArmor-accessible path
        # (e.g. it's under /home/), relocate it to /var/lib/libvirt/images/.
        if iso_path and not str(iso_path).startswith('/var/lib/libvirt/'):
            libvirt_iso = Path('/var/lib/libvirt/images') / f'ocp-agent-{job_id}.iso'
            if libvirt_iso.exists():
                log(f'  Existing libvirt ISO found: {libvirt_iso} ✓')
                iso_path = libvirt_iso
                _job_set(job_id, iso_path=str(iso_path))
            else:
                try:
                    log(f'  Relocating ISO to QEMU-accessible path: {libvirt_iso}…')
                    shutil.copy2(str(iso_path), str(libvirt_iso))
                    libvirt_iso.chmod(0o644)
                    iso_path = libvirt_iso
                    _job_set(job_id, iso_path=str(iso_path))
                    log('  ISO relocated ✓')
                except Exception as relocate_err:
                    log(f'  Warning: could not relocate ISO: {relocate_err}', 'warn')

        if not iso_path:
            phase('Building agent ISO', 15)
            _task_start(job_id, 'iso', 'Running openshift-install agent create image…')
            log('  Running: openshift-install agent create image…')
            log('  (This may take 2–5 minutes)')
            result = subprocess.run(
                [str(binary), 'agent', 'create', 'image', '--dir', str(install_dir)],
                capture_output=True, text=True, timeout=600,
                env=sub_env,
            )
            if result.returncode != 0:
                _task_fail(job_id, 'iso', 'ISO creation failed')
                fail(f'ISO creation failed:\n{result.stderr[-500:]}')
                return

            # The ISO is created as agent.x86_64.iso in the install dir
            iso_candidates = list(install_dir.glob('agent*.iso'))
            if not iso_candidates:
                fail('ISO file not found after openshift-install agent create image')
                return
            iso_path = iso_candidates[0]
            log(f'  ISO created: {iso_path.name} ({iso_path.stat().st_size // 1_048_576} MB) ✓')

            # AppArmor restricts QEMU (libvirt-qemu profile) from accessing /home/ paths.
            # Copy the ISO to /var/lib/libvirt/images/ which is explicitly allowed.
            libvirt_iso = Path('/var/lib/libvirt/images') / f'ocp-agent-{job_id}.iso'
            try:
                log(f'  Copying ISO to {libvirt_iso} (AppArmor-accessible path)…')
                shutil.copy2(str(iso_path), str(libvirt_iso))
                libvirt_iso.chmod(0o644)
                iso_path = libvirt_iso
                log(f'  ISO available at {libvirt_iso} ✓')
            except Exception as copy_err:
                log(f'  Warning: could not copy ISO to {libvirt_iso}: {copy_err}', 'warn')
                log('  Falling back to permission approach (may fail with AppArmor)', 'warn')
                try:
                    iso_path.chmod(iso_path.stat().st_mode | 0o004)
                    p = iso_path.parent
                    while p != WORK_DIR.parent:
                        current = p.stat().st_mode
                        p.chmod(current | 0o001)
                        p = p.parent
                    log('  Set QEMU-accessible permissions on ISO ✓')
                except Exception as perm_err:
                    log(f'  Warning: could not set ISO permissions: {perm_err}', 'warn')

            iso_mb = iso_path.stat().st_size // 1_048_576
            _job_set(job_id, iso_path=str(iso_path))
            _task_done(job_id, 'iso', f'{iso_path.name} ({iso_mb} MB)')
        else:
            _task_skip(job_id, 'iso', f'Resumed — ISO already at {iso_path}')

        # ── Step 4: Create VMs ────────────────────────────────────────────────
        phase('Creating virtual machines', 30)
        _task_start(job_id, 'vms', f'Creating {len(nodes_cfg)} VM(s)…')
        if not saved_vms:
            # Resolve network name → actual bridge and validate it exists
            bridge            = _resolve_bridge(network)
            available_bridges = _list_host_bridges()
            if bridge not in available_bridges:
                fail(
                    f'Bridge/network "{network}" not found on this host.\n'
                    f'Available bridges: {", ".join(available_bridges) or "none"}\n'
                    f'Fix the network name in your deployment config and reset.'
                )
                return
            log(f'  Using bridge: {bridge} ✓')

            created_vms = []
            conn = libvirt.open('qemu:///system')
            try:
                for idx, node in enumerate(nodes_cfg):
                    nm      = node['hostname']
                    role    = node.get('role', 'master')
                    is_cp   = role == 'master'
                    # Prefer per-node values, fall back to cluster-level config
                    vcpus   = int(node.get('vcpu') or node.get('vcpus') or
                                  (cfg.get('cp_vcpus', 8) if is_cp else cfg.get('w_vcpus', 4)))
                    # ram_mb in node takes priority; else use ram_gb cluster config
                    ram_mb_node = node.get('ram_mb')
                    if ram_mb_node:
                        ram_gb = int(ram_mb_node) // 1024
                    else:
                        ram_gb = int(cfg.get('cp_ram_gb', 16) if is_cp else cfg.get('w_ram_gb', 8))
                    disk_gb = int(node.get('disk_gb') or
                                  (cfg.get('cp_disk_gb', 120) if is_cp else cfg.get('w_disk_gb', 100)))
                    mac     = node['mac']

                    # Check if already defined
                    try:
                        conn.lookupByName(nm)
                        log(f'  {nm} already defined ✓')
                        created_vms.append(nm)
                        continue
                    except libvirt.libvirtError:
                        pass

                    disk_path = storage_path / f'{nm}.qcow2'
                    if not disk_path.exists():
                        log(f'  Creating {disk_gb}GB disk for {nm}…')
                        _create_disk(disk_path, disk_gb)

                    log(f'  Creating VM {nm} ({vcpus} vCPU, {ram_gb}GB RAM)…')
                    try:
                        conn.close()
                    except Exception:
                        pass
                    _create_vm(nm, vcpus, ram_gb * 1024, disk_path, iso_path, bridge, mac)
                    conn = libvirt.open('qemu:///system')
                    log(f'  VM {nm} started ✓')
                    created_vms.append(nm)

            except Exception as e:
                try:
                    conn.close()
                except Exception:
                    pass
                _task_fail(job_id, 'vms', str(e))
                fail(f'VM creation failed: {e}')
                return

            try:
                conn.close()
            except Exception:
                pass
            _job_set(job_id, vms=created_vms)
            _task_done(job_id, 'vms', f'{len(created_vms)} VM(s) running')
        else:
            log(f'  Resuming: {len(saved_vms)} VMs already exist ✓')
            _task_skip(job_id, 'vms', f'Resumed — {len(saved_vms)} VMs already exist')

        # ── Step 5: Wait for bootstrap ────────────────────────────────────────
        if job_id in _stop_jobs:
            _stop_jobs.discard(job_id)
            log('Deployment stopped by reset request.', 'warn')
            return

        phase('Waiting for bootstrap complete', 45)
        _task_start(job_id, 'bootstrap', 'Nodes booting from ISO — takes 10–30 min')
        log('  Running: openshift-install agent wait-for bootstrap-complete')
        log('  (Nodes are booting from ISO — takes 10–30 min)')

        bootstrap_proc = subprocess.Popen(
            [str(binary), 'agent', 'wait-for', 'bootstrap-complete',
             '--dir', str(install_dir), '--log-level', 'info'],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=sub_env,
        )
        _job_set(job_id, bootstrap_pid=bootstrap_proc.pid)

        for line in bootstrap_proc.stdout:
            if job_id in _stop_jobs:
                bootstrap_proc.kill()
                _stop_jobs.discard(job_id)
                log('Deployment stopped by reset request.', 'warn')
                return
            line = line.rstrip()
            if line:
                # Parse progress hints from openshift-install output
                if 'Bootstrap status' in line or 'bootstrapComplete' in line:
                    phase('Bootstrap in progress', 55)
                elif 'Bootstrap Complete' in line or 'bootstrap complete' in line.lower():
                    phase('Bootstrap complete', 65)
                log(f'  {line}')

        bootstrap_proc.wait()
        if bootstrap_proc.returncode != 0:
            _task_fail(job_id, 'bootstrap', f'Bootstrap failed (exit {bootstrap_proc.returncode})')
            fail(f'Bootstrap failed (exit {bootstrap_proc.returncode})')
            return
        log('Bootstrap complete ✓')
        _task_done(job_id, 'bootstrap', 'Bootstrap complete')

        # ── Step 6: Wait for install complete ────────────────────────────────
        if job_id in _stop_jobs:
            _stop_jobs.discard(job_id)
            log('Deployment stopped by reset request.', 'warn')
            return

        phase('Installing OpenShift', 70)
        _task_start(job_id, 'install', 'Installing OpenShift cluster — takes 30–90 min')
        log('  Running: openshift-install agent wait-for install-complete')
        log('  (Takes 30–90 minutes)')

        install_proc = subprocess.Popen(
            [str(binary), 'agent', 'wait-for', 'install-complete',
             '--dir', str(install_dir), '--log-level', 'info'],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=sub_env,
        )
        _job_set(job_id, install_pid=install_proc.pid)

        for line in install_proc.stdout:
            if job_id in _stop_jobs:
                install_proc.kill()
                _stop_jobs.discard(job_id)
                log('Deployment stopped by reset request.', 'warn')
                return
            line = line.rstrip()
            if line:
                if 'Install complete' in line or 'install complete' in line.lower():
                    phase('Finalizing', 90)
                elif 'Cluster is installed' in line:
                    phase('Cluster ready', 95)
                log(f'  {line}')

        install_proc.wait()
        if install_proc.returncode != 0:
            _task_fail(job_id, 'install', f'Installation failed (exit {install_proc.returncode})')
            fail(f'Installation failed (exit {install_proc.returncode})')
            return
        _task_done(job_id, 'install', 'OpenShift installed successfully')

        # ── Step 7: Collect credentials ───────────────────────────────────────
        phase('Collecting credentials', 97)
        _task_start(job_id, 'creds', 'Reading kubeconfig and kubeadmin-password…')
        kc_src  = install_dir / 'auth' / 'kubeconfig'
        pwd_src = install_dir / 'auth' / 'kubeadmin-password'
        result  = {}

        if kc_src.exists():
            kc_dest = job_dir / 'kubeconfig'
            kc_dest.write_text(kc_src.read_text())
            result['kubeconfig_path'] = str(kc_dest)
            log(f'kubeconfig saved → {kc_dest} ✓')
        else:
            log('  kubeconfig not found after install', 'warn')

        if pwd_src.exists():
            result['kubeadmin_password'] = pwd_src.read_text().strip()
            log('kubeadmin-password collected ✓')

        base_domain  = cfg.get('base_domain', '')
        result['api_url']     = f'https://api.{cluster_name}.{base_domain}:6443'
        result['console_url'] = f'https://console-openshift-console.apps.{cluster_name}.{base_domain}'

        _task_done(job_id, 'creds', result.get('console_url', ''))
        _job_set(job_id, result=result, status='complete', progress=100, phase='Complete')
        log(f'OpenShift installation complete! 🎉')
        log(f'Console: {result["console_url"]}')
        log(f'API:     {result["api_url"]}')

    except Exception as e:
        import traceback
        _job_set(job_id, status='failed', phase='Failed')
        _job_log(job_id, f'Unhandled error: {e}', 'error')
        _job_log(job_id, traceback.format_exc(), 'error')
    finally:
        _running_jobs.discard(job_id)


def _resume_pending_jobs():
    """Re-spawn threads for jobs that were pending when the service last restarted."""
    for job_id, job in list(_jobs.items()):
        if job.get('status') != 'pending':
            continue
        if job_id in _running_jobs:
            continue
        cfg = job.get('config', {})
        if not cfg:
            continue
        _running_jobs.add(job_id)
        t = threading.Thread(target=_run_agent_deploy, args=(job_id, cfg),
                             daemon=True, name=f'ocp-agent-resume-{job_id}')
        t.start()

_resume_pending_jobs()

# ── kubectl helper ─────────────────────────────────────────────────────────────

def _run_kubectl(kubeconfig: Path, args: list, timeout: int = 15) -> dict:
    kubectl = shutil.which('kubectl') or shutil.which('oc')
    if not kubectl:
        return {'ok': False, 'error': 'kubectl / oc not found in PATH'}
    try:
        result = subprocess.run(
            [kubectl, f'--kubeconfig={kubeconfig}'] + args,
            capture_output=True, text=True, timeout=timeout,
            env={**os.environ, 'KUBECONFIG': str(kubeconfig)},
        )
        if result.returncode != 0:
            return {'ok': False, 'error': result.stderr.strip()[:500]}
        return {'ok': True, 'data': json.loads(result.stdout)}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': f'kubectl timed out after {timeout}s'}
    except Exception as e:
        return {'ok': False, 'error': str(e)}

# ── Credentials helpers ───────────────────────────────────────────────────────

def _load_creds() -> dict:
    try:
        if _CREDS_FILE.exists():
            return json.loads(_CREDS_FILE.read_text())
    except Exception:
        pass
    return {}

def _save_creds(creds: dict):
    try:
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        _CREDS_FILE.write_text(json.dumps(creds))
        _CREDS_FILE.chmod(0o600)   # owner-read only — pull secret is sensitive
    except Exception:
        pass

# ── API routes ─────────────────────────────────────────────────────────────────

@agent_bp.route('/api/ocp-agent/credentials', methods=['GET'])
def get_credentials():
    err = _auth()
    if err:
        return err
    creds = _load_creds()
    if not creds:
        return jsonify({'saved': False})
    ps = creds.get('pull_secret', '')
    # Return a masked hint so the UI can show something without exposing the secret
    ps_hint = ''
    if ps:
        try:
            import base64 as _b64
            auths = json.loads(ps).get('auths', {})
            registries = list(auths.keys())
            ps_hint = f"{len(registries)} registr{'y' if len(registries)==1 else 'ies'}"
        except Exception:
            ps_hint = f'{len(ps)} chars'
    return jsonify({
        'saved':          True,
        'pull_secret':    creds.get('pull_secret', ''),
        'ssh_public_key': creds.get('ssh_public_key', ''),
        'ps_hint':        ps_hint,
    })


@agent_bp.route('/api/ocp-agent/credentials', methods=['POST'])
def save_credentials():
    err = _auth()
    if err:
        return err
    data = request.get_json() or {}
    existing = _load_creds()
    updated = {**existing}
    if 'pull_secret' in data:
        updated['pull_secret'] = data['pull_secret'].strip()
    if 'ssh_public_key' in data:
        updated['ssh_public_key'] = data['ssh_public_key'].strip()
    _save_creds(updated)
    return jsonify({'saved': True})


@agent_bp.route('/api/ocp-agent/credentials', methods=['DELETE'])
def delete_credentials():
    err = _auth()
    if err:
        return err
    try:
        _CREDS_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    return jsonify({'deleted': True})


@agent_bp.route('/api/ocp-agent/preflight')
def preflight():
    """Check if openshift-install binary is available."""
    err = _auth()
    if err:
        return err

    version = request.args.get('version', '')
    binary  = _find_oi_binary(version or None)
    checks  = []

    # openshift-install
    if binary:
        ver = _oi_version(binary)
        checks.append({'name': 'openshift-install', 'ok': True,
                        'detail': f'{binary} ({ver})'})
    else:
        checks.append({'name': 'openshift-install', 'ok': False,
                        'detail': 'Not found. Will be downloaded automatically if version is specified.'})

    # libvirt
    try:
        conn = libvirt.open('qemu:///system')
        conn.close()
        checks.append({'name': 'libvirt', 'ok': True, 'detail': 'Connected'})
    except Exception as e:
        checks.append({'name': 'libvirt', 'ok': False, 'detail': str(e)})

    # qemu-img
    qimg = shutil.which('qemu-img')
    checks.append({'name': 'qemu-img', 'ok': bool(qimg),
                   'detail': qimg or 'Not found'})

    # oc client — required by openshift-install agent create image
    oc = shutil.which('oc') or shutil.which('kubectl')
    # also check versioned bin dir
    if not oc and version:
        oc_local = WORK_DIR / 'bin' / version / 'oc'
        if oc_local.exists():
            oc = str(oc_local)
    checks.append({
        'name':   'oc (openshift client)',
        'ok':     bool(oc),
        'detail': oc if oc else (
            'Not found — will be downloaded automatically alongside openshift-install'
        ),
    })

    # nmstatectl — needed for static IP (NMState) config in agent ISO
    nmstate = shutil.which('nmstatectl')
    checks.append({
        'name':   'nmstatectl',
        'ok':     bool(nmstate),
        'detail': nmstate if nmstate else (
            'Not found — static IPs will be skipped (nodes use DHCP). '
            'Install with: sudo apt install nmstate  OR  sudo dnf install nmstate'
        ),
    })

    # Storage path
    storage_path = request.args.get('storage_path', '/var/lib/libvirt/images')
    sp = Path(storage_path)
    checks.append({'name': 'storage', 'ok': sp.exists(),
                   'detail': str(sp)})

    # Available binaries
    available_binaries = []
    bin_dir = WORK_DIR / 'bin'
    if bin_dir.exists():
        for d in sorted(bin_dir.iterdir()):
            b = d / 'openshift-install'
            if b.exists():
                available_binaries.append({'version': d.name, 'path': str(b)})
    ub = bin_dir / 'openshift-install'
    if ub.exists():
        available_binaries.append({'version': 'unversioned', 'path': str(ub)})

    return jsonify({'checks': checks, 'available_binaries': available_binaries})


@agent_bp.route('/api/ocp-agent/versions')
def list_versions():
    """Return available OCP versions from mirror.openshift.com (GA releases only)."""
    err = _auth()
    if err:
        return err

    try:
        # Fetch the directory listing from the OCP mirror
        r = requests.get(
            'https://mirror.openshift.com/pub/openshift-v4/clients/ocp/',
            timeout=10,
        )
        r.raise_for_status()

        import re
        # Extract version directories like 4.16.3, 4.21.0, etc.
        versions = re.findall(r'href="(4\.\d+\.\d+)/"', r.text)

        # Deduplicate, sort descending, keep only GA (no -rc, -fc, -ec suffixes)
        seen_minors: dict = {}
        for v in sorted(set(versions), key=lambda x: list(map(int, x.split('.'))), reverse=True):
            minor = '.'.join(v.split('.')[:2])   # e.g. "4.21"
            seen_minors.setdefault(minor, []).append(v)

        # Return up to 3 patch releases per minor, latest minors first
        result = []
        for minor in sorted(seen_minors.keys(),
                            key=lambda x: list(map(int, x.split('.'))), reverse=True):
            result.extend(seen_minors[minor][:3])

        return jsonify({'versions': result[:60]})  # cap at 60 entries

    except Exception as e:
        # Fallback list when mirror is unreachable
        fallback = [
            '4.21.0', '4.20.0', '4.19.0', '4.18.0',
            '4.17.0', '4.16.0', '4.15.0', '4.14.0',
            '4.13.0', '4.12.0',
        ]
        return jsonify({'versions': fallback, 'warning': str(e)})


@agent_bp.route('/api/ocp-agent/networks')
def list_networks():
    """List available libvirt bridge networks."""
    err = _auth()
    if err:
        return err
    try:
        import xml.etree.ElementTree as ET
        conn    = libvirt.open('qemu:///system')
        bridges = []
        for net in conn.listAllNetworks():
            xml  = ET.fromstring(net.XMLDesc())
            br   = xml.findtext('bridge/@name') or ''
            fwd  = xml.findtext('forward/@mode') or 'none'
            bridges.append({'name': net.name(), 'bridge': br,
                            'active': bool(net.isActive()), 'forward': fwd})
        conn.close()
        # Also include host bridges (not managed by libvirt)
        import subprocess as _sp
        r = _sp.run(['ip', '-j', 'link', 'show', 'type', 'bridge'],
                    capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            for iface in json.loads(r.stdout or '[]'):
                nm = iface.get('ifname', '')
                if nm and not any(b['bridge'] == nm for b in bridges):
                    bridges.append({'name': nm, 'bridge': nm,
                                    'active': True, 'forward': 'bridge'})
        return jsonify({'networks': bridges})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@agent_bp.route('/api/ocp-agent/deploy', methods=['POST'])
def deploy():
    err = _auth()
    if err:
        return err

    data = request.get_json() or {}

    required = ['cluster_name', 'base_domain', 'pull_secret', 'ocp_version']
    missing  = [f for f in required if not data.get(f, '').strip()]
    if missing:
        return jsonify({'error': f'Missing required fields: {", ".join(missing)}'}), 400

    job_id  = uuid.uuid4().hex[:8]
    created = time.time()

    with _lock:
        _jobs[job_id] = {
            'id':       job_id,
            'status':   'pending',
            'phase':    'Queued',
            'progress': 0,
            'result':   None,
            'created':  created,
            'config':   data,
            'logs':     [],
            'tasks':    _fresh_tasks(),
        }
        _save_jobs()

    _running_jobs.add(job_id)
    threading.Thread(target=_run_agent_deploy, args=(job_id, data),
                     daemon=True, name=f'ocp-agent-{job_id}').start()

    return jsonify({'job_id': job_id}), 201


@agent_bp.route('/api/ocp-agent/jobs')
def list_jobs():
    err = _auth()
    if err:
        return err
    jobs = []
    with _lock:
        for jid, j in _jobs.items():
            jobs.append({
                'id':       jid,
                'status':   j.get('status'),
                'phase':    j.get('phase'),
                'progress': j.get('progress', 0),
                'created':  j.get('created'),
                'cluster_name': j.get('config', {}).get('cluster_name'),
                'ocp_version':  j.get('config', {}).get('ocp_version'),
                'deployment_type': j.get('config', {}).get('deployment_type', 'sno'),
            })
    return jsonify({'jobs': sorted(jobs, key=lambda x: x['created'] or 0, reverse=True)})


@agent_bp.route('/api/ocp-agent/jobs/<job_id>')
def job_detail(job_id):
    err = _auth()
    if err:
        return err
    job = _jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify(job)


@agent_bp.route('/api/ocp-agent/jobs/<job_id>', methods=['DELETE'])
def delete_job(job_id):
    err = _auth()
    if err:
        return err

    if job_id in _running_jobs:
        _stop_jobs.add(job_id)
        _running_jobs.discard(job_id)
        time.sleep(0.5)

    # Clean up the ISO copy in /var/lib/libvirt/images/ if present
    libvirt_iso = Path('/var/lib/libvirt/images') / f'ocp-agent-{job_id}.iso'
    try:
        libvirt_iso.unlink(missing_ok=True)
    except Exception:
        pass

    with _lock:
        _jobs.pop(job_id, None)
        _save_jobs()

    return jsonify({'deleted': job_id})


@agent_bp.route('/api/ocp-agent/jobs/<job_id>/kubeconfig')
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
    return send_file(str(kc_path), as_attachment=True,
                     download_name='kubeconfig', mimetype='text/plain')


@agent_bp.route('/api/ocp-agent/jobs/<job_id>/cluster')
def cluster_status(job_id):
    """Live cluster status via kubectl."""
    err = _auth()
    if err:
        return err

    job = _jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    kc_path = WORK_DIR / job_id / 'kubeconfig'
    if not kc_path.exists():
        alt = Path(job.get('result', {}).get('kubeconfig_path', ''))
        if alt.exists():
            kc_path = alt
        else:
            return jsonify({'error': 'kubeconfig not available'}), 404

    payload: dict = {'nodes': [], 'operators': [], 'version': None, 'errors': []}

    r = _run_kubectl(kc_path, ['get', 'nodes', '-o', 'json'], 15)
    if r['ok']:
        for item in r['data'].get('items', []):
            labels = item.get('metadata', {}).get('labels', {})
            roles  = sorted([k.split('/')[-1] for k in labels
                             if k.startswith('node-role.kubernetes.io/')]) or ['worker']
            ready = 'Unknown'
            for cond in item.get('status', {}).get('conditions', []):
                if cond.get('type') == 'Ready':
                    ready = 'Ready' if cond.get('status') == 'True' else 'NotReady'
                    break
            payload['nodes'].append({
                'name': item['metadata']['name'], 'roles': roles, 'ready': ready,
                'kubelet_version': item.get('status', {}).get('nodeInfo', {}).get('kubeletVersion', ''),
            })
    else:
        payload['errors'].append(f'nodes: {r["error"]}')

    r = _run_kubectl(kc_path, ['get', 'clusteroperators', '-o', 'json'], 20)
    if r['ok']:
        for item in r['data'].get('items', []):
            conds = {c['type']: c for c in item.get('status', {}).get('conditions', [])}
            payload['operators'].append({
                'name':        item['metadata']['name'],
                'available':   conds.get('Available',   {}).get('status', 'Unknown'),
                'progressing': conds.get('Progressing', {}).get('status', 'Unknown'),
                'degraded':    conds.get('Degraded',    {}).get('status', 'Unknown'),
                'message':     (conds.get('Degraded') or conds.get('Progressing') or {}).get('message', ''),
            })
    else:
        payload['errors'].append(f'clusteroperators: {r["error"]}')

    r = _run_kubectl(kc_path, ['get', 'clusterversion', 'version', '-o', 'json'], 15)
    if r['ok']:
        data    = r['data']
        history = data.get('status', {}).get('history', [])
        current = next((h for h in history if h.get('state') == 'Completed'), history[0] if history else {})
        payload['version'] = {
            'version': current.get('version', ''),
            'channel': data.get('spec', {}).get('channel', ''),
        }
    else:
        payload['errors'].append(f'clusterversion: {r["error"]}')

    return jsonify(payload)


@agent_bp.route('/api/ocp-agent/jobs/<job_id>/reset', methods=['POST'])
def reset_job(job_id):
    """Reset or reinstall: destroy VMs, clear state, start fresh."""
    err = _auth()
    if err:
        return err

    job = _jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    def _log(msg, level='info'):
        _job_log(job_id, msg, level)

    # Signal running thread to stop
    if job_id in _running_jobs:
        _stop_jobs.add(job_id)
        _log('── Reset: signalling active thread to stop ──', 'warn')
        time.sleep(1)
        _running_jobs.discard(job_id)

    vm_names     = job.get('vms', [])
    # Also include node hostnames from config in case VMs were created but not tracked in job.vms
    nodes_cfg    = job.get('config', {}).get('nodes', []) or job.get('nodes', [])
    for n in nodes_cfg:
        nm = n.get('hostname') or n.get('name', '')
        if nm and nm not in vm_names:
            vm_names.append(nm)
    storage_path = Path(job.get('config', {}).get('storage_path', '/var/lib/libvirt/images'))
    destroyed    = []

    # Check whether a valid ISO already exists — reuse it to skip the 2-5 min build step
    saved_iso    = job.get('iso_path')
    iso_exists   = bool(saved_iso and Path(saved_iso).exists())

    _log('── Resetting agent-based deployment ──', 'warn')
    if iso_exists:
        _log(f'  Existing ISO found — will reuse (skips rebuild) ✓', 'info')
    else:
        _log('  No existing ISO — will rebuild from scratch')

    # Destroy VMs
    if vm_names:
        try:
            conn = libvirt.open('qemu:///system')
            for nm in vm_names:
                try:
                    dom = conn.lookupByName(nm)
                    if dom.isActive():
                        dom.destroy()
                    dom.undefine()
                    destroyed.append(nm)
                    _log(f'  Destroyed VM {nm} ✓')
                except libvirt.libvirtError:
                    pass
            conn.close()
        except Exception as e:
            _log(f'  libvirt warning: {e}', 'warn')

        for nm in vm_names:
            for disk in storage_path.glob(f'{nm}*.qcow2'):
                try:
                    disk.unlink()
                    _log(f'  Removed disk {disk.name} ✓')
                except OSError:
                    pass

    # If we're NOT preserving the ISO, also remove the libvirt copy so it
    # doesn't consume disk and a fresh copy is made on the next build.
    if not iso_exists:
        libvirt_iso = Path('/var/lib/libvirt/images') / f'ocp-agent-{job_id}.iso'
        try:
            libvirt_iso.unlink(missing_ok=True)
        except Exception:
            pass

    # Clear the install dir state (ignition assets, auth/, etc.) but KEEP the ISO.
    # The deploy pipeline checks saved_iso before deciding whether to rebuild.
    install_dir = WORK_DIR / job_id / 'install'
    if install_dir.exists() and not iso_exists:
        # Full wipe only when there's no ISO to preserve
        shutil.rmtree(install_dir, ignore_errors=True)
        install_dir.mkdir()
        _log('  Cleared install directory ✓')
    elif install_dir.exists() and iso_exists:
        # Preserve the ISO and the openshift-install state file (needed by wait-for commands).
        # Remove everything else (ignition files, auth/, logs)
        iso_file    = Path(saved_iso)
        state_json  = install_dir / '.openshift_install_state.json'
        keep_paths  = {iso_file.resolve(), state_json.resolve()}
        for item in install_dir.iterdir():
            if item.resolve() not in keep_paths:
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink(missing_ok=True)
        _log('  Cleared install state (ISO + state.json preserved) ✓')

    now_ts = time.strftime('%H:%M:%S')
    _job_set(job_id,
        status='pending', phase='Queued for reset', progress=0,
        vms=[], result=None,
        # Keep iso_path so the deploy pipeline skips the rebuild
        iso_path=saved_iso if iso_exists else None,
        logs=[{'ts': now_ts, 'msg': '── Reset triggered ──', 'level': 'warn'},
              {'ts': now_ts,
               'msg': f'  ISO {"reused from previous build" if iso_exists else "will be rebuilt"}',
               'level': 'info'}],
    )

    cfg = job.get('config', {})
    _running_jobs.add(job_id)
    threading.Thread(target=_run_agent_deploy, args=(job_id, cfg),
                     daemon=True, name=f'ocp-agent-reset-{job_id}').start()

    return jsonify({'action': 'reinstall', 'destroyed_vms': destroyed})
