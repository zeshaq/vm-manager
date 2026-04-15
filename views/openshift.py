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
WORK_DIR      = Path.home() / 'hypercloud' / 'openshift'
_JOBS_FILE    = WORK_DIR / 'jobs.json'
ISO_CACHE_DIR = WORK_DIR / 'iso-cache'
_ISO_CACHE_FILE = ISO_CACHE_DIR / 'cache.json'

_iso_cache: dict = {}   # fingerprint → {infra_env_id, iso_path, ocp_version, downloaded_at}
_iso_lock = threading.Lock()


def _iso_fingerprint(ocp_version: str, pull_secret: str, ssh_public_key: str) -> str:
    """Stable key for a DHCP (version, pull_secret, ssh_key) combination.
    Static IP deployments must never use the cache — their MAC/IP config
    is deployment-specific and embedded in the infra-env/ISO."""
    raw = f'dhcp|{ocp_version}|{pull_secret.strip()}|{ssh_public_key.strip()}'
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _load_iso_cache():
    global _iso_cache
    try:
        ISO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        if _ISO_CACHE_FILE.exists():
            with open(_ISO_CACHE_FILE) as f:
                _iso_cache = json.load(f)
    except Exception:
        _iso_cache = {}


def _save_iso_cache():
    try:
        ISO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(_ISO_CACHE_FILE, 'w') as f:
            json.dump(_iso_cache, f, indent=2)
    except Exception:
        pass


def _get_cached_iso(fingerprint: str):
    """Return (infra_env_id, iso_path) if a valid cached ISO exists, else (None, None)."""
    with _iso_lock:
        entry = _iso_cache.get(fingerprint)
    if not entry:
        return None, None
    iso_path = Path(entry['iso_path'])
    if not iso_path.exists():
        return None, None
    return entry['infra_env_id'], iso_path


def _store_iso_cache(fingerprint: str, infra_env_id: str, iso_path: Path,
                     ocp_version: str, pull_secret: str, ssh_public_key: str):
    with _iso_lock:
        _iso_cache[fingerprint] = {
            'infra_env_id':   infra_env_id,
            'iso_path':       str(iso_path),
            'ocp_version':    ocp_version,
            'downloaded_at':  time.time(),
            'ps_hint':        pull_secret.strip()[:6] + '…',
            'ssh_hint':       (ssh_public_key.strip()[:30] + '…') if ssh_public_key.strip() else '',
        }
        _save_iso_cache()


_load_iso_cache()

# per-job dict: job_id → { status, logs, progress, phase, result, config }
_jobs: dict = {}
_token_cache: dict = {}  # ps_hash → { token, expires_at }
_lock = threading.Lock()
_running_jobs: set = set()   # job_ids with active deploy threads in this process
_stop_jobs: set   = set()    # job_ids whose threads should exit at the next safe point


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


# ── credential secrets store ──────────────────────────────────────────────────
# Credentials (offline_token, pull_secret) are kept in a separate file with
# 0600 permissions so they survive service restarts for resume capability,
# but are never included in the UI-visible jobs.json config summary.

_SECRETS_FILE = WORK_DIR / '.job_secrets'
_secrets: dict = {}   # job_id → { offline_token, pull_secret }


def _load_secrets():
    global _secrets
    try:
        if _SECRETS_FILE.exists():
            with open(_SECRETS_FILE) as f:
                _secrets = json.load(f)
    except Exception:
        _secrets = {}


def _save_secrets():
    try:
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        with open(_SECRETS_FILE, 'w') as f:
            json.dump(_secrets, f)
        os.chmod(_SECRETS_FILE, 0o600)
    except Exception:
        pass


def _store_job_secrets(job_id: str, offline_token: str, pull_secret: str):
    with _lock:
        _secrets[job_id] = {'offline_token': offline_token, 'pull_secret': pull_secret}
        _save_secrets()


def _get_job_secrets(job_id: str) -> dict:
    return dict(_secrets.get(job_id, {}))


def _delete_job_secrets(job_id: str):
    with _lock:
        _secrets.pop(job_id, None)
        _save_secrets()


_load_secrets()


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

    # Persist credentials separately (0600 file) so the job can be resumed
    # if the service restarts mid-deployment.
    _store_job_secrets(job_id, cfg['offline_token'], cfg['pull_secret'])

    _running_jobs.add(job_id)
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
    # Tell the frontend whether credentials are stored so it can skip the input form
    stored = _get_job_secrets(job_id)
    return jsonify({**job, 'has_credentials': bool(stored.get('offline_token'))})


@ocp_bp.route('/api/openshift/jobs/<job_id>', methods=['DELETE'])
def delete_job(job_id):
    err = _auth()
    if err:
        return err

    with _lock:
        job = _jobs.get(job_id, {})

    vm_names     = job.get('vms', [])
    storage_path = Path(job.get('config', {}).get('storage_path', '/var/lib/libvirt/images'))
    deleted_vms, deleted_disks = [], []

    if vm_names:
        try:
            conn = libvirt.open('qemu:///system')
            for vm_name in vm_names:
                try:
                    dom = conn.lookupByName(vm_name)
                    if dom.isActive():
                        dom.destroy()
                    dom.undefine()
                    deleted_vms.append(vm_name)
                except libvirt.libvirtError:
                    pass  # VM already gone

            conn.close()
        except Exception:
            pass

        # Remove disk files (primary + extra disks)
        for vm_name in vm_names:
            for disk in storage_path.glob(f'{vm_name}*.qcow2'):
                try:
                    disk.unlink()
                    deleted_disks.append(str(disk))
                except OSError:
                    pass

    with _lock:
        _jobs.pop(job_id, None)
        _save_jobs()

    _delete_job_secrets(job_id)

    return jsonify({'ok': True, 'deleted_vms': deleted_vms, 'deleted_disks': deleted_disks})


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


