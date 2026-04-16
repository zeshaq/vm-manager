"""
OpenShift package — all Flask route handlers.
"""

import json
import os
import subprocess
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

import requests as _req
from flask import Blueprint, jsonify, request, session, send_file

from .constants import WORK_DIR, _LIBVIRT, _PSUTIL
from .job_store import (
    _jobs, _lock, _running_jobs, _stop_jobs,
    _job_log, _job_set, _job_event,
)
from .secrets import _store_job_secrets, _get_job_secrets, _delete_job_secrets
from .iso_cache import _iso_cache, _iso_lock, _iso_fingerprint, _get_cached_iso, _store_iso_cache, _save_iso_cache
from .ai_client import _get_access_token, _ai
from .vm_ops import _eject_cdroms, _insert_cdroms, _reboot_vms
from .deploy import _run_deploy, _parse_host_mac
from .monitoring import _monitor_install_thread, _collect_credentials

if _LIBVIRT:
    import libvirt

if _PSUTIL:
    import psutil

ocp_bp = Blueprint('openshift', __name__)


def _auth():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    return None


# ── OCP version list ──────────────────────────────────────────────────────────

# Hardcoded fallback — updated to latest as of 2026-04; regenerated at runtime
_FALLBACK_VERSIONS = ['4.21', '4.20', '4.19', '4.18', '4.17', '4.16', '4.15', '4.14']
_versions_cache: dict = {}   # { versions: [...], fetched_at: float }


def _fetch_versions_from_mirror() -> list:
    """Scrape available stable-X.Y channels from the OCP public mirror."""
    import re
    resp = _req.get(
        'https://mirror.openshift.com/pub/openshift-v4/clients/ocp/',
        timeout=10,
    )
    resp.raise_for_status()
    channels = re.findall(r'stable-(\d+\.\d+)', resp.text)
    unique = sorted(set(channels), key=lambda v: tuple(int(x) for x in v.split('.')), reverse=True)
    return unique


# FEATURE: deploy-api

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


def _host_bridges():
    """Return host-level Linux bridges not managed by libvirt."""
    import socket, struct, json as _json
    bridges = []
    try:
        result = subprocess.run(['ip', '-j', 'addr'], capture_output=True, text=True, timeout=5)
        ifaces = _json.loads(result.stdout) if result.returncode == 0 else []
    except Exception:
        ifaces = []

    for iface in ifaces:
        ifname = iface.get('ifname', '')
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

    if _LIBVIRT:
        try:
            conn = libvirt.open('qemu:///system')
            libvirt_bridges = set()
            for net in conn.listAllNetworks(0):
                try:
                    xml_str = net.XMLDesc(0)
                    root    = ET.fromstring(xml_str)
                    name    = net.name()
                    active  = net.isActive() == 1

                    bridge_el = root.find('bridge')
                    bridge    = bridge_el.get('name', '') if bridge_el is not None else ''
                    if bridge:
                        libvirt_bridges.add(bridge)

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

            for hb in _host_bridges():
                if hb['bridge'] not in libvirt_bridges:
                    nets.append(hb)

        except Exception as e:
            return jsonify({'networks': nets, 'error': str(e)})
    else:
        nets = _host_bridges()

    return jsonify({'networks': nets})


@ocp_bp.route('/api/openshift/preflight')
def preflight():
    err = _auth()
    if err:
        return err

    checks = {}

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

    if _PSUTIL:
        disk = psutil.disk_usage('/')
        checks['disk_free_gb'] = round(disk.free / 1024 ** 3, 1)
        checks['disk_ok'] = disk.free > 50 * 1024 ** 3
        mem = psutil.virtual_memory()
        checks['ram_free_gb']  = round(mem.available / 1024 ** 3, 1)
        checks['ram_ok']       = mem.available > 16 * 1024 ** 3
    else:
        checks['disk_free_gb'] = None
        checks['disk_ok']      = None
        checks['ram_free_gb']  = None
        checks['ram_ok']       = None

    try:
        _req.head('https://api.openshift.com', timeout=5)
        checks['internet'] = True
    except Exception:
        checks['internet'] = False

    return jsonify(checks)


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
        from .job_store import _save_jobs
        _save_jobs()

    # Persist credentials separately (0600 file) so the job can be resumed
    # if the service restarts mid-deployment.
    _store_job_secrets(job_id, cfg['offline_token'], cfg['pull_secret'])

    _running_jobs.add(job_id)
    t = threading.Thread(target=_run_deploy, args=(job_id, cfg), daemon=True,
                         name=f'ocp-deploy-{job_id}')
    t.start()
    return jsonify({'job_id': job_id})


