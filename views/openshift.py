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
WORK_DIR = Path('/var/lib/hypercloud/openshift')

# per-job dict: job_id → { status, logs, progress, phase, result, config }
_jobs: dict = {}
_token_cache: dict = {}  # ps_hash → { token, expires_at }
_lock = threading.Lock()


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


def _job_set(job_id: str, **kw):
    with _lock:
        if job_id in _jobs:
            _jobs[job_id].update(kw)


# ── Assisted Installer API ────────────────────────────────────────────────────

def _extract_offline_token(pull_secret: str) -> str:
    """Decode cloud.openshift.com auth field → offline token."""
    ps = json.loads(pull_secret)
    b64 = ps['auths']['cloud.openshift.com']['auth']
    decoded = base64.b64decode(b64 + '==').decode()
    # format: "<user>:<token>"
    return decoded.split(':', 1)[1]


def _get_access_token(pull_secret: str) -> str:
    """Exchange offline token for a short-lived access token via RH SSO."""
    ps_hash = hashlib.sha256(pull_secret.encode()).hexdigest()[:16]
    now = time.time()
    cached = _token_cache.get(ps_hash)
    if cached and cached['expires_at'] > now + 60:
        return cached['token']

    offline_token = _extract_offline_token(pull_secret)
    resp = _req.post(SSO_URL, data={
        'grant_type':    'refresh_token',
        'client_id':     'cloud-services',
        'refresh_token': offline_token,
    }, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    token = data['access_token']
    _token_cache[ps_hash] = {'token': token, 'expires_at': now + data.get('expires_in', 900)}
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
    resp.raise_for_status()
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
    required = ['cluster_name', 'base_domain', 'pull_secret',
                'ocp_version', 'deployment_type', 'machine_cidr']
    missing = [f for f in required if not cfg.get(f)]
    if missing:
        return jsonify({'error': f'Missing fields: {", ".join(missing)}'}), 400

    job_id = uuid.uuid4().hex[:8]
    safe_cfg = {k: v for k, v in cfg.items() if k != 'pull_secret'}
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
    _jobs.pop(job_id, None)
    return jsonify({'ok': True})


# ── VM XML generation ─────────────────────────────────────────────────────────

def _vm_xml(name: str, vcpus: int, ram_mb: int, disk_path: str,
             iso_path: str, network: str = 'default') -> str:
    return f"""
<domain type='kvm'>
  <name>{name}</name>
  <uuid>{uuid.uuid4()}</uuid>
  <memory unit='MiB'>{ram_mb}</memory>
  <vcpu>{vcpus}</vcpu>
  <os>
    <type arch='x86_64' machine='q35'>hvm</type>
    <boot dev='hd'/>
    <boot dev='cdrom'/>
    <bootmenu enable='yes' timeout='3000'/>
  </os>
  <features><acpi/><apic/></features>
  <cpu mode='host-passthrough'/>
  <clock offset='utc'/>
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
    <interface type='network'>
      <source network='{network}'/>
      <model type='virtio'/>
    </interface>
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

        # ── Step 1: Auth ──────────────────────────────────────────────────────
        phase('Authenticating with Red Hat', 5)
        try:
            token = _get_access_token(cfg['pull_secret'])
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
            'name':               f'{cluster_name}-infra',
            'cluster_id':         cluster_id,
            'openshift_version':  cfg['ocp_version'],
            'pull_secret':        cfg['pull_secret'],
            'ssh_authorized_key': cfg.get('ssh_public_key', ''),
            'image_type':         'minimal-iso',
        }
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
        except Exception as e:
            fail(f'ISO download failed: {e}')
            return

        # ── Step 5: Create VMs ────────────────────────────────────────────────
        phase('Creating KVM virtual machines', 35)
        vm_names = []
        if is_sno:
            vm_names = [f'{cluster_name}-sno']
        else:
            vm_names  = [f'{cluster_name}-master-{i}' for i in range(n_control)]
            vm_names += [f'{cluster_name}-worker-{i}' for i in range(n_workers)]

        vcpus_cp  = int(cfg.get('cp_vcpus',  8))
        ram_cp    = int(cfg.get('cp_ram_gb',  32)) * 1024
        disk_cp   = int(cfg.get('cp_disk_gb', 120))
        vcpus_w   = int(cfg.get('w_vcpus',   4))
        ram_w     = int(cfg.get('w_ram_gb',   16)) * 1024
        disk_w    = int(cfg.get('w_disk_gb',  100))

        try:
            conn = libvirt.open('qemu:///system')
        except Exception as e:
            fail(f'Cannot connect to libvirt: {e}')
            return

        disk_dir = Path(cfg.get('storage_path', '/var/lib/libvirt/images'))
        created_vms = []

        try:
            for i, vm_name in enumerate(vm_names):
                is_worker = (not is_sno) and (i >= n_control)
                vcpus   = vcpus_w   if is_worker else vcpus_cp
                ram_mb  = ram_w     if is_worker else ram_cp
                disk_gb = disk_w    if is_worker else disk_cp

                disk_path = disk_dir / f'{vm_name}.qcow2'
                # Create qcow2 disk
                import subprocess
                r = subprocess.run(
                    ['qemu-img', 'create', '-f', 'qcow2', str(disk_path), f'{disk_gb}G'],
                    capture_output=True, text=True,
                )
                if r.returncode != 0:
                    fail(f'qemu-img failed for {vm_name}: {r.stderr}')
                    return

                xml = _vm_xml(
                    name    = vm_name,
                    vcpus   = vcpus,
                    ram_mb  = ram_mb,
                    disk_path = str(disk_path),
                    iso_path  = str(iso_path),
                    network   = cfg.get('libvirt_network', 'default'),
                )
                dom = conn.defineXML(xml)
                dom.create()
                created_vms.append(vm_name)
                role = 'worker' if is_worker else ('SNO' if is_sno else 'control-plane')
                log(f'VM {vm_name} started ({vcpus} vCPU, {ram_mb//1024} GB RAM, {disk_gb} GB disk, {role}) ✓')

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
        deadline = time.time() + 30 * 60   # 30 min timeout
        registered = []

        while time.time() < deadline:
            try:
                # Refresh token if needed
                token = _get_access_token(cfg['pull_secret'])
                r = _ai('GET', f'/clusters/{cluster_id}/hosts', token)
                hosts = r.json()
                registered = [h for h in hosts if h.get('status') not in ('', None, 'disconnected')]
                known_count = len(registered)
                log(f'  {known_count}/{total_nodes} node(s) discovered')
                _job_set(job_id, progress=45 + min(known_count, total_nodes) * 3)

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
            token = _get_access_token(cfg['pull_secret'])
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
            token = _get_access_token(cfg['pull_secret'])
            _ai('POST', f'/clusters/{cluster_id}/actions/install', token)
            log('Installation triggered ✓')
        except Exception as e:
            fail(f'Failed to trigger installation: {e}')
            return

        # ── Step 8: Monitor installation ──────────────────────────────────────
        phase('Installing OpenShift', 65)
        log('Installation in progress — this takes 45–90 minutes…')
        deadline = time.time() + 2 * 3600  # 2 hours
        last_status = ''
        last_pct    = 0

        PHASE_PCT = {
            'preparing-for-installation': 65,
            'installing':                 70,
            'installing-in-progress':     70,
            'finalizing':                 88,
            'installed':                  100,
        }

        while time.time() < deadline:
            try:
                token = _get_access_token(cfg['pull_secret'])
                r = _ai('GET', f'/clusters/{cluster_id}', token)
                cluster_data = r.json()
                status     = cluster_data.get('status', '')
                status_info = cluster_data.get('status_info', '')
                install_pct = cluster_data.get('progress', {}).get('total_percentage', 0)

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

            except Exception as e:
                log(f'  Monitoring error (retrying): {e}', 'warn')

            time.sleep(30)
        else:
            fail('Installation timed out after 2 hours.')
            return

        # ── Step 9: Fetch credentials ─────────────────────────────────────────
        phase('Collecting credentials', 98)
        result = {}
        try:
            token = _get_access_token(cfg['pull_secret'])
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