def _insert_cdroms(vm_names: list, iso_path: str, log_fn):
    """Re-insert the discovery ISO into all VMs (used when retrying after error).

    Also sets the CDROM as boot-order-1 so the next reboot boots from ISO.
    """
    try:
        conn = libvirt.open('qemu:///system')
    except Exception as e:
        log_fn(f'  Cannot open libvirt for CDROM insert: {e}', 'warn')
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
                insert_xml = (
                    f"<disk type='file' device='cdrom'>"
                    f"<driver name='qemu' type='raw'/>"
                    f"<source file='{iso_path}'/>"
                    f"<target dev='{dev}' bus='{bus}'/>"
                    f"<readonly/>"
                    f"<boot order='1'/>"
                    f"</disk>"
                )
                try:
                    dom.updateDeviceFlags(
                        insert_xml,
                        libvirt.VIR_DOMAIN_AFFECT_LIVE |
                        libvirt.VIR_DOMAIN_AFFECT_CONFIG,
                    )
                    log_fn(f'  Inserted ISO into {vm_name} ({dev}) ✓')
                except libvirt.libvirtError as e:
                    log_fn(f'  CDROM insert warning ({vm_name}): {e}', 'warn')
        except libvirt.libvirtError:
            pass

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
    """Full deployment pipeline — runs in a daemon thread.

    Safe to restart mid-flight: each major step checks whether its output
    already exists in the persisted job state and skips the work if so.
    This lets _resume_pending_jobs() re-spawn this function after a service
    restart without redoing completed steps.
    """
    _running_jobs.add(job_id)

    def log(msg, level='info'):
        _job_log(job_id, msg, level)

    def phase(name, pct):
        # Never go backwards in progress when resuming
        current = _jobs.get(job_id, {}).get('progress', 0)
        _job_set(job_id, phase=name, progress=max(current, pct))
        log(f'── {name} ──')

    def fail(msg):
        _job_set(job_id, status='failed', phase='Failed')
        log(msg, 'error')
        _running_jobs.discard(job_id)

    try:
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        cluster_name = cfg['cluster_name']
        job_dir      = WORK_DIR / job_id
        job_dir.mkdir(exist_ok=True)

        # ── Resume state ──────────────────────────────────────────────────────
        # Read whatever was persisted before the restart.
        saved          = _jobs.get(job_id, {})
        saved_cluster  = saved.get('cluster_id')
        saved_infra    = saved.get('infra_env_id')
        saved_iso      = saved.get('iso_path')
        saved_vms      = saved.get('vms', [])
        is_resuming    = saved.get('progress', 0) > 0

        if is_resuming:
            log('── Resuming interrupted deployment ──', 'warn')
            log(f'  cluster_id   : {saved_cluster or "not yet created"}')
            log(f'  infra_env_id : {saved_infra   or "not yet created"}')
            log(f'  iso_path     : {saved_iso     or "not yet downloaded"}')
            log(f'  vms          : {saved_vms or "not yet created"}')

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
        if saved_cluster:
            cluster_id = saved_cluster
            log(f'Resuming: cluster already exists ({cluster_id}) ✓')
            phase('Cluster record exists', 10)
        else:
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

        # ── Step 3 + 4: Infra-env + discovery ISO ───────────────────────────────
        use_static_ip   = bool(cfg.get('static_ip_enabled') and cfg.get('node_ips'))
        iso_fingerprint = _iso_fingerprint(
            cfg['ocp_version'], cfg['pull_secret'], cfg.get('ssh_public_key', '')
        )

        # Check if infra-env + ISO already exist from a previous (interrupted) run
        if saved_infra and saved_iso and Path(saved_iso).exists():
            infra_env_id = saved_infra
            iso_path     = Path(saved_iso)
            log(f'Resuming: infra-env {infra_env_id} and ISO already downloaded ✓')
            phase('Infrastructure environment ready', 32)
        else:
            phase('Creating infrastructure environment', 18)
            # Static IP config (MAC→IP) is deployment-specific and baked into the ISO —
            # never reuse a cached ISO for static IP deployments.
            if use_static_ip:
                cached_infra_env_id, cached_iso_path = None, None
            else:
                cached_infra_env_id, cached_iso_path = _get_cached_iso(iso_fingerprint)

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

            if cached_infra_env_id:
                infra_env_id = cached_infra_env_id
                iso_path     = cached_iso_path
                log(f'Using cached infra-env {infra_env_id} and ISO ✓ (skipping download)')
                phase('Using cached discovery ISO', 32)
            else:
                try:
                    r = _ai('POST', '/infra-envs', token, infra_payload)
                    infra_env_id = r.json()['id']
                    log(f'Infra-env created: {infra_env_id} ✓')
                except Exception as e:
                    fail(f'Failed to create infra-env: {e}')
                    return

                # ── Step 4: Download discovery ISO ───────────────────────────
                phase('Downloading discovery ISO', 22)
                if use_static_ip:
                    iso_path = job_dir / 'discovery.iso'
                    iso_path.parent.mkdir(parents=True, exist_ok=True)
                else:
                    iso_path = ISO_CACHE_DIR / iso_fingerprint / 'discovery.iso'
                    iso_path.parent.mkdir(parents=True, exist_ok=True)
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
                    if use_static_ip:
                        log(f'ISO downloaded (static IP — not cached) ✓')
                    else:
                        log(f'ISO downloaded and cached ✓')
                    os.chmod(iso_path, 0o644)
                    p = iso_path.parent
                    while p != WORK_DIR.parent:
                        try:
                            os.chmod(p, p.stat().st_mode | 0o111)
                        except Exception:
                            pass
                        p = p.parent
                    if not use_static_ip:
                        _store_iso_cache(iso_fingerprint, infra_env_id, iso_path,
                                         cfg['ocp_version'], cfg['pull_secret'],
                                         cfg.get('ssh_public_key', ''))
                except Exception as e:
                    fail(f'ISO download failed: {e}')
                    return

            # Persist infra_env_id + iso_path so a resume skips this section
            _job_set(job_id, infra_env_id=infra_env_id, iso_path=str(iso_path))

        # ── Step 5: Create VMs ────────────────────────────────────────────────
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

        # Check if VMs already exist from a previous (interrupted) run
        if saved_vms:
            existing_running = []
            try:
                for vm_name in saved_vms:
                    try:
                        d = conn.lookupByName(vm_name)
                        if d.isActive():
                            existing_running.append(vm_name)
                    except libvirt.libvirtError:
                        pass
            except Exception:
                pass

            if len(existing_running) == len(vm_names):
                log(f'Resuming: all {len(existing_running)} VMs already running ✓')
                phase('VMs already running', 44)
                conn.close()
                _job_set(job_id, vms=saved_vms)
                # Jump straight to node registration
                created_vms = saved_vms
                goto_node_wait = True
            else:
                log(f'Resuming: only {len(existing_running)}/{len(vm_names)} VMs running — recreating all', 'warn')
                goto_node_wait = False
                created_vms = []
        else:
            goto_node_wait = False
            created_vms = []

        if not goto_node_wait:
            phase('Creating KVM virtual machines', 35)

            def _make_disk(path: Path, size_gb: int) -> bool:
                """Create a qcow2 at path, chmod it. Returns True on success."""
                if path.exists():
                    path.unlink()
                rc = subprocess.run(
                    ['qemu-img', 'create', '-f', 'qcow2', str(path), f'{size_gb}G'],
                    capture_output=True, text=True,
                )
                if rc.returncode != 0:
                    fail(f'qemu-img failed ({path.name}): {rc.stderr}')
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
                        mac_address = mac_map.get(vm_name),
                    )
                    dom = conn.defineXML(xml)
                    dom.create()
                    created_vms.append(vm_name)
                    role = 'worker' if is_worker else ('SNO' if is_sno else 'control-plane')
                    extra_info = f' + {len(extra_paths)} extra disk(s)' if extra_paths else ''
                    mac_info   = f', MAC {mac_map[vm_name]}' if vm_name in mac_map else ''
                    log(f'VM {vm_name} started ({vcpus} vCPU, {ram_mb//1024} GB RAM, {disk_gb} GB{extra_info}, {role}{mac_info}) ✓')
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

        # Fast-forward: if the cluster is already past the "ready" state (i.e. a
        # previous run triggered installation before this thread was killed), skip
        # node-waiting and jump straight to installation monitoring.
        try:
            _fc_token = _get_access_token(cfg['offline_token'])
            _fc_r = _ai('GET', f'/clusters/{cluster_id}', _fc_token)
            _fc_status = _fc_r.json().get('status', '')
            _PAST_READY = {'preparing-for-installation', 'installing', 'installing-in-progress',
                           'installing-pending-user-action', 'finalizing', 'installed'}
            if _fc_status in _PAST_READY:
                log(f'Resuming: cluster already in "{_fc_status}" — skipping node wait, jumping to install monitor')
                phase('Installing OpenShift', 65)
                if 'pending-user-action' in _fc_status:
                    log('  ⚠ Nodes pending user action — ejecting ISO and rebooting…', 'warn')
                    _eject_cdroms(vm_names, log)
                    time.sleep(2)
                    _reboot_vms(vm_names, log)
                _monitor_install_thread(job_id, cfg, cluster_id)
                return
            elif _fc_status == 'error':
                fail(f'Cluster is in error state: {_fc_r.json().get("status_info", "")}')
                return
        except Exception as _fc_e:
            log(f'  Cluster status pre-check failed ({_fc_e}), proceeding with node wait…', 'warn')

        deadline = time.time() + 45 * 60   # 45 min timeout
        registered = []
        _vm_cpu_prev: dict = {}   # vm_name → last seen cpu_time_ns

        def _qemu_log_tail(vm_name: str, lines: int = 10) -> list:
            """Return last N error/warning lines from the QEMU log for this VM."""
            log_path = Path(f'/var/log/libvirt/qemu/{vm_name}.log')
            if not log_path.exists():
                return []
            try:
                text = log_path.read_text(errors='replace')
                matches = []
                for line in text.splitlines():
                    lower = line.lower()
                    if any(kw in lower for kw in ('error', 'warn', 'fail', 'killed', 'oom',
                                                   'out of memory', 'segfault', 'panic')):
                        matches.append(line.strip())
                return matches[-lines:] if matches else text.strip().splitlines()[-lines:]
            except Exception:
                return []

        while time.time() < deadline:
            if job_id in _stop_jobs:
                _stop_jobs.discard(job_id)
                log('Deployment stopped by reset request.', 'warn')
                return
            try:
                # Refresh token if needed
                token = _get_access_token(cfg['offline_token'])
                r = _ai('GET', f'/clusters/{cluster_id}/hosts', token)
                hosts = r.json()
                registered = [h for h in hosts if h.get('status') not in ('', None, 'disconnected')]
                known_count = len(registered)
                log(f'  {known_count}/{total_nodes} node(s) discovered')
                _job_set(job_id, progress=45 + min(known_count, total_nodes) * 3)

                # Diagnose VMs that haven't registered yet
                if known_count < total_nodes:
                    registered_names = {h.get('requested_hostname', '') for h in registered}
                    missing = [n for n in vm_names if not any(n in rn for rn in registered_names)]
                    if missing:
                        try:
                            lv = libvirt.open('qemu:///system')
                            for vm in missing:
                                try:
                                    d = lv.lookupByName(vm)
                                    state_map = {0: 'nostate', 1: 'running', 2: 'blocked',
                                                 3: 'paused', 4: 'shutdown', 5: 'shutoff', 6: 'crashed'}
                                    st = state_map.get(d.state()[0], 'unknown')

                                    if st not in ('running', 'blocked'):
                                        # VM is not running — log QEMU errors and restart
                                        log(f'  ⚠ {vm} is {st} — checking logs…', 'warn')
                                        for err_line in _qemu_log_tail(vm):
                                            log(f'    QEMU: {err_line}', 'warn')
                                        log(f'  ↺ Restarting {vm}…', 'warn')
                                        try:
                                            if d.isActive(): d.destroy()
                                            d.create()
                                            log(f'  {vm} restarted ✓')
                                        except Exception as re:
                                            log(f'  {vm} restart failed: {re}', 'warn')
                                    else:
                                        # VM is running — check if CPU time is growing
                                        try:
                                            cpu_ns = d.getCPUStats(True)[0].get('cpu_time', 0)
                                            prev   = _vm_cpu_prev.get(vm, 0)
                                            _vm_cpu_prev[vm] = cpu_ns
                                            cpu_s  = cpu_ns / 1e9

                                            if prev > 0 and (cpu_ns - prev) < 1e8:   # <0.1s growth
                                                # Frozen — not making progress
                                                log(f'  ⚠ {vm}: running but CPU frozen ({cpu_s:.1f}s) — checking logs…', 'warn')
                                                for err_line in _qemu_log_tail(vm):
                                                    log(f'    QEMU: {err_line}', 'warn')
                                                log(f'  ↺ Restarting frozen VM {vm}…', 'warn')
                                                try:
                                                    d.destroy()
                                                    d.create()
                                                    _vm_cpu_prev[vm] = 0
                                                    log(f'  {vm} restarted ✓')
                                                except Exception as re:
                                                    log(f'  {vm} restart failed: {re}', 'warn')
                                            else:
                                                log(f'  {vm}: running ({cpu_s:.1f}s CPU), still booting…')
                                        except Exception:
                                            log(f'  {vm}: running, still booting…')
                                except libvirt.libvirtError:
                                    log(f'  ⚠ {vm}: not found in libvirt', 'warn')
                                    for err_line in _qemu_log_tail(vm):
                                        log(f'    QEMU: {err_line}', 'warn')
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
            'preparing-for-installation':     65,
            'installing':                     70,
            'installing-in-progress':         70,
            'installing-pending-user-action': 72,
            'finalizing':                     88,
            'installed':                      100,
        }

        consecutive_errors = 0

        while time.time() < deadline:
            if job_id in _stop_jobs:
                _stop_jobs.discard(job_id)
                log('Deployment stopped by reset request.', 'warn')
                return
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
        # Credentials no longer needed — delete from secrets file
        _delete_job_secrets(job_id)

    except Exception as e:
        _job_set(job_id, status='failed', phase='Failed')
        _job_log(job_id, f'Unexpected error: {e}', 'error')

    finally:
        _running_jobs.discard(job_id)


