"""Physical Server management via BMC / iLO Redfish API.

Proxies all Redfish calls through the backend so credentials never
reach the browser and self-signed iLO certificates are handled centrally.

Server list is persisted in ~/hypercloud/bmc_servers.json.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

import requests
import urllib3
from flask import Blueprint, jsonify, request, session

# Suppress self-signed cert warnings from iLO
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

bmc_bp = Blueprint('bmc', __name__)

_WORK_DIR    = Path.home() / 'hypercloud'
_SERVERS_FILE = _WORK_DIR / 'bmc_servers.json'

REDFISH_TIMEOUT = 10   # seconds per request

# ── Auth ──────────────────────────────────────────────────────────────────────

def _auth():
    from flask import session
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    return None

# ── Server persistence ────────────────────────────────────────────────────────

def _load_servers() -> dict:
    try:
        _WORK_DIR.mkdir(parents=True, exist_ok=True)
        if _SERVERS_FILE.exists():
            return json.loads(_SERVERS_FILE.read_text())
    except Exception:
        pass
    return {}

def _save_servers(servers: dict):
    try:
        _WORK_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _SERVERS_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps(servers, indent=2))
        tmp.replace(_SERVERS_FILE)
        _SERVERS_FILE.chmod(0o600)   # credentials — owner only
    except Exception:
        pass

# ── Redfish helpers ───────────────────────────────────────────────────────────

def _rf_get(ilo_ip: str, user: str, password: str, path: str) -> dict:
    """GET a Redfish endpoint. Returns parsed JSON or raises."""
    url = f'https://{ilo_ip}{path}'
    r = requests.get(url, auth=(user, password),
                     verify=False, timeout=REDFISH_TIMEOUT,
                     headers={'Accept': 'application/json'})
    r.raise_for_status()
    return r.json()

def _rf_post(ilo_ip: str, user: str, password: str, path: str, body: dict) -> dict:
    url = f'https://{ilo_ip}{path}'
    r = requests.post(url, auth=(user, password),
                      verify=False, timeout=REDFISH_TIMEOUT,
                      headers={'Content-Type': 'application/json'},
                      json=body)
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        return {'status': r.status_code}


def _get_server_or_404(server_id: str):
    servers = _load_servers()
    srv = servers.get(server_id)
    if not srv:
        return None, jsonify({'error': 'Server not found'}), 404
    return srv, None, None


def _health_color(status: str | None) -> str:
    if not status:
        return 'unknown'
    s = status.lower()
    if s == 'ok':
        return 'ok'
    if s == 'warning':
        return 'warning'
    return 'critical'

# ── Server CRUD ───────────────────────────────────────────────────────────────

@bmc_bp.route('/api/bmc/servers', methods=['GET'])
def list_servers():
    err = _auth()
    if err: return err
    servers = _load_servers()
    return jsonify({'servers': list(servers.values())})


@bmc_bp.route('/api/bmc/servers', methods=['POST'])
def add_server():
    err = _auth()
    if err: return err
    data = request.get_json() or {}
    required = ['name', 'ilo_ip', 'username', 'password']
    missing = [f for f in required if not data.get(f, '').strip()]
    if missing:
        return jsonify({'error': f'Missing: {", ".join(missing)}'}), 400

    servers = _load_servers()
    sid = uuid.uuid4().hex[:8]
    servers[sid] = {
        'id':          sid,
        'name':        data['name'].strip(),
        'ilo_ip':      data['ilo_ip'].strip(),
        'username':    data['username'].strip(),
        'password':    data['password'],
        'description': data.get('description', '').strip(),
        'added':       time.time(),
    }
    _save_servers(servers)
    return jsonify({'id': sid}), 201


@bmc_bp.route('/api/bmc/servers/<server_id>', methods=['PUT'])
def update_server(server_id):
    err = _auth()
    if err: return err
    servers = _load_servers()
    if server_id not in servers:
        return jsonify({'error': 'Server not found'}), 404
    data = request.get_json() or {}
    for field in ['name', 'ilo_ip', 'username', 'password', 'description']:
        if field in data:
            servers[server_id][field] = data[field]
    _save_servers(servers)
    return jsonify({'updated': server_id})


@bmc_bp.route('/api/bmc/servers/<server_id>', methods=['DELETE'])
def delete_server(server_id):
    err = _auth()
    if err: return err
    servers = _load_servers()
    if server_id not in servers:
        return jsonify({'error': 'Server not found'}), 404
    servers.pop(server_id)
    _save_servers(servers)
    return jsonify({'deleted': server_id})

# ── System overview ───────────────────────────────────────────────────────────

@bmc_bp.route('/api/bmc/servers/<server_id>/system')
def server_system(server_id):
    err = _auth()
    if err: return err
    srv, err_resp, code = _get_server_or_404(server_id)
    if err_resp: return err_resp, code

    ilo_ip = srv['ilo_ip']
    user   = srv['username']
    pwd    = srv['password']

    try:
        sys_data = _rf_get(ilo_ip, user, pwd, '/redfish/v1/Systems/1/')
        mgr_data = _rf_get(ilo_ip, user, pwd, '/redfish/v1/Managers/1/')

        proc  = sys_data.get('ProcessorSummary', {})
        mem   = sys_data.get('MemorySummary', {})
        boot  = sys_data.get('Boot', {})
        oem   = sys_data.get('Oem', {}).get('Hpe', {})

        return jsonify({
            'id':              server_id,
            'name':            srv['name'],
            'ilo_ip':          ilo_ip,
            'description':     srv.get('description', ''),
            'model':           sys_data.get('Model', ''),
            'serial':          sys_data.get('SerialNumber', ''),
            'sku':             sys_data.get('SKU', ''),
            'bios_version':    sys_data.get('BiosVersion', ''),
            'ilo_firmware':    mgr_data.get('FirmwareVersion', ''),
            'ilo_model':       mgr_data.get('Model', ''),
            'power_state':     sys_data.get('PowerState', 'Unknown'),
            'post_state':      oem.get('PostState', ''),
            'health':          sys_data.get('Status', {}).get('Health', ''),
            'health_rollup':   sys_data.get('Status', {}).get('HealthRollup', ''),
            'cpu_model':       proc.get('Model', ''),
            'cpu_count':       proc.get('Count', 0),
            'cpu_health':      proc.get('Status', {}).get('Health', ''),
            'ram_gib':         mem.get('TotalSystemMemoryGiB', 0),
            'ram_health':      mem.get('Status', {}).get('Health', ''),
            'boot_source':     boot.get('BootSourceOverrideTarget', ''),
            'console_url':     f'https://{ilo_ip}/html/html5.html',
        })
    except requests.exceptions.ConnectionError:
        return jsonify({'error': f'Cannot reach iLO at {ilo_ip}', 'offline': True}), 503
    except requests.exceptions.Timeout:
        return jsonify({'error': 'iLO timed out', 'offline': True}), 504
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Health (thermal + power) ──────────────────────────────────────────────────

@bmc_bp.route('/api/bmc/servers/<server_id>/health')
def server_health(server_id):
    err = _auth()
    if err: return err
    srv, err_resp, code = _get_server_or_404(server_id)
    if err_resp: return err_resp, code

    ilo_ip = srv['ilo_ip']
    user   = srv['username']
    pwd    = srv['password']

    try:
        thermal = _rf_get(ilo_ip, user, pwd, '/redfish/v1/Chassis/1/Thermal/')
        power   = _rf_get(ilo_ip, user, pwd, '/redfish/v1/Chassis/1/Power/')

        # Temperatures
        temps = []
        for t in thermal.get('Temperatures', []):
            if t.get('Status', {}).get('State') == 'Absent':
                continue
            reading = t.get('ReadingCelsius')
            if reading is None:
                continue
            temps.append({
                'name':        t.get('Name', ''),
                'reading_c':   reading,
                'upper_warn':  t.get('UpperThresholdNonCritical'),
                'upper_crit':  t.get('UpperThresholdCritical'),
                'health':      t.get('Status', {}).get('Health', 'OK'),
                'location':    t.get('PhysicalContext', ''),
            })
        temps.sort(key=lambda x: x['reading_c'], reverse=True)

        # Fans
        fans = []
        for f in thermal.get('Fans', []):
            if f.get('Status', {}).get('State') == 'Absent':
                continue
            fans.append({
                'name':    f.get('Name', ''),
                'reading': f.get('Reading'),
                'units':   f.get('ReadingUnits', 'RPM'),
                'health':  f.get('Status', {}).get('Health', 'OK'),
            })

        # Power
        power_ctrl = power.get('PowerControl', [{}])[0]
        power_supplies = []
        for ps in power.get('PowerSupplies', []):
            power_supplies.append({
                'name':          ps.get('Name', ''),
                'state':         ps.get('Status', {}).get('State', ''),
                'health':        ps.get('Status', {}).get('Health', ''),
                'input_watts':   ps.get('PowerInputWatts'),
                'output_watts':  ps.get('PowerOutputWatts'),
                'capacity_watts': ps.get('PowerCapacityWatts'),
            })

        return jsonify({
            'temperatures':    temps,
            'fans':            fans,
            'power_consumed_watts': power_ctrl.get('PowerConsumedWatts'),
            'power_capacity_watts': power_ctrl.get('PowerCapacityWatts'),
            'power_avg_watts': power_ctrl.get('PowerMetrics', {}).get('AverageConsumedWatts'),
            'power_supplies':  power_supplies,
        })
    except requests.exceptions.ConnectionError:
        return jsonify({'error': f'Cannot reach iLO at {ilo_ip}', 'offline': True}), 503
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Event log (IML) ───────────────────────────────────────────────────────────

@bmc_bp.route('/api/bmc/servers/<server_id>/logs')
def server_logs(server_id):
    err = _auth()
    if err: return err
    srv, err_resp, code = _get_server_or_404(server_id)
    if err_resp: return err_resp, code

    ilo_ip = srv['ilo_ip']
    user   = srv['username']
    pwd    = srv['password']
    limit  = int(request.args.get('limit', 50))

    try:
        data = _rf_get(ilo_ip, user, pwd,
                       '/redfish/v1/Systems/1/LogServices/IML/Entries/')
        entries = []
        for e in data.get('Members', [])[:limit]:
            entries.append({
                'id':       e.get('Id', ''),
                'created':  e.get('Created', ''),
                'severity': e.get('Severity', 'OK'),
                'message':  e.get('Message', ''),
                'category': e.get('Oem', {}).get('Hpe', {}).get('Class', ''),
            })
        return jsonify({'entries': entries, 'total': data.get('Members@odata.count', len(entries))})
    except requests.exceptions.ConnectionError:
        return jsonify({'error': f'Cannot reach iLO at {ilo_ip}', 'offline': True}), 503
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Power control ─────────────────────────────────────────────────────────────

VALID_RESET_TYPES = {
    'on':               'On',
    'off':              'ForceOff',
    'graceful_off':     'GracefulShutdown',
    'restart':          'GracefulRestart',
    'force_restart':    'ForceRestart',
    'cold_boot':        'PowerCycle',
}

@bmc_bp.route('/api/bmc/servers/<server_id>/power', methods=['POST'])
def server_power(server_id):
    err = _auth()
    if err: return err
    srv, err_resp, code = _get_server_or_404(server_id)
    if err_resp: return err_resp, code

    ilo_ip = srv['ilo_ip']
    user   = srv['username']
    pwd    = srv['password']

    action = (request.get_json() or {}).get('action', '').lower()
    reset_type = VALID_RESET_TYPES.get(action)
    if not reset_type:
        return jsonify({'error': f'Unknown action. Valid: {list(VALID_RESET_TYPES)}'}), 400

    try:
        _rf_post(ilo_ip, user, pwd,
                 '/redfish/v1/Systems/1/Actions/ComputerSystem.Reset/',
                 {'ResetType': reset_type})
        return jsonify({'action': action, 'reset_type': reset_type, 'status': 'sent'})
    except requests.exceptions.ConnectionError:
        return jsonify({'error': f'Cannot reach iLO at {ilo_ip}', 'offline': True}), 503
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Quick summary (for server list cards) ────────────────────────────────────

@bmc_bp.route('/api/bmc/servers/<server_id>/summary')
def server_summary(server_id):
    """Lightweight endpoint for the server list — avoids 3 API calls."""
    err = _auth()
    if err: return err
    srv, err_resp, code = _get_server_or_404(server_id)
    if err_resp: return err_resp, code

    ilo_ip = srv['ilo_ip']
    user   = srv['username']
    pwd    = srv['password']

    try:
        sys_data = _rf_get(ilo_ip, user, pwd, '/redfish/v1/Systems/1/')
        power_data = _rf_get(ilo_ip, user, pwd, '/redfish/v1/Chassis/1/Power/')
        power_ctrl = power_data.get('PowerControl', [{}])[0]

        return jsonify({
            'id':           server_id,
            'name':         srv['name'],
            'description':  srv.get('description', ''),
            'ilo_ip':       ilo_ip,
            'model':        sys_data.get('Model', ''),
            'serial':       sys_data.get('SerialNumber', ''),
            'power_state':  sys_data.get('PowerState', 'Unknown'),
            'health':       sys_data.get('Status', {}).get('HealthRollup', ''),
            'cpu_model':    sys_data.get('ProcessorSummary', {}).get('Model', '').strip(),
            'ram_gib':      sys_data.get('MemorySummary', {}).get('TotalSystemMemoryGiB', 0),
            'power_watts':  power_ctrl.get('PowerConsumedWatts'),
            'post_state':   sys_data.get('Oem', {}).get('Hpe', {}).get('PostState', ''),
        })
    except requests.exceptions.ConnectionError:
        return jsonify({
            'id': server_id, 'name': srv['name'], 'ilo_ip': ilo_ip,
            'offline': True, 'error': f'Cannot reach iLO at {ilo_ip}'
        })
    except Exception as e:
        return jsonify({
            'id': server_id, 'name': srv['name'], 'ilo_ip': ilo_ip,
            'offline': True, 'error': str(e)
        })
