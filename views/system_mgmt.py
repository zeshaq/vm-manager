"""
System Management API
  - Processes (psutil + kill)
  - Systemd service manager (list / status / start / stop / restart / enable / disable / kill)
  - UFW firewall (status / toggle / rules CRUD)
  - Security overview

Privilege note — systemctl and ufw need root.  The app tries `sudo -n` first;
if that fails due to a missing sudoers entry it returns a clear error.
Add to /etc/sudoers.d/hypercloud:
    ze ALL=(ALL) NOPASSWD: /usr/bin/systemctl, /usr/sbin/ufw, /usr/bin/grep, /usr/bin/ss
"""

import re
import subprocess
import time
from flask import Blueprint, jsonify, request, session

try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False

system_bp = Blueprint('system', __name__)


# ── helpers ───────────────────────────────────────────────────────────────────

def _auth():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    return None


def _run(cmd, input_data=None, timeout=10, shell=False):
    try:
        r = subprocess.run(
            cmd, shell=shell,
            capture_output=True, text=True,
            timeout=timeout, input=input_data,
        )
        return r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        return '', 'Command timed out', 124
    except Exception as e:
        return '', str(e), 1


def _sudo(cmd, **kw):
    """Try sudo -n first; if that fails with a password prompt, try without sudo."""
    out, err, rc = _run(['sudo', '-n'] + cmd, **kw)
    if rc != 0 and ('sudo' in err.lower() or 'password' in err.lower() or 'sudoers' in err.lower()):
        out, err, rc = _run(cmd, **kw)
    return out, err, rc


# ── Processes ─────────────────────────────────────────────────────────────────