# ── Resume interrupted deployments on startup ────────────────────────────────

def _resume_pending_jobs():
    """Called once at startup (per worker process).

    Any job that is still 'pending' (i.e. the deploy thread was killed by a
    service restart) and has stored credentials will have its thread re-spawned.
    The re-spawned _run_deploy call reads the persisted job state (cluster_id,
    infra_env_id, iso_path, vms) and skips all already-completed steps.
    """
    import logging as _logging
    logger = _logging.getLogger(__name__)
    resumed = 0
    for job_id, job in list(_jobs.items()):
        if job.get('status') != 'pending':
            continue
        if job_id in _running_jobs:
            continue   # already running in this process
        secrets = _get_job_secrets(job_id)
        if not secrets.get('offline_token') or not secrets.get('pull_secret'):
            logger.warning(f'[OCP] Job {job_id} is pending but has no stored credentials — cannot auto-resume')
            continue
        # Merge credentials back into the config so _run_deploy has everything it needs
        cfg = {**job.get('config', {}), **secrets}
        logger.info(f'[OCP] Auto-resuming interrupted deployment: {job_id} ({job.get("config", {}).get("cluster_name", "?")})')
        _running_jobs.add(job_id)
        t = threading.Thread(
            target=_run_deploy,
            args=(job_id, cfg),
            daemon=True,
            name=f'ocp-resume-{job_id}',
        )
        t.start()
        resumed += 1
    if resumed:
        logger.info(f'[OCP] Resumed {resumed} interrupted deployment(s)')


