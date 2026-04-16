"""
OpenShift package — installation monitoring thread and credential collection.
"""

import time
from pathlib import Path

from .job_store import (
    _jobs, _running_jobs, _stop_jobs,
    _job_log, _job_set, _job_event,
)
from .secrets import _delete_job_secrets
from .ai_client import _get_access_token, _ai
from .vm_ops import _eject_cdroms, _reboot_vms
from .deploy import _parse_host_mac
from .constants import WORK_DIR


# FEATURE: collect-credentials

def _collect_credentials(job_id: str, cluster_id: str, cluster_name: str,
                          base_domain: str, token: str):
    """Fetch kubeconfig + kubeadmin password and mark the job complete."""
    def log(msg, level='info'):
        _job_log(job_id, msg, level)

    job_dir = WORK_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    _job_set(job_id, phase='Collecting credentials', progress=98)

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


# FEATURE: install-monitoring

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

    # mac_map: vm_name → MAC — used to match API hosts back to libvirt VM names
    mac_map = _jobs.get(job_id, {}).get('mac_map', {})
    mac_to_vm = {mac.lower(): vm for vm, mac in mac_map.items()}

    try:
        deadline = time.time() + 2 * 3600
        last_status = ''
        last_pct    = 0
        pending_handled: dict = {}   # host_id → last handled timestamp
        host_stages:     dict = {}   # host_id → last known stage
        host_stuck_warn: dict = {}   # host_id → last stuck warning timestamp
        seen_operators:  set  = set()
        consecutive_errors    = 0

        POLL_INTERVAL = {
            'preparing-for-installation':     30,
            'installing':                     15,
            'installing-in-progress':         15,
            'installing-pending-user-action': 10,
            'finalizing':                     20,
        }

        # FEATURE: stuck-detection
        STUCK_THRESHOLD = 15 * 60
        STUCK_REWARN    = 10 * 60

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
                    if status != last_status:
                        _job_event(job_id, 'status_change', status=status,
                                   msg=status_info, pct=install_pct)
                    last_status = status
                    last_pct    = install_pct
                    pct = PHASE_PCT.get(status, 70) + int(install_pct * 0.25)
                    _job_set(job_id, progress=min(pct, 98),
                             phase=f'Installing OpenShift ({install_pct}%)')

                # FEATURE: operator-tracking
                # ── Operator tracking (finalizing phase) ───────────────────────
                if status == 'finalizing':
                    for op in cluster_data.get('monitored_operators', []):
                        op_name   = op.get('name', '')
                        op_status = op.get('status', '')
                        op_key    = f'{op_name}:{op_status}'
                        if op_status == 'available' and op_key not in seen_operators:
                            seen_operators.add(op_key)
                            log(f'  ✓ Operator available: {op_name}')
                            _job_event(job_id, 'operator_available', operator=op_name)
                        elif op_status not in ('', 'available') and f'{op_name}:warn' not in seen_operators:
                            seen_operators.add(f'{op_name}:warn')
                            _job_event(job_id, 'operator_update', operator=op_name,
                                       status=op_status, msg=op.get('status_info', ''))
                    _job_set(job_id, ai_operators=[
                        {'name': op.get('name'), 'status': op.get('status'),
                         'msg': op.get('status_info', '')}
                        for op in cluster_data.get('monitored_operators', [])
                    ])

                if status == 'installed':
                    _collect_credentials(job_id, cluster_id, cluster_name, base_domain, token)
                    return
                if status in ('error', 'cancelled'):
                    try:
                        hr = _ai('GET', f'/clusters/{cluster_id}/hosts', token)
                        for h in hr.json():
                            if h.get('status') in ('error', 'installing-pending-user-action'):
                                hname = h.get('requested_hostname') or h['id'][:8]
                                log(f'  ✗ Host {hname}: {h.get("status_info", "no detail")}', 'error')
                                _job_event(job_id, 'host_failed', node=hname,
                                           msg=h.get('status_info', ''))
                    except Exception:
                        pass
                    _job_set(job_id, status='failed', phase='Failed')
                    log(f'Installation {status}: {status_info}', 'error')
                    return

                # ── Per-host stage + stuck + pending-user-action ───────────────
                try:
                    hr = _ai('GET', f'/clusters/{cluster_id}/hosts', token)
                    now = time.time()
                    nodes_payload = []
                    for h in hr.json():
                        h_status = h.get('status', '')
                        h_id     = h.get('id', '')
                        hostname = h.get('requested_hostname') or ''
                        prog     = h.get('progress', {})
                        stage    = prog.get('current_stage', '')
                        stage_ts = prog.get('stage_updated_at', '')
                        pct_h    = prog.get('installation_percentage', 0)

                        # Resolve display name: prefer MAC-matched VM name,
                        # then requested_hostname, then UUID prefix as last resort
                        vm_name = _parse_host_mac(h, mac_to_vm)
                        display_name = vm_name or hostname or h_id[:8]

                        nodes_payload.append({
                            'id':       h_id,
                            'name':     display_name,
                            'vm':       vm_name or '',
                            'hostname': hostname,
                            'status':   h_status,
                            'stage':    stage,
                            'pct':      pct_h,
                            'stage_ts': stage_ts,
                            'role':     h.get('role', ''),
                        })

                        # Stage change detection
                        prev_stage = host_stages.get(h_id)
                        if stage and stage != prev_stage:
                            host_stages[h_id] = stage
                            if prev_stage is not None:
                                log(f'  [{display_name}] {prev_stage or "—"} → {stage}')
                                _job_event(job_id, 'stage_change', node=display_name,
                                           from_stage=prev_stage, to_stage=stage)
                            else:
                                host_stages[h_id] = stage

                        # FEATURE: stuck-detection (per-host)
                        if stage_ts and stage:
                            try:
                                import datetime as _dt
                                ts_val = stage_ts.rstrip('Z')
                                if '.' in ts_val:
                                    ts_val = ts_val[:26]
                                stage_age = now - _dt.datetime.fromisoformat(ts_val).timestamp()
                                last_warn = host_stuck_warn.get(h_id, 0)
                                if stage_age > STUCK_THRESHOLD and (now - last_warn) > STUCK_REWARN:
                                    host_stuck_warn[h_id] = now
                                    mins = int(stage_age // 60)
                                    log(f'  ⚠ [{display_name}] stuck in "{stage}" for {mins}m', 'warn')
                                    _job_event(job_id, 'stuck', node=display_name,
                                               stage=stage, minutes=mins)
                            except Exception:
                                pass

                        # FEATURE: pending-user-action-recovery
                        # pending-user-action: eject + reboot (5-min cooldown)
                        if 'pending-user-action' in h_status:
                            last_t = pending_handled.get(h_id, 0)
                            if now - last_t >= 300:
                                vm_match = vm_name or next(
                                    (v for v in vm_names
                                     if v == hostname or (hostname and (hostname.startswith(v) or v in hostname))),
                                    None
                                )
                                if not vm_match:
                                    log(f'  ⚠ {display_name} pending-user-action but VM not identified', 'warn')
                                    pending_handled[h_id] = now
                                else:
                                    log(f'  ⚠ {display_name} rebooted into ISO — ejecting and rebooting {vm_match}…', 'warn')
                                    _job_event(job_id, 'pending_user_action', node=display_name, vm=vm_match)
                                    _eject_cdroms([vm_match], log)
                                    time.sleep(2)
                                    _reboot_vms([vm_match], log)
                                    pending_handled[h_id] = now

                    _job_set(job_id, nodes=nodes_payload)
                except Exception:
                    pass

            except Exception as e:
                consecutive_errors += 1
                if consecutive_errors == 1:
                    log(f'  Monitoring error (retrying): {e}', 'warn')
                wait = min(30 * (2 ** min(consecutive_errors - 1, 4)), 300)
                time.sleep(wait)
                continue

            time.sleep(POLL_INTERVAL.get(status, 30))
        else:
            _job_set(job_id, status='failed', phase='Failed')
            log('Installation monitoring timed out after 2 hours.', 'error')

    except Exception as e:
        _job_set(job_id, status='failed', phase='Failed')
        _job_log(job_id, f'Unexpected error in monitor thread: {e}', 'error')
    finally:
        _running_jobs.discard(job_id)