# FEATURE: job-management-api

@ocp_bp.route('/api/openshift/jobs')
def list_jobs():
    err = _auth()
    if err:
        return err
    jobs = sorted(_jobs.values(), key=lambda j: j['created'], reverse=True)
    return jsonify({'jobs': [{**j, 'logs': j['logs'][-5:]} for j in jobs]})


@ocp_bp.route('/api/openshift/jobs/<job_id>')
def job_detail(job_id):
    err = _auth()
    if err:
        return err
    job = _jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
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
                    pass
            conn.close()
        except Exception:
            pass

        for vm_name in vm_names:
            for disk in storage_path.glob(f'{vm_name}*.qcow2'):
                try:
                    disk.unlink()
                    deleted_disks.append(str(disk))
                except OSError:
                    pass

    cluster_id    = job.get('cluster_id')
    infra_env_id  = job.get('infra_env_id')
    secrets       = _get_job_secrets(job_id)
    offline_token = secrets.get('offline_token', '')
    ai_deleted    = []

    if offline_token and (cluster_id or infra_env_id):
        try:
            token = _get_access_token(offline_token)
            if infra_env_id:
                try:
                    _ai('DELETE', f'/infra-envs/{infra_env_id}', token)
                    ai_deleted.append(f'infra-env:{infra_env_id}')
                except Exception as e:
                    ai_deleted.append(f'infra-env:warn:{e}')
            if cluster_id:
                try:
                    _ai('DELETE', f'/clusters/{cluster_id}', token)
                    ai_deleted.append(f'cluster:{cluster_id}')
                except Exception as e:
                    ai_deleted.append(f'cluster:warn:{e}')
        except Exception as e:
            ai_deleted.append(f'auth:warn:{e}')

    job_dir = WORK_DIR / job_id
    try:
        iso = job_dir / 'discovery.iso'
        if iso.exists():
            iso.unlink()
    except Exception:
        pass

    with _lock:
        _jobs.pop(job_id, None)
        from .job_store import _save_jobs
        _save_jobs()

    _delete_job_secrets(job_id)

    return jsonify({'ok': True, 'deleted_vms': deleted_vms,
                    'deleted_disks': deleted_disks, 'ai_deleted': ai_deleted})


# FEATURE: cluster-sync-api

@ocp_bp.route('/api/openshift/jobs/<job_id>/sync', methods=['POST'])
def sync_job(job_id):
    """Sync a stuck/pending job with the real state in the Assisted Installer."""
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

    stored        = _get_job_secrets(job_id)
    data          = request.get_json(silent=True) or {}
    offline_token = stored.get('offline_token') or data.get('offline_token', '').strip()
    pull_secret   = stored.get('pull_secret')   or data.get('pull_secret', '').strip()

    if not offline_token or not pull_secret:
        return jsonify({'error': 'no_stored_credentials',
                        'message': 'No stored credentials for this job — please provide offline_token and pull_secret'}), 400

    cfg = {**job.get('config', {}), 'offline_token': offline_token, 'pull_secret': pull_secret}
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
        _job_set(job_id, status='pending', phase='Resuming deployment')
        _running_jobs.add(job_id)
        threading.Thread(target=_run_deploy, args=(job_id, cfg),
                         daemon=True, name=f'ocp-sync-{job_id}').start()
        return jsonify({'action': 'full_resume', 'ai_status': ai_status})


