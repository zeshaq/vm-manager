"""
OpenShift package — background deployment worker and startup resume logic.
"""

import json
import os
import subprocess
import threading
import time
from pathlib import Path

from .constants import WORK_DIR, _LIBVIRT
from .job_store import (
    _jobs, _lock, _running_jobs, _stop_jobs,
    _job_log, _job_set, _job_event,
)
from .secrets import _store_job_secrets, _get_job_secrets, _delete_job_secrets
from .iso_cache import _iso_fingerprint, _get_cached_iso, _store_iso_cache
from .ai_client import _get_access_token, _ai
from .vm_ops import _make_mac, _vm_xml, _build_nmstate_yaml, _eject_cdroms, _reboot_vms

import requests as _req

if _LIBVIRT:
    import libvirt


# FEATURE: host-mac-parse

def _parse_host_mac(host: dict, mac_to_vm: dict):
    """Return the VM name matching any NIC MAC in host's inventory, or None."""
    raw_inv = host.get('inventory') or '{}'
    if isinstance(raw_inv, str):
        try:
            inv = json.loads(raw_inv)
        except Exception:
            inv = {}
    else:
        inv = raw_inv
    nics = inv.get('interfaces') or inv.get('nics') or []
    for nic in nics:
        mac = (nic.get('mac_address') or nic.get('macAddress') or '').lower()
        if mac in mac_to_vm:
            return mac_to_vm[mac]
    return None