# Called after all functions are defined so _run_deploy is available
_resume_pending_jobs()


# ── Sync job state from Assisted Installer ────────────────────────────────────

def _collect_credentials(job_id: str, cluster_id: str, cluster_name: str,
                          base_domain: str, token: str):
    """Fetch kubeconfig + kubeadmin password and mark the job complete."""
    def log(msg, level='info'):
        _job_log(job_id, msg, level)

    job_dir = WORK_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    phase_fn = lambda name, pct: _job_set(job_id, phase=name, progress=pct)
    phase_fn('Collecting credentials', 98)

    result = {}
    try:
        r = _ai('GET', f'/clusters/{cluster_id}/credentials', token)
        creds = r.json()
        kubeconfig_raw = creds.get('kubeconfig', '')
        result['kubeadmin_password'] = creds.get('password', '')
        kc_path = job_dir / 'kubeconfig'
        kc_path.write_text(kubeconfig_raw)
        result['kubeconfig_path'] = str(kc_path)
        log(f'kubeconfig saved → {kc_path} ✓')
    except Exception as e:
        log(f'Credential fetch warning: {e}', 'warn')

    api_url = f'https://api.{cluster_name}.{base_domain}:6443'
    console = f'https://console-openshift-console.apps.{cluster_name}.{base_domain}'
    result.update({'api_url': api_url, 'console_url': console, 'cluster_id': cluster_id})
    _job_set(job_id, result=result, status='complete', progress=100, phase='Complete')
    log('OpenShift installation complete! 🎉')
    log(f'Console: {console}')
    log(f'API:     {api_url}')
    _delete_job_secrets(job_id)
    _running_jobs.discard(job_id)