@ocp_bp.route('/api/openshift/jobs/<job_id>/retry', methods=['POST'])
def retry_job(job_id):
    """Retry a failed deployment."""
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
        r = _ai('GET', f'/clusters/{cluster_id}', token)
        ai_status = r.json().get('status', '')
    except Exception as e:
        return jsonify({'error': f'Could not reach Assisted Installer: {e}'}), 502

    _job_log(job_id, '── Retry: resetting cluster ──', 'warn')
    _job_log(job_id, f'  AI cluster status before reset: {ai_status}')

    try:
        _CANCELLABLE = {'installing', 'installing-in-progress', 'installing-pending-user-action',
                        'finalizing', 'error'}
        if ai_status in _CANCELLABLE:
            rc = _ai('POST', f'/clusters/{cluster_id}/actions/cancel', token, body={})
            _job_log(job_id, f'  Cancel: HTTP {rc.status_code}')
            time.sleep(1)

        rr = _ai('POST', f'/clusters/{cluster_id}/actions/reset', token, body={})
        if rr.status_code not in (200, 201, 202):
            return jsonify({'error': f'Reset failed: HTTP {rr.status_code} — {rr.text[:200]}'}), 502
        _job_log(job_id, f'  Cluster reset ✓ (HTTP {rr.status_code})')

    except Exception as e:
        return jsonify({'error': f'Reset failed: {e}'}), 502

    vm_names = job.get('vms', [])
    if vm_names:
        _job_log(job_id, '  Re-inserting discovery ISO into VMs…')
        _insert_cdroms(vm_names, iso_path, _log)
        time.sleep(1)
        _job_log(job_id, '  Rebooting VMs into discovery mode…')
        _reboot_vms(vm_names, _log)
    else:
        _job_log(job_id, '  No VM list found — skipping ISO reinsert', 'warn')

    _job_set(job_id, status='pending', phase='Waiting for nodes (retry)', progress=45)
    _running_jobs.add(job_id)
    threading.Thread(target=_run_deploy, args=(job_id, cfg),
                     daemon=True, name=f'ocp-retry-{job_id}').start()

    return jsonify({'action': 'retrying', 'ai_status': ai_status, 'vms_rebooted': len(vm_names)})


@ocp_bp.route('/api/openshift/jobs/<job_id>/reset', methods=['POST'])
def reset_cluster(job_id):
    """Reset or reinstall a cluster."""
    err = _auth()
    if err:
        return err

    job = _jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    cluster_id   = job.get('cluster_id')
    infra_env_id = job.get('infra_env_id')
    iso_path     = job.get('iso_path')
    job_status   = job.get('status', '')

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

    if job_id in _running_jobs:
        _stop_jobs.add(job_id)
        _log('── Reset requested — signalling active thread to stop ──', 'warn')
        time.sleep(1)
        _running_jobs.discard(job_id)

    # ══ COMPLETE JOB: full teardown + fresh deploy ════════════════════════════
    if job_status == 'complete':
        _log('── Full teardown for reinstall ──', 'warn')

        try:
            token = _get_access_token(offline_token)
        except Exception as e:
            return jsonify({'error': f'Token exchange failed: {e}'}), 502

        if cluster_id:
            try:
                try:
                    _ai('POST', f'/clusters/{cluster_id}/actions/cancel', token, body={})
                except Exception:
                    pass
                _ai('DELETE', f'/clusters/{cluster_id}', token)
                _log(f'  Deleted AI cluster {cluster_id} ✓')
            except Exception as e:
                _log(f'  AI cluster delete warning: {e}', 'warn')

        if infra_env_id:
            try:
                _ai('DELETE', f'/infra-envs/{infra_env_id}', token)
                _log(f'  Deleted AI infra-env {infra_env_id} ✓')
            except Exception as e:
                _log(f'  AI infra-env delete warning: {e}', 'warn')

        vm_names     = job.get('vms', [])
        storage_path = Path(job.get('config', {}).get('storage_path', '/var/lib/libvirt/images'))
        destroyed_vms, destroyed_disks = [], []

        if vm_names:
            try:
                conn = libvirt.open('qemu:///system')
                for vm_name in vm_names:
                    try:
                        dom = conn.lookupByName(vm_name)
                        if dom.isActive():
                            dom.destroy()
                        dom.undefine()
                        destroyed_vms.append(vm_name)
                        _log(f'  Destroyed VM {vm_name} ✓')
                    except libvirt.libvirtError:
                        pass
                conn.close()
            except Exception as e:
                _log(f'  libvirt warning: {e}', 'warn')

            for vm_name in vm_names:
                for disk in storage_path.glob(f'{vm_name}*.qcow2'):
                    try:
                        disk.unlink()
                        destroyed_disks.append(disk.name)
                        _log(f'  Removed disk {disk.name} ✓')
                    except OSError as e:
                        _log(f'  Disk remove warning: {e}', 'warn')

        now_ts = time.strftime('%H:%M:%S')
        _job_set(job_id,
            status='pending', phase='Queued for reinstall', progress=0,
            cluster_id=None, infra_env_id=None, iso_path=None, vms=[], result=None,
            logs=[{'ts': now_ts, 'msg': '── Reinstall triggered ──', 'level': 'warn'}],
        )

        cfg = {**job.get('config', {}), 'offline_token': offline_token, 'pull_secret': pull_secret}
        _running_jobs.add(job_id)
        threading.Thread(target=_run_deploy, args=(job_id, cfg),
                         daemon=True, name=f'ocp-reinstall-{job_id}').start()

        return jsonify({'action': 'reinstall',
                        'destroyed_vms': destroyed_vms,
                        'destroyed_disks': destroyed_disks})

    # ══ NON-COMPLETE JOB: cancel/reset in AI, reboot VMs into discovery ════════
    if not cluster_id:
        return jsonify({'error': 'No cluster_id recorded — job must be restarted from scratch'}), 400

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