# FEATURE: vm-provisioning

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
            # Always create a fresh infra-env and download a new ISO for every deployment.
            cached_infra_env_id, cached_iso_path = None, None

            infra_payload = {
                'name':              f'{cluster_name}-infra',
                'cluster_id':        cluster_id,
                'openshift_version': cfg['ocp_version'],
                'pull_secret':       cfg['pull_secret'],
                'image_type':        'full-iso',
                'cpu_architecture':  'x86_64',
            }
            if cfg.get('ssh_public_key', '').strip():
                infra_payload['ssh_authorized_key'] = cfg['ssh_public_key'].strip()

            # FEATURE: iso-management
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
                iso_path = job_dir / 'discovery.iso'
                iso_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    r = _ai('GET', f'/infra-envs/{infra_env_id}/downloads/image-url', token)
                    iso_url = r.json()['url']
                    log(f'ISO URL obtained, downloading full ISO…')
                    with _req.get(iso_url, stream=True, timeout=600) as dl:
                        dl.raise_for_status()
                        total    = int(dl.headers.get('content-length', 0))
                        done     = 0
                        t_start  = time.time()
                        t_last   = t_start
                        spd_bytes = 0
                        with open(iso_path, 'wb') as f:
                            for chunk in dl.iter_content(chunk_size=1024 * 1024):
                                f.write(chunk)
                                n = len(chunk)
                                done      += n
                                spd_bytes += n
                                now = time.time()
                                if now - t_last >= 1.0:
                                    speed = spd_bytes / (now - t_last) / 1_048_576
                                    pct   = int(done / total * 100) if total else 0
                                    eta_s = int((total - done) / (done / (now - t_start))) if done > 0 and total > done else 0
                                    _job_set(job_id,
                                             progress=22 + int(pct / 10),
                                             iso_dl={
                                                 'pct':       pct,
                                                 'speed_mbs': round(speed, 1),
                                                 'done_mb':   round(done / 1_048_576, 1),
                                                 'total_mb':  round(total / 1_048_576, 1),
                                                 'eta_s':     eta_s,
                                             })
                                    t_last    = now
                                    spd_bytes = 0
                    _job_set(job_id, iso_dl=None)
                    log(f'ISO downloaded ✓')
                    os.chmod(iso_path, 0o644)
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

            _job_set(job_id, vms=created_vms, mac_map=mac_map)

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
                # Do NOT eject/reboot here — the monitor loop handles per-host
                # pending-user-action with proper MAC matching. Blanket eject here
                # would interrupt nodes that are mid-install and haven't rebooted yet.
                from .monitoring import _monitor_install_thread
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

        # FEATURE: hostname-assignment
        # Set host roles + hostnames for multi-node.
        # Match each registered host to its VM via MAC address so the hostname
        # in OpenShift matches the libvirt VM name exactly.
        if not is_sno:
            phase('Assigning node roles', 55)
            token = _get_access_token(cfg['offline_token'])
            r = _ai('GET', f'/clusters/{cluster_id}/hosts', token)
            hosts = r.json()

            # Build MAC → vm_name lookup from mac_map
            mac_to_vm = {mac.lower(): vm for vm, mac in mac_map.items()}

            # Sort hosts by discovery time for stable fallback ordering
            hosts_sorted = sorted(hosts, key=lambda h: h.get('created_at', ''))

            for idx, host in enumerate(hosts_sorted):
                host_id  = host['id']
                url      = f'/infra-envs/{infra_env_id}/hosts/{host_id}'
                vm_name  = _parse_host_mac(host, mac_to_vm)

                # Fallback: assign by index if MAC didn't match
                if not vm_name and idx < len(vm_names):
                    vm_name = vm_names[idx]
                    log(f'  Host {host_id[:8]}: MAC not matched — using index fallback → {vm_name}', 'warn')
                else:
                    log(f'  Host {host_id[:8]}: MAC matched → {vm_name}')

                role = 'worker' if (vm_name in vm_names[n_control:]) else 'master'

                # Assign role
                try:
                    _ai('PATCH', url, token, {'host_role': role})
                    log(f'  Role: {vm_name} → {role} ✓')
                except Exception as e:
                    log(f'  Role assignment warning for {vm_name}: {e}', 'warn')

                # Assign hostname — retry up to 3x with 5s delay
                if vm_name:
                    assigned = False
                    for attempt in range(3):
                        try:
                            resp = _ai('PATCH', url, token, {'requested_hostname': vm_name})
                            if resp.status_code < 300:
                                log(f'  Hostname: {vm_name} ✓')
                                assigned = True
                                break
                            else:
                                log(f'  Hostname PATCH attempt {attempt+1} failed: HTTP {resp.status_code} — {resp.text[:120]}', 'warn')
                        except Exception as e:
                            log(f'  Hostname PATCH attempt {attempt+1} error: {e}', 'warn')
                        time.sleep(5)
                    if not assigned:
                        log(f'  Could not assign hostname for {vm_name} after 3 attempts', 'warn')

        # FEATURE: hostname-verification
        # Poll until all hosts reflect the correct VM name.
        # Re-patch any host that still shows the wrong hostname.
        if not is_sno:
            phase('Verifying hostname assignments', 58)
            log('Verifying hostnames are applied…')
            mac_to_vm = {mac.lower(): vm for vm, mac in mac_map.items()}

            for poll in range(12):   # up to 60 seconds
                token = _get_access_token(cfg['offline_token'])
                r = _ai('GET', f'/clusters/{cluster_id}/hosts', token)
                hosts_now = r.json()

                wrong = []
                for host in hosts_now:
                    host_id      = host['id']
                    current_name = host.get('requested_hostname', '')
                    vm_name      = _parse_host_mac(host, mac_to_vm)
                    if not vm_name:
                        # fallback by current name already matching a vm name
                        vm_name = next((v for v in vm_names if v == current_name), None)
                    if vm_name and current_name != vm_name:
                        wrong.append((host_id, vm_name, current_name))

                if not wrong:
                    log('  All hostnames verified ✓')
                    break

                log(f'  {len(wrong)} host(s) still have wrong hostname — re-patching…')
                for host_id, vm_name, current_name in wrong:
                    url = f'/infra-envs/{infra_env_id}/hosts/{host_id}'
                    try:
                        resp = _ai('PATCH', url, token, {'requested_hostname': vm_name})
                        if resp.status_code < 300:
                            log(f'  Re-patched: {current_name or host_id[:8]} → {vm_name} ✓')
                        else:
                            log(f'  Re-patch failed for {vm_name}: HTTP {resp.status_code}', 'warn')
                    except Exception as e:
                        log(f'  Re-patch error for {vm_name}: {e}', 'warn')
                time.sleep(5)
            else:
                log('  Hostname verification timed out — continuing anyway', 'warn')

        # Build MAC → vm_name reverse lookup unconditionally (used in monitoring loop)
        mac_to_vm = {mac.lower(): vm for vm, mac in mac_map.items()}

        # ── Step 7: Start installation ────────────────────────────────────────
        phase('Starting OpenShift installation', 60)
        try:
            token = _get_access_token(cfg['offline_token'])
            _ai('POST', f'/clusters/{cluster_id}/actions/install', token)
            log('Installation triggered ✓')
        except Exception as e:
            fail(f'Failed to trigger installation: {e}')
            return

        # ── Step 8: Monitor installation ──────────────────────────────────────
        # ISO is intentionally left in all VMs throughout installation.
        # Nodes read RHCOS from the ISO during install — ejecting early causes
        # "Unable to read from discovery media" failures.
        # ISO is only ejected per-host when it gets pending-user-action status,
        # which means RHCOS is already written and the node rebooted back into ISO.
        phase('Installing OpenShift', 65)
        log('Installation in progress — this takes 45–90 minutes…')
        _job_event(job_id, 'status_change', status='installing', msg='Installation started')

        deadline = time.time() + 2 * 3600  # 2 hours
        last_status    = ''
        last_pct       = 0
        pending_handled: dict = {}   # host_id → last handled timestamp
        host_stages:     dict = {}   # host_id → last known stage
        host_stuck_warn: dict = {}   # host_id → last stuck warning timestamp
        seen_operators:  set  = set()

        PHASE_PCT = {
            'preparing-for-installation':     65,
            'installing':                     70,
            'installing-in-progress':         70,
            'installing-pending-user-action': 72,
            'finalizing':                     88,
            'installed':                      100,
        }
        POLL_INTERVAL = {
            'preparing-for-installation': 30,
            'installing':                 15,
            'installing-in-progress':     15,
            'installing-pending-user-action': 10,
            'finalizing':                 20,
        }
        STUCK_THRESHOLD  = 15 * 60   # 15 min without stage change = stuck
        STUCK_REWARN     = 10 * 60   # re-warn every 10 min after first alert

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
                    if status != last_status:
                        _job_event(job_id, 'status_change', status=status,
                                   msg=status_info, pct=install_pct)
                    last_status = status
                    last_pct    = install_pct
                    pct = PHASE_PCT.get(status, 70) + int(install_pct * 0.25)
                    _job_set(job_id, progress=min(pct, 98),
                             phase=f'Installing OpenShift ({install_pct}%)')

                # ── Track monitored operators (finalizing phase) ───────────────
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
                    ops_payload = [
                        {'name': op.get('name'), 'status': op.get('status'),
                         'msg': op.get('status_info', '')}
                        for op in cluster_data.get('monitored_operators', [])
                    ]
                    _job_set(job_id, ai_operators=ops_payload)

                if status == 'installed':
                    break
                if status in ('error', 'cancelled'):
                    # Log per-host failure details before giving up
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
                    fail(f'Installation {status}: {status_info}')
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
                                log(f'  [{hostname}] {prev_stage or "—"} → {stage}')
                                _job_event(job_id, 'stage_change', node=hostname,
                                           from_stage=prev_stage, to_stage=stage)
                            else:
                                host_stages[h_id] = stage

                        # Stuck node detection (15 min without stage change)
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
                                    log(f'  ⚠ [{hostname}] stuck in "{stage}" for {mins}m', 'warn')
                                    _job_event(job_id, 'stuck', node=hostname,
                                               stage=stage, minutes=mins)
                            except Exception:
                                pass

                        # pending-user-action: eject ISO + reboot (5-min cooldown)
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

            time.sleep(POLL_INTERVAL.get(status, 30))
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


# FEATURE: resume-pending-jobs

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