def _monitor_install_thread(job_id: str, cfg: dict, cluster_id: str):
    """Lightweight thread: just monitors an already-started installation."""
    _running_jobs.add(job_id)

    def log(msg, level='info'):
        _job_log(job_id, msg, level)

    cluster_name = cfg.get('cluster_name', '')
    base_domain  = cfg.get('base_domain', '')
    vm_names     = _jobs.get(job_id, {}).get('vms', [])

    PHASE_PCT = {
        'preparing-for-installation':      65,
        'installing':                      70,
        'installing-in-progress':          70,
        'installing-pending-user-action':  72,
        'finalizing':                      88,
        'installed':                       100,
    }

    try:
        deadline = time.time() + 2 * 3600
        last_status = ''
        last_pct    = 0
        pending_handled: set = set()
        consecutive_errors  = 0

        while time.time() < deadline:
            if job_id in _stop_jobs:
                _stop_jobs.discard(job_id)
                log('Monitoring stopped by reset request.', 'warn')
                return
            try:
                token = _get_access_token(cfg['offline_token'])
                r = _ai('GET', f'/clusters/{cluster_id}', token)
                cluster_data = r.json()
                status      = cluster_data.get('status', '')
                status_info = cluster_data.get('status_info', '')
                install_pct = cluster_data.get('progress', {}).get('total_percentage', 0)
                consecutive_errors = 0

                if status != last_status or install_pct != last_pct:
                    log(f'  Status: {status} ({install_pct}%) — {status_info}')
                    last_status = status
                    last_pct    = install_pct
                    pct = PHASE_PCT.get(status, 70) + int(install_pct * 0.25)
                    _job_set(job_id, progress=min(pct, 98),
                             phase=f'Installing OpenShift ({install_pct}%)')

                if status == 'installed':
                    _collect_credentials(job_id, cluster_id, cluster_name, base_domain, token)
                    return
                if status in ('error', 'cancelled'):
                    _job_set(job_id, status='failed', phase='Failed')
                    log(f'Installation {status}: {status_info}', 'error')
                    return

                # Auto-recover pending-user-action hosts
                try:
                    hr = _ai('GET', f'/clusters/{cluster_id}/hosts', token)
                    for h in hr.json():
                        h_status = h.get('status', '')
                        h_id     = h.get('id', '')
                        hostname = h.get('requested_hostname') or h_id[:8]
                        if 'pending-user-action' in h_status and h_id not in pending_handled:
                            log(f'  ⚠ Host {hostname} booted ISO — auto-recovering…', 'warn')
                            _eject_cdroms(vm_names, log)
                            time.sleep(3)
                            _reboot_vms(vm_names, log)
                            pending_handled.add(h_id)
                except Exception:
                    pass

            except Exception as e:
                consecutive_errors += 1
                if consecutive_errors == 1:
                    log(f'  Monitoring error (retrying): {e}', 'warn')
                wait = min(30 * (2 ** min(consecutive_errors - 1, 4)), 300)
                time.sleep(wait)
                continue

            time.sleep(30)
        else:
            _job_set(job_id, status='failed', phase='Failed')
            log('Installation monitoring timed out after 2 hours.', 'error')

    except Exception as e:
        _job_set(job_id, status='failed', phase='Failed')
        _job_log(job_id, f'Unexpected error in monitor thread: {e}', 'error')
    finally:
        _running_jobs.discard(job_id)