@system_bp.route('/api/system/processes')
def list_processes():
    err = _auth()
    if err:
        return err
    if not _PSUTIL:
        return jsonify({'error': 'psutil not installed'}), 503

    procs = []
    for p in psutil.process_iter(['pid', 'name', 'username', 'cpu_percent',
                                   'memory_percent', 'status', 'cmdline']):
        try:
            info = p.info
            procs.append({
                'pid':    info['pid'],
                'name':   info['name'] or '',
                'user':   info['username'] or '',
                'cpu':    round(info['cpu_percent'] or 0, 1),
                'mem':    round(info['memory_percent'] or 0, 2),
                'status': info['status'] or '',
                'cmd':    ' '.join(info['cmdline'] or [])[:200],
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    procs.sort(key=lambda x: x['cpu'], reverse=True)
    return jsonify({'processes': procs, 'count': len(procs), 'ts': time.time()})


@system_bp.route('/api/system/processes/<int:pid>/kill', methods=['POST'])
def kill_process(pid):
    err = _auth()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    force = data.get('force', False)
    try:
        p = psutil.Process(pid)
        name = p.name()
        if force:
            p.kill()
        else:
            p.terminate()
        return jsonify({'ok': True, 'name': name})
    except psutil.NoSuchProcess:
        return jsonify({'error': 'Process not found'}), 404
    except psutil.AccessDenied:
        sig = '-9' if force else '-15'
        _, stderr, rc = _sudo(['kill', sig, str(pid)])
        if rc != 0:
            return jsonify({'error': f'Access denied — {stderr.strip()}'}), 403
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Systemd Services ──────────────────────────────────────────────────────────

@system_bp.route('/api/system/services')
def list_services():
    err = _auth()
    if err:
        return err

    out_units, _, _ = _run([
        'systemctl', 'list-units', '--type=service', '--all',
        '--no-pager', '--no-legend', '--plain',
    ])
    out_files, _, _ = _run([
        'systemctl', 'list-unit-files', '--type=service',
        '--no-pager', '--no-legend', '--plain',
    ])

    enabled_map = {}
    for line in out_files.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            enabled_map[parts[0]] = parts[1]

    services = []
    for line in out_units.splitlines():
        stripped = line.strip().lstrip('●').strip()
        if not stripped:
            continue
        parts = stripped.split(None, 4)
        if len(parts) < 3:
            continue
        name = parts[0]
        if not name.endswith('.service'):
            continue
        services.append({
            'name':        name,
            'load':        parts[1] if len(parts) > 1 else '',
            'active':      parts[2] if len(parts) > 2 else '',
            'sub':         parts[3] if len(parts) > 3 else '',
            'description': parts[4] if len(parts) > 4 else '',
            'enabled':     enabled_map.get(name, 'unknown'),
        })

    return jsonify({'services': services, 'count': len(services)})


@system_bp.route('/api/system/services/<path:name>/status')
def service_status(name):
    err = _auth()
    if err:
        return err
    if not name.endswith('.service'):
        name += '.service'
    stdout, _, rc = _run(
        ['systemctl', 'status', '--no-pager', '-l', '-n', '60', name],
        timeout=5,
    )
    return jsonify({'output': stdout, 'rc': rc})


@system_bp.route('/api/system/services/<path:name>/<action>', methods=['POST'])
def service_action(name, action):
    err = _auth()
    if err:
        return err
    if not name.endswith('.service'):
        name += '.service'

    allowed = {'start', 'stop', 'restart', 'reload', 'enable', 'disable', 'kill', 'force-kill'}
    if action not in allowed:
        return jsonify({'error': f'Unknown action: {action}'}), 400

    if action == 'force-kill':
        cmd = ['systemctl', 'kill', '-s', 'SIGKILL', name]
    elif action == 'kill':
        cmd = ['systemctl', 'kill', name]
    else:
        cmd = ['systemctl', action, name]

    stdout, stderr, rc = _sudo(cmd, timeout=15)
    if rc != 0:
        return jsonify({'error': (stderr or stdout or f'rc={rc}').strip()}), 500
    return jsonify({'ok': True, 'output': stdout.strip()})


# ── UFW Firewall ──────────────────────────────────────────────────────────────

@system_bp.route('/api/system/ufw')
def ufw_status():
    err = _auth()
    if err:
        return err

    verbose, _, rv = _sudo(['ufw', 'status', 'verbose'], timeout=6)
    numbered, _, rn = _sudo(['ufw', 'status', 'numbered'], timeout=6)

    if rv != 0 and rn != 0:
        return jsonify({'enabled': False, 'raw': verbose, 'rules': [],
                        'error': 'ufw unavailable or permission denied'})

    enabled = 'Status: active' in verbose

    rules = []
    for line in numbered.splitlines():
        m = re.match(r'\[\s*(\d+)\]\s+(.*)', line)
        if m:
            rules.append({'num': int(m.group(1)), 'rule': m.group(2).strip()})

    return jsonify({'enabled': enabled, 'raw': verbose, 'rules': rules})


@system_bp.route('/api/system/ufw/toggle', methods=['POST'])
def ufw_toggle():
    err = _auth()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    enable = data.get('enable', True)
    action = 'enable' if enable else 'disable'
    out, stderr, rc = _sudo(['ufw', '--force', action], timeout=10)
    if rc != 0:
        return jsonify({'error': (stderr or out).strip()}), 500
    return jsonify({'ok': True, 'enabled': enable, 'output': out.strip()})


@system_bp.route('/api/system/ufw/rules', methods=['POST'])
def ufw_add_rule():
    err = _auth()
    if err:
        return err
    data = request.get_json(silent=True) or {}

    action    = data.get('action', 'allow')
    port      = str(data.get('port', '')).strip()
    proto     = data.get('proto', '')       # tcp | udp | ''
    direction = data.get('direction', 'in') # in | out | ''
    comment   = data.get('comment', '').strip()[:64]

    if action not in ('allow', 'deny', 'limit', 'reject'):
        return jsonify({'error': 'action must be allow, deny, limit, or reject'}), 400
    if not port:
        return jsonify({'error': 'port is required'}), 400
    if not re.match(r'^[\d:]+(/\w+)?$', port):
        return jsonify({'error': 'invalid port — use 22, 8080:8090, or 22/tcp'}), 400

    cmd = ['ufw', action]
    if direction in ('in', 'out'):
        cmd.append(direction)
    spec = f'{port}/{proto}' if proto in ('tcp', 'udp') else port
    cmd.append(spec)
    if comment:
        cmd += ['comment', comment]

    out, stderr, rc = _sudo(cmd, timeout=10)
    if rc != 0:
        return jsonify({'error': (stderr or out).strip()}), 500
    return jsonify({'ok': True, 'output': out.strip()})


@system_bp.route('/api/system/ufw/rules', methods=['DELETE'])
def ufw_delete_rule():
    err = _auth()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    num = data.get('num')
    if not num:
        return jsonify({'error': 'num is required'}), 400
    out, stderr, rc = _sudo(['ufw', '--force', 'delete', str(num)], timeout=10)
    if rc != 0:
        return jsonify({'error': (stderr or out).strip()}), 500
    return jsonify({'ok': True})


# ── Security Overview ─────────────────────────────────────────────────────────

@system_bp.route('/api/system/security')
def security_overview():
    err = _auth()
    if err:
        return err

    out = {}

    # Failed SSH logins
    failed = 0
    for lf in ('/var/log/auth.log', '/var/log/secure'):
        r, _, rc = _run(['grep', '-c', 'Failed password', lf], timeout=5)
        if rc == 0:
            try:
                failed = int(r.strip())
                break
            except ValueError:
                pass
    out['failed_logins'] = failed

    # Recent logins
    last_out, _, _ = _run(['last', '-n', '12', '-w'])
    out['recent_logins'] = [
        l for l in last_out.splitlines()
        if l.strip() and not l.startswith('wtmp') and not l.startswith('reboot')
    ][:12]

    # Currently logged-in users
    who_out, _, _ = _run(['who'])
    out['logged_in'] = [l.strip() for l in who_out.splitlines() if l.strip()]

    # Listening ports
    ss_out, _, _ = _run(['ss', '-tlnup'])
    ports = []
    for line in ss_out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 5 and parts[0] in ('LISTEN', 'UNCONN'):
            local   = parts[4]
            process = parts[6] if len(parts) > 6 else ''
            ports.append({'local': local, 'process': process})
    out['open_ports'] = ports

    # Pending apt updates
    apt_out, _, apt_rc = _run(['apt', 'list', '--upgradable'], timeout=20)
    if apt_rc == 0:
        lines = [l for l in apt_out.splitlines() if '[upgradable' in l]
        sec   = [l for l in lines if 'security' in l.lower()]
        out['updates'] = {'total': len(lines), 'security': len(sec), 'security_list': sec[:20]}
    else:
        out['updates'] = None

    # SSH config
    ssh = {}
    try:
        with open('/etc/ssh/sshd_config') as f:
            sshd = f.read()

        def _sshd(key, default='unset'):
            m = re.search(rf'^\s*{key}\s+(\S+)', sshd, re.I | re.M)
            return m.group(1).lower() if m else default

        ssh['permit_root_login']  = _sshd('PermitRootLogin')
        ssh['password_auth']      = _sshd('PasswordAuthentication')
        ssh['permit_empty_pw']    = _sshd('PermitEmptyPasswords')
        ssh['pubkey_auth']        = _sshd('PubkeyAuthentication', 'yes')
        ssh['port']               = _sshd('Port', '22')
    except Exception as e:
        ssh['error'] = str(e)
    out['ssh'] = ssh

    # Sudoers NOPASSWD
    nopasswd_out, _, _ = _sudo(['grep', '-rh', 'NOPASSWD',
                                 '/etc/sudoers', '/etc/sudoers.d/'], timeout=5)
    out['sudo_nopasswd'] = [
        l.strip() for l in nopasswd_out.splitlines()
        if l.strip() and not l.strip().startswith('#')
    ]

    # UFW
    ufw_out, _, _ = _sudo(['ufw', 'status'], timeout=5)
    out['ufw_enabled'] = 'Status: active' in ufw_out

    # Services
    def _svc_active(name):
        _, _, rc = _run(['systemctl', 'is-active', '--quiet', name])
        return rc == 0

    out['fail2ban'] = _svc_active('fail2ban')
    out['clamav']   = _svc_active('clamav-daemon')
    out['apparmor'] = _svc_active('apparmor')

    # SELinux
    se_out, _, _ = _run(['getenforce'])
    out['selinux'] = se_out.strip() or None

    out['ts'] = time.time()
    return jsonify(out)