# FEATURE: iso-management-api

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

            from .constants import ISO_CACHE_DIR
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
        except Exception:
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
    return send_file(str(kc_path), as_attachment=True,
                     download_name='kubeconfig', mimetype='text/plain')


# FEATURE: cluster-live-status-api

def _run_kubectl(kubeconfig: Path, args: list, timeout: int = 15) -> dict:
    """Run kubectl/oc with the given kubeconfig and return parsed JSON."""
    import shutil
    kubectl = shutil.which('kubectl') or shutil.which('oc')
    if not kubectl:
        return {'ok': False, 'error': 'kubectl / oc not found in PATH'}
    cmd = [kubectl, f'--kubeconfig={kubeconfig}'] + args
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            env={**os.environ, 'KUBECONFIG': str(kubeconfig)},
        )
        if result.returncode != 0:
            return {'ok': False, 'error': result.stderr.strip()[:500]}
        return {'ok': True, 'data': json.loads(result.stdout)}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': f'kubectl timed out after {timeout}s'}
    except json.JSONDecodeError as e:
        return {'ok': False, 'error': f'JSON parse error: {e}'}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


@ocp_bp.route('/api/openshift/jobs/<job_id>/cluster')
def cluster_status(job_id):
    """Live cluster status via kubectl: nodes, cluster operators, version."""
    err = _auth()
    if err:
        return err

    job = _jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    kc_path = WORK_DIR / job_id / 'kubeconfig'
    if not kc_path.exists():
        kc_path_alt = Path(job.get('result', {}).get('kubeconfig_path', ''))
        if kc_path_alt.exists():
            kc_path = kc_path_alt
        else:
            return jsonify({'error': 'kubeconfig not available — cluster may still be deploying'}), 404

    payload: dict = {'nodes': [], 'operators': [], 'version': None, 'errors': []}

    r = _run_kubectl(kc_path, ['get', 'nodes', '-o', 'json'], timeout=15)
    if r['ok']:
        for item in r['data'].get('items', []):
            labels = item.get('metadata', {}).get('labels', {})
            roles  = sorted([
                k.split('/')[-1]
                for k in labels if k.startswith('node-role.kubernetes.io/')
            ]) or ['worker']
            ready = 'Unknown'
            for cond in item.get('status', {}).get('conditions', []):
                if cond.get('type') == 'Ready':
                    ready = 'Ready' if cond.get('status') == 'True' else 'NotReady'
                    break
            payload['nodes'].append({
                'name':            item['metadata']['name'],
                'roles':           roles,
                'ready':           ready,
                'kubelet_version': item.get('status', {}).get('nodeInfo', {}).get('kubeletVersion', ''),
            })
    else:
        payload['errors'].append(f'nodes: {r["error"]}')

    r = _run_kubectl(kc_path, ['get', 'clusteroperators', '-o', 'json'], timeout=20)
    if r['ok']:
        for item in r['data'].get('items', []):
            conds = {c['type']: c for c in item.get('status', {}).get('conditions', [])}
            payload['operators'].append({
                'name':        item['metadata']['name'],
                'available':   conds.get('Available',   {}).get('status', 'Unknown'),
                'progressing': conds.get('Progressing', {}).get('status', 'Unknown'),
                'degraded':    conds.get('Degraded',    {}).get('status', 'Unknown'),
                'message':     (conds.get('Degraded')    or conds.get('Progressing') or {}).get('message', ''),
            })
    else:
        payload['errors'].append(f'clusteroperators: {r["error"]}')

    r = _run_kubectl(kc_path, ['get', 'clusterversion', 'version', '-o', 'json'], timeout=15)
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