@ocp_bp.route('/api/openshift/jobs/<job_id>/sync', methods=['POST'])
def sync_job(job_id):
    """Sync a stuck/pending job with the real state in the Assisted Installer.

    Credentials priority:
      1. Stored secrets file (written at deploy time — most common case)
      2. offline_token + pull_secret in request body (fallback for legacy jobs)

    Once resolved, queries the AI cluster state and:
      • installed              → collect credentials, mark complete
      • installing / finalizing → spawn monitoring thread
      • ready / known          → full resume via _run_deploy
      • error / cancelled      → mark failed
    """
    err = _auth()
    if err:
        return err

    job = _jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    if job_id in _running_jobs:
        return jsonify({'error': 'Job already has an active thread'}), 409

    cluster_id = job.get('cluster_id')
    if not cluster_id:
        return jsonify({'error': 'No cluster_id recorded — job must be restarted from scratch'}), 400

    # ── Resolve credentials: stored secrets take priority ─────────────────────
    stored        = _get_job_secrets(job_id)
    data          = request.get_json(silent=True) or {}
    offline_token = stored.get('offline_token') or data.get('offline_token', '').strip()
    pull_secret   = stored.get('pull_secret')   or data.get('pull_secret', '').strip()

    if not offline_token or not pull_secret:
        return jsonify({'error': 'no_stored_credentials',
                        'message': 'No stored credentials for this job — please provide offline_token and pull_secret'}), 400

    cfg = {**job.get('config', {}), 'offline_token': offline_token, 'pull_secret': pull_secret}

    # Persist credentials (in case they came from request body this time)
    _store_job_secrets(job_id, offline_token, pull_secret)

    try:
        token = _get_access_token(offline_token)
        r = _ai('GET', f'/clusters/{cluster_id}', token)
        ai_status   = r.json().get('status', '')
        status_info = r.json().get('status_info', '')
    except Exception as e:
        return jsonify({'error': f'Could not reach Assisted Installer: {e}'}), 502

    cluster_name = cfg.get('cluster_name', '')
    base_domain  = cfg.get('base_domain', '')

    _job_log(job_id, f'── Sync from Assisted Installer ──', 'warn')
    _job_log(job_id, f'  AI cluster status: {ai_status} — {status_info}')

    INSTALLING = {
        'preparing-for-installation', 'installing', 'installing-in-progress',
        'finalizing', 'installing-pending-user-action',
    }

    vm_names = job.get('vms', [])

    if ai_status == 'installed':
        # Already done — just collect credentials in a quick thread
        def _do_collect():
            _running_jobs.add(job_id)
            try:
                tok = _get_access_token(offline_token)
                _collect_credentials(job_id, cluster_id, cluster_name, base_domain, tok)
            except Exception as ex:
                _job_set(job_id, status='failed', phase='Failed')
                _job_log(job_id, f'Credential collection failed: {ex}', 'error')
            finally:
                _running_jobs.discard(job_id)
        threading.Thread(target=_do_collect, daemon=True,
                         name=f'ocp-sync-{job_id}').start()
        return jsonify({'action': 'collecting_credentials', 'ai_status': ai_status})

    elif ai_status in INSTALLING:
        _job_set(job_id, phase='Installing OpenShift', progress=65)

        # If nodes are stuck waiting for a reboot-from-disk, fix it immediately
        # rather than waiting for the first monitor poll (30 s).
        if 'pending-user-action' in ai_status and vm_names:
            def _do_eject_log(msg, level='info'):
                _job_log(job_id, msg, level)
            _job_log(job_id, '  ⚠ Nodes pending user action — ejecting ISO and rebooting VMs…', 'warn')
            try:
                _eject_cdroms(vm_names, _do_eject_log)
                time.sleep(2)
                _reboot_vms(vm_names, _do_eject_log)
            except Exception as ex:
                _job_log(job_id, f'  Eject/reboot warning: {ex}', 'warn')

        threading.Thread(target=_monitor_install_thread,
                         args=(job_id, cfg, cluster_id),
                         daemon=True, name=f'ocp-sync-{job_id}').start()
        return jsonify({'action': 'monitoring_installation', 'ai_status': ai_status})

    elif ai_status in ('error', 'cancelled'):
        _job_set(job_id, status='failed', phase='Failed')
        _job_log(job_id, f'Cluster {ai_status}: {status_info}', 'error')
        return jsonify({'action': 'marked_failed', 'ai_status': ai_status})

    else:
        # ready / known / insufficient / etc — do a full resume
        _job_set(job_id, status='pending', phase='Resuming deployment')
        _running_jobs.add(job_id)
        threading.Thread(target=_run_deploy, args=(job_id, cfg),
                         daemon=True, name=f'ocp-sync-{job_id}').start()
        return jsonify({'action': 'full_resume', 'ai_status': ai_status})


@ocp_bp.route('/api/openshift/jobs/<job_id>/retry', methods=['POST'])
def retry_job(job_id):
    """Retry a failed deployment.

    Cancels + resets the cluster in Assisted Installer, re-inserts the discovery
    ISO into every VM, reboots them back into discovery mode, then resumes the
    deployment pipeline from the node-registration step.
    """
    err = _auth()
    if err:
        return err

    job = _jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    if job_id in _running_jobs:
        return jsonify({'error': 'Job already has an active thread'}), 409

    cluster_id = job.get('cluster_id')
    if not cluster_id:
        return jsonify({'error': 'No cluster_id recorded — job must be restarted from scratch'}), 400

    iso_path = job.get('iso_path')
    if not iso_path or not Path(iso_path).exists():
        return jsonify({'error': f'Discovery ISO not found at {iso_path} — cannot retry'}), 400

    stored        = _get_job_secrets(job_id)
    data          = request.get_json(silent=True) or {}
    offline_token = stored.get('offline_token') or data.get('offline_token', '').strip()
    pull_secret   = stored.get('pull_secret')   or data.get('pull_secret', '').strip()

    if not offline_token or not pull_secret:
        return jsonify({'error': 'no_stored_credentials',
                        'message': 'No stored credentials — please provide offline_token and pull_secret'}), 400

    cfg = {**job.get('config', {}), 'offline_token': offline_token, 'pull_secret': pull_secret}
    _store_job_secrets(job_id, offline_token, pull_secret)

    def _log(msg, level='info'):
        _job_log(job_id, msg, level)

    try:
        token = _get_access_token(offline_token)
        # Check current AI status so we know whether cancel is needed
        r = _ai('GET', f'/clusters/{cluster_id}', token)
        ai_status = r.json().get('status', '')
    except Exception as e:
        return jsonify({'error': f'Could not reach Assisted Installer: {e}'}), 502

    _job_log(job_id, '── Retry: resetting cluster ──', 'warn')
    _job_log(job_id, f'  AI cluster status before reset: {ai_status}')

    try:
        # Cancel first if the cluster is in a cancellable state
        _CANCELLABLE = {'installing', 'installing-in-progress', 'installing-pending-user-action',
                        'finalizing', 'error'}
        if ai_status in _CANCELLABLE:
            rc = _ai('POST', f'/clusters/{cluster_id}/actions/cancel', token, body={})
            _job_log(job_id, f'  Cancel: HTTP {rc.status_code}')
            time.sleep(1)

        # Reset cluster to insufficient / pending state
        rr = _ai('POST', f'/clusters/{cluster_id}/actions/reset', token, body={})
        if rr.status_code not in (200, 201, 202):
            return jsonify({'error': f'Reset failed: HTTP {rr.status_code} — {rr.text[:200]}'}), 502
        _job_log(job_id, f'  Cluster reset ✓ (HTTP {rr.status_code})')

    except Exception as e:
        return jsonify({'error': f'Reset failed: {e}'}), 502

    # Re-insert ISO and reboot VMs
    vm_names = job.get('vms', [])
    if vm_names:
        _job_log(job_id, '  Re-inserting discovery ISO into VMs…')
        _insert_cdroms(vm_names, iso_path, _log)
        time.sleep(1)
        _job_log(job_id, '  Rebooting VMs into discovery mode…')
        _reboot_vms(vm_names, _log)
    else:
        _job_log(job_id, '  No VM list found — skipping ISO reinsert', 'warn')

    # Reset job to pending so the resume loop runs from node registration
    _job_set(job_id, status='pending', phase='Waiting for nodes (retry)', progress=45)

    # Resume full deployment pipeline; it will skip cluster/ISO/VM creation
    # since cluster_id, infra_env_id, and vms are already persisted.
    _running_jobs.add(job_id)
    threading.Thread(target=_run_deploy, args=(job_id, cfg),
                     daemon=True, name=f'ocp-retry-{job_id}').start()

    return jsonify({'action': 'retrying', 'ai_status': ai_status, 'vms_rebooted': len(vm_names)})


@ocp_bp.route('/api/openshift/jobs/<job_id>/reset', methods=['POST'])
def reset_cluster(job_id):
    """Reset a cluster back to discovery/ready state without auto-resuming.

    Signals any running thread to stop, then:
      1. Cancels the installation (if active)
      2. Resets the AI cluster to insufficient
      3. Re-inserts the discovery ISO into every VM
      4. Reboots VMs back into discovery mode
      5. Sets job status to 'pending', awaiting manual Retry or Sync

    Works regardless of current job status — useful for running, stuck,
    or failed deployments that need a clean slate at the cluster level.
    """
    err = _auth()
    if err:
        return err

    job = _jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    cluster_id = job.get('cluster_id')
    if not cluster_id:
        return jsonify({'error': 'No cluster_id recorded — job must be restarted from scratch'}), 400

    iso_path = job.get('iso_path')

    stored        = _get_job_secrets(job_id)
    data          = request.get_json(silent=True) or {}
    offline_token = stored.get('offline_token') or data.get('offline_token', '').strip()
    pull_secret   = stored.get('pull_secret')   or data.get('pull_secret', '').strip()

    if not offline_token or not pull_secret:
        return jsonify({'error': 'no_stored_credentials',
                        'message': 'No stored credentials — please provide offline_token and pull_secret'}), 400

    _store_job_secrets(job_id, offline_token, pull_secret)

    def _log(msg, level='info'):
        _job_log(job_id, msg, level)

    # Signal any running thread to stop at its next safe point
    if job_id in _running_jobs:
        _stop_jobs.add(job_id)
        _log('── Reset requested — signalling active thread to stop ──', 'warn')
        # Give it up to 5 s to notice the flag (poll intervals are 20–30 s,
        # so we can't wait for a clean exit; new thread starts immediately).
        time.sleep(1)
        _running_jobs.discard(job_id)

    try:
        token = _get_access_token(offline_token)
        r = _ai('GET', f'/clusters/{cluster_id}', token)
        ai_status = r.json().get('status', '')
    except Exception as e:
        return jsonify({'error': f'Could not reach Assisted Installer: {e}'}), 502

    _log('── Resetting cluster ──', 'warn')
    _log(f'  AI cluster status before reset: {ai_status}')

    try:
        _CANCELLABLE = {'installing', 'installing-in-progress', 'installing-pending-user-action',
                        'finalizing', 'error'}
        if ai_status in _CANCELLABLE:
            rc = _ai('POST', f'/clusters/{cluster_id}/actions/cancel', token, body={})
            _log(f'  Cancel: HTTP {rc.status_code}')
            time.sleep(1)

        rr = _ai('POST', f'/clusters/{cluster_id}/actions/reset', token, body={})
        if rr.status_code not in (200, 201, 202):
            return jsonify({'error': f'Reset failed: HTTP {rr.status_code} — {rr.text[:200]}'}), 502
        _log(f'  Cluster reset ✓ (HTTP {rr.status_code})')

    except Exception as e:
        return jsonify({'error': f'Reset failed: {e}'}), 502

    vm_names = job.get('vms', [])
    if vm_names and iso_path and Path(iso_path).exists():
        _log('  Re-inserting discovery ISO into VMs…')
        _insert_cdroms(vm_names, iso_path, _log)
        time.sleep(1)
        _log('  Rebooting VMs into discovery mode…')
        _reboot_vms(vm_names, _log)
    elif vm_names:
        _log(f'  ISO not found at {iso_path} — VMs not rebooted', 'warn')
    else:
        _log('  No VM list — skipping ISO reinsert', 'warn')

    _job_set(job_id, status='pending', phase='Cluster reset — waiting for nodes', progress=45)

    return jsonify({'action': 'reset', 'ai_status': ai_status, 'vms_rebooted': len(vm_names)})


# ── Kubeconfig download ───────────────────────────────────────────────────────

# ── ISO cache management ──────────────────────────────────────────────────────

@ocp_bp.route('/api/openshift/isos')
def list_isos():
    err = _auth()
    if err:
        return err
    with _iso_lock:
        entries = []
        for fp, entry in _iso_cache.items():
            iso_path = Path(entry['iso_path'])
            entries.append({
                'fingerprint':   fp,
                'ocp_version':   entry.get('ocp_version'),
                'downloaded_at': entry.get('downloaded_at'),
                'ps_hint':       entry.get('ps_hint'),
                'ssh_hint':      entry.get('ssh_hint'),
                'size':          iso_path.stat().st_size if iso_path.exists() else 0,
                'exists':        iso_path.exists(),
            })
    return jsonify({'isos': entries})


@ocp_bp.route('/api/openshift/isos/prefetch', methods=['POST'])
def prefetch_iso():
    """Pre-download a discovery ISO so the next deployment can skip the download step."""
    err = _auth()
    if err:
        return err

    data           = request.get_json() or {}
    ocp_version    = data.get('ocp_version', '').strip()
    pull_secret    = data.get('pull_secret', '').strip()
    ssh_public_key = data.get('ssh_public_key', '').strip()

    if not ocp_version or not pull_secret:
        return jsonify({'error': 'ocp_version and pull_secret required'}), 400

    fingerprint = _iso_fingerprint(ocp_version, pull_secret, ssh_public_key)
    cached_id, cached_path = _get_cached_iso(fingerprint)
    if cached_id:
        return jsonify({'status': 'cached', 'fingerprint': fingerprint,
                        'infra_env_id': cached_id, 'iso_path': str(cached_path)})

    def _do_prefetch():
        try:
            token = _get_access_token(pull_secret)
            infra_payload = {
                'name':              f'prefetch-{ocp_version}-{fingerprint[:6]}',
                'openshift_version': ocp_version,
                'pull_secret':       pull_secret,
                'image_type':        'minimal-iso',
                'cpu_architecture':  'x86_64',
            }
            if ssh_public_key:
                infra_payload['ssh_authorized_key'] = ssh_public_key

            r = _ai('POST', '/infra-envs', token, infra_payload)
            infra_env_id = r.json()['id']

            r = _ai('GET', f'/infra-envs/{infra_env_id}/downloads/image-url', token)
            iso_url = r.json()['url']

            iso_path = ISO_CACHE_DIR / fingerprint / 'discovery.iso'
            iso_path.parent.mkdir(parents=True, exist_ok=True)

            with _req.get(iso_url, stream=True, timeout=300) as dl:
                dl.raise_for_status()
                with open(iso_path, 'wb') as f:
                    for chunk in dl.iter_content(chunk_size=1024 * 1024):
                        f.write(chunk)

            os.chmod(iso_path, 0o644)
            p = iso_path.parent
            while p != WORK_DIR.parent:
                try:
                    os.chmod(p, p.stat().st_mode | 0o111)
                except Exception:
                    pass
                p = p.parent

            _store_iso_cache(fingerprint, infra_env_id, iso_path,
                             ocp_version, pull_secret, ssh_public_key)
        except Exception as e:
            pass  # errors visible on next list_isos call

    threading.Thread(target=_do_prefetch, daemon=True).start()
    return jsonify({'status': 'downloading', 'fingerprint': fingerprint}), 202


@ocp_bp.route('/api/openshift/isos/<fingerprint>', methods=['DELETE'])
def delete_iso(fingerprint):
    err = _auth()
    if err:
        return err
    with _iso_lock:
        entry = _iso_cache.pop(fingerprint, None)
        _save_iso_cache()
    if entry:
        try:
            iso_path = Path(entry['iso_path'])
            if iso_path.exists():
                iso_path.unlink()
            if iso_path.parent.exists():
                iso_path.parent.rmdir()
        except Exception:
            pass
    return jsonify({'ok': True})


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
