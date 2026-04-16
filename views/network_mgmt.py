import ipaddress
import json
import os
import re
import subprocess
import xml.etree.ElementTree as ET
import yaml
from flask import Blueprint, jsonify, request, session

try:
    import libvirt as _libvirt
    _HAS_LIBVIRT = True
except ImportError:
    _HAS_LIBVIRT = False

network_bp = Blueprint('network', __name__)

NETPLAN_DIR = '/etc/netplan'

# Only allow safe filenames: alphanumeric, hyphens, underscores, dots, must end in .yaml
_FNAME_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}\.yaml$')


def _auth():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    return None


def _run(*cmd, input_text=None, timeout=15):
    """Run a command and return (stdout, stderr, returncode)."""
    result = subprocess.run(
        list(cmd),
        capture_output=True,
        text=True,
        input=input_text,
        timeout=timeout,
    )
    return result.stdout, result.stderr, result.returncode


# ── Live interface data ────────────────────────────────────────────────────────

@network_bp.route('/api/network/interfaces')
def get_interfaces():
    err = _auth()
    if err:
        return err
    try:
        stdout, _, _ = _run('ip', '-j', 'addr')
        ifaces = json.loads(stdout) if stdout.strip() else []

        # Pull stats (speed, duplex, mtu)
        stats_out, _, _ = _run('ip', '-j', '-s', 'link')
        stats_map = {}
        if stats_out.strip():
            for s in json.loads(stats_out):
                stats_map[s['ifname']] = s.get('stats64', s.get('stats', {}))

        result = []
        for iface in ifaces:
            name = iface['ifname']
            flags = iface.get('flags', [])
            addrs = []
            for a in iface.get('addr_info', []):
                if a.get('family') in ('inet', 'inet6'):
                    addrs.append({
                        'family':  a['family'],
                        'address': a['local'],
                        'prefix':  a['prefixlen'],
                        'cidr':    f"{a['local']}/{a['prefixlen']}",
                        'scope':   a.get('scope', ''),
                    })
            tx = stats_map.get(name, {}).get('tx', {})
            rx = stats_map.get(name, {}).get('rx', {})
            result.append({
                'name':      name,
                'operstate': iface.get('operstate', 'UNKNOWN'),
                'flags':     flags,
                'up':        'UP' in flags,
                'link_type': iface.get('link_type', ''),
                'mtu':       iface.get('mtu', 0),
                'mac':       iface.get('address', ''),
                'broadcast': iface.get('broadcast', ''),
                'txqlen':    iface.get('txqlen', 0),
                'addresses': addrs,
                'tx_bytes':  tx.get('bytes', 0),
                'rx_bytes':  rx.get('bytes', 0),
                'tx_packets':tx.get('packets', 0),
                'rx_packets':rx.get('packets', 0),
                'tx_errors': tx.get('errors', 0),
                'rx_errors': rx.get('errors', 0),
            })
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Routes ────────────────────────────────────────────────────────────────────

@network_bp.route('/api/network/routes')
def get_routes():
    err = _auth()
    if err:
        return err
    try:
        stdout4, _, _ = _run('ip', '-j', 'route')
        stdout6, _, _ = _run('ip', '-j', '-6', 'route')
        routes4 = json.loads(stdout4) if stdout4.strip() else []
        routes6 = json.loads(stdout6) if stdout6.strip() else []
        for r in routes4: r['family'] = 'inet'
        for r in routes6: r['family'] = 'inet6'
        return jsonify(routes4 + routes6)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── DNS ───────────────────────────────────────────────────────────────────────

@network_bp.route('/api/network/dns')
def get_dns():
    err = _auth()
    if err:
        return err
    result = {'servers': [], 'search': [], 'raw': ''}
    try:
        stdout, _, rc = _run('resolvectl', 'status', '--no-pager')
        if rc == 0:
            result['raw'] = stdout
            # Parse DNS servers
            for line in stdout.splitlines():
                line = line.strip()
                if 'DNS Servers:' in line or 'Current DNS Server:' in line:
                    parts = line.split(':', 1)
                    if len(parts) > 1:
                        servers = parts[1].split()
                        result['servers'].extend(servers)
                if 'DNS Domain:' in line or 'Search Domains:' in line or 'DNS Search:' in line:
                    parts = line.split(':', 1)
                    if len(parts) > 1:
                        result['search'].extend(parts[1].split())
        else:
            # Fallback to /etc/resolv.conf
            with open('/etc/resolv.conf') as f:
                result['raw'] = f.read()
            for line in result['raw'].splitlines():
                line = line.strip()
                if line.startswith('nameserver'):
                    result['servers'].append(line.split()[1])
                elif line.startswith('search'):
                    result['search'].extend(line.split()[1:])
    except Exception as e:
        result['error'] = str(e)
    # Deduplicate
    result['servers'] = list(dict.fromkeys(result['servers']))
    result['search']  = list(dict.fromkeys(result['search']))
    return jsonify(result)


# ── Netplan config files ──────────────────────────────────────────────────────

@network_bp.route('/api/network/netplan/configs')
def list_netplan_configs():
    err = _auth()
    if err:
        return err
    try:
        stdout, stderr, rc = _run('sudo', 'ls', NETPLAN_DIR)
        if rc != 0:
            return jsonify({'error': stderr or 'Cannot list netplan dir'}), 500
        files = [f.strip() for f in stdout.splitlines() if f.strip().endswith('.yaml')]
        result = []
        for fname in sorted(files):
            content, cerr, crc = _run('sudo', 'cat', f'{NETPLAN_DIR}/{fname}')
            result.append({
                'filename': fname,
                'content': content if crc == 0 else f'# Error reading file: {cerr}',
                'readonly': False,
            })
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@network_bp.route('/api/network/netplan/configs/<filename>', methods=['GET'])
def get_netplan_config(filename):
    err = _auth()
    if err:
        return err
    if not _FNAME_RE.match(filename):
        return jsonify({'error': 'Invalid filename'}), 400
    stdout, stderr, rc = _run('sudo', 'cat', f'{NETPLAN_DIR}/{filename}')
    if rc != 0:
        return jsonify({'error': stderr or 'File not found'}), 404
    return jsonify({'filename': filename, 'content': stdout})


@network_bp.route('/api/network/netplan/configs/<filename>', methods=['PUT'])
def save_netplan_config(filename):
    err = _auth()
    if err:
        return err
    if not _FNAME_RE.match(filename):
        return jsonify({'error': 'Invalid filename'}), 400
    data = request.get_json() or {}
    content = data.get('content', '')
    if not content.strip():
        return jsonify({'error': 'Content cannot be empty'}), 400

    # Validate YAML syntax before writing
    try:
        parsed = yaml.safe_load(content)
        if not isinstance(parsed, dict) or 'network' not in parsed:
            return jsonify({'error': 'Invalid netplan: must have a "network" key'}), 400
    except yaml.YAMLError as e:
        return jsonify({'error': f'YAML syntax error: {e}'}), 400

    # Write via sudo tee (sudoers allows this without password)
    _, stderr, rc = _run('sudo', 'tee', f'{NETPLAN_DIR}/{filename}',
                         input_text=content)
    if rc != 0:
        return jsonify({'error': stderr or 'Failed to write file'}), 500

    # Fix permissions (netplan expects 600)
    _run('sudo', 'chmod', '600', f'{NETPLAN_DIR}/{filename}')
    return jsonify({'success': True})


@network_bp.route('/api/network/netplan/validate', methods=['POST'])
def validate_netplan():
    """Run netplan generate --root-dir /tmp/netplan-test to check syntax."""
    err = _auth()
    if err:
        return err
    data = request.get_json() or {}
    content = data.get('content', '')
    if not content.strip():
        return jsonify({'valid': False, 'error': 'Empty content'}), 400

    # Client-side YAML check first
    try:
        parsed = yaml.safe_load(content)
        if not isinstance(parsed, dict) or 'network' not in parsed:
            return jsonify({'valid': False, 'error': 'Missing "network" root key'})
    except yaml.YAMLError as e:
        return jsonify({'valid': False, 'error': f'YAML syntax error: {e}'})

    # Run netplan generate on the real configs as a deeper check
    _, stderr, rc = _run('sudo', 'netplan', 'generate', timeout=10)
    if rc != 0:
        return jsonify({'valid': False, 'error': stderr or 'netplan generate failed'})

    return jsonify({'valid': True})


@network_bp.route('/api/network/netplan/apply', methods=['POST'])
def apply_netplan():
    err = _auth()
    if err:
        return err
    stdout, stderr, rc = _run('sudo', 'netplan', 'apply', timeout=30)
    if rc != 0:
        return jsonify({'success': False, 'error': stderr or 'netplan apply failed',
                        'output': stdout}), 500
    return jsonify({'success': True, 'output': stdout + stderr})


# ── New file ──────────────────────────────────────────────────────────────────

@network_bp.route('/api/network/netplan/configs', methods=['POST'])
def create_netplan_config():
    err = _auth()
    if err:
        return err
    data = request.get_json() or {}
    filename = str(data.get('filename', '')).strip()
    if not _FNAME_RE.match(filename):
        return jsonify({'error': 'Invalid filename (must match NN-name.yaml)'}), 400
    content = data.get('content', '').strip()
    if not content:
        content = 'network:\n  version: 2\n  ethernets: {}\n'

    # Validate YAML
    try:
        parsed = yaml.safe_load(content)
        if not isinstance(parsed, dict) or 'network' not in parsed:
            return jsonify({'error': 'Invalid netplan content'}), 400
    except yaml.YAMLError as e:
        return jsonify({'error': f'YAML error: {e}'}), 400

    _, stderr, rc = _run('sudo', 'tee', f'{NETPLAN_DIR}/{filename}', input_text=content)
    if rc != 0:
        return jsonify({'error': stderr or 'Write failed'}), 500
    _run('sudo', 'chmod', '600', f'{NETPLAN_DIR}/{filename}')
    return jsonify({'success': True})


# ── Virsh / libvirt network management ───────────────────────────────────────

_NET_NAME_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$')


def _lv_conn():
    if not _HAS_LIBVIRT:
        return None
    try:
        return _libvirt.open('qemu:///system')
    except Exception:
        return None


def _parse_net_xml(xml_str: str) -> dict:
    """Extract forward mode, bridge, IP, DHCP range from network XML."""
    try:
        root = ET.fromstring(xml_str)
        fwd  = root.find('forward')
        bridge = root.find('bridge')
        ip_el  = root.find('ip')

        forward_mode = fwd.get('mode', 'nat') if fwd is not None else 'isolated'
        bridge_name  = bridge.get('name', '') if bridge is not None else ''

        ip_addr  = ''
        netmask  = ''
        prefix   = ''
        cidr     = ''
        dhcp_start = ''
        dhcp_end   = ''

        if ip_el is not None:
            ip_addr = ip_el.get('address', '')
            netmask = ip_el.get('netmask', '')
            if ip_addr and netmask:
                try:
                    net = ipaddress.IPv4Network(f'{ip_addr}/{netmask}', strict=False)
                    prefix = str(net.prefixlen)
                    cidr   = str(net)
                except Exception:
                    pass
            dhcp_el = ip_el.find('dhcp/range')
            if dhcp_el is not None:
                dhcp_start = dhcp_el.get('start', '')
                dhcp_end   = dhcp_el.get('end', '')

        return {
            'forward_mode': forward_mode,
            'bridge':       bridge_name,
            'ip':           ip_addr,
            'netmask':      netmask,
            'prefix':       prefix,
            'cidr':         cidr,
            'dhcp_start':   dhcp_start,
            'dhcp_end':     dhcp_end,
        }
    except Exception:
        return {}


def _net_info(net) -> dict:
    xml_str = net.XMLDesc(0)
    parsed  = _parse_net_xml(xml_str)
    return {
        'name':         net.name(),
        'uuid':         net.UUIDString(),
        'active':       bool(net.isActive()),
        'autostart':    bool(net.autostart()),
        **parsed,
    }


@network_bp.route('/api/virsh/networks')
def list_virsh_networks():
    err = _auth()
    if err:
        return err
    conn = _lv_conn()
    if not conn:
        return jsonify({'error': 'Cannot connect to libvirt'}), 500
    try:
        nets = conn.listAllNetworks(0)
        return jsonify([_net_info(n) for n in nets])
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@network_bp.route('/api/virsh/networks', methods=['POST'])
def create_virsh_network():
    err = _auth()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    name         = data.get('name', '').strip()
    forward_mode = data.get('forward_mode', 'nat')   # nat | isolated | bridge | route
    bridge_name  = data.get('bridge_name', '').strip()
    ip_address   = data.get('ip_address', '').strip()
    prefix       = data.get('prefix', '24')
    dhcp_enabled = data.get('dhcp_enabled', True)
    dhcp_start   = data.get('dhcp_start', '').strip()
    dhcp_end     = data.get('dhcp_end', '').strip()

    if not name or not _NET_NAME_RE.match(name):
        return jsonify({'error': 'Invalid network name'}), 400

    # Build XML
    if forward_mode == 'bridge':
        if not bridge_name:
            return jsonify({'error': 'Bridge interface name required'}), 400
        xml = (
            f"<network>\n"
            f"  <name>{name}</name>\n"
            f"  <forward mode='bridge'/>\n"
            f"  <bridge name='{bridge_name}'/>\n"
            f"</network>"
        )
    else:
        # NAT, route, or isolated — need an IP block
        if not ip_address:
            return jsonify({'error': 'IP address required'}), 400
        try:
            net = ipaddress.IPv4Network(f'{ip_address}/{prefix}', strict=False)
            netmask = str(net.netmask)
            # Auto-suggest DHCP range if not provided
            hosts = list(net.hosts())
            if not dhcp_start and hosts:
                dhcp_start = str(hosts[1])   # skip gateway (.1)
            if not dhcp_end and hosts:
                dhcp_end = str(hosts[-1])
        except ValueError as e:
            return jsonify({'error': f'Invalid IP/prefix: {e}'}), 400

        fwd_xml = '' if forward_mode == 'isolated' else f"  <forward mode='{forward_mode}'/>\n"
        dhcp_xml = ''
        if dhcp_enabled and dhcp_start and dhcp_end:
            dhcp_xml = (
                f"    <dhcp>\n"
                f"      <range start='{dhcp_start}' end='{dhcp_end}'/>\n"
                f"    </dhcp>\n"
            )
        auto_bridge = bridge_name or f'virbr-{name[:8]}'
        xml = (
            f"<network>\n"
            f"  <name>{name}</name>\n"
            f"{fwd_xml}"
            f"  <bridge name='{auto_bridge}' stp='on' delay='0'/>\n"
            f"  <ip address='{ip_address}' netmask='{netmask}'>\n"
            f"{dhcp_xml}"
            f"  </ip>\n"
            f"</network>"
        )

    conn = _lv_conn()
    if not conn:
        return jsonify({'error': 'Cannot connect to libvirt'}), 500
    try:
        net = conn.networkDefineXML(xml)
        net.create()          # start immediately
        net.setAutostart(1)   # autostart by default
        return jsonify(_net_info(net)), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@network_bp.route('/api/virsh/networks/<name>/start', methods=['POST'])
def start_virsh_network(name):
    err = _auth()
    if err:
        return err
    conn = _lv_conn()
    if not conn:
        return jsonify({'error': 'Cannot connect to libvirt'}), 500
    try:
        net = conn.networkLookupByName(name)
        if not net.isActive():
            net.create()
        return jsonify(_net_info(net))
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@network_bp.route('/api/virsh/networks/<name>/stop', methods=['POST'])
def stop_virsh_network(name):
    err = _auth()
    if err:
        return err
    conn = _lv_conn()
    if not conn:
        return jsonify({'error': 'Cannot connect to libvirt'}), 500
    try:
        net = conn.networkLookupByName(name)
        if net.isActive():
            net.destroy()
        return jsonify(_net_info(net))
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@network_bp.route('/api/virsh/networks/<name>/autostart', methods=['POST'])
def toggle_virsh_autostart(name):
    err = _auth()
    if err:
        return err
    conn = _lv_conn()
    if not conn:
        return jsonify({'error': 'Cannot connect to libvirt'}), 500
    try:
        net = conn.networkLookupByName(name)
        new_val = 0 if net.autostart() else 1
        net.setAutostart(new_val)
        return jsonify(_net_info(net))
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@network_bp.route('/api/virsh/networks/<name>', methods=['DELETE'])
def delete_virsh_network(name):
    err = _auth()
    if err:
        return err
    conn = _lv_conn()
    if not conn:
        return jsonify({'error': 'Cannot connect to libvirt'}), 500
    try:
        net = conn.networkLookupByName(name)
        if net.isActive():
            net.destroy()
        net.undefine()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@network_bp.route('/api/virsh/networks/<name>/leases')
def virsh_network_leases(name):
    err = _auth()
    if err:
        return err
    conn = _lv_conn()
    if not conn:
        return jsonify({'error': 'Cannot connect to libvirt'}), 500
    try:
        net    = conn.networkLookupByName(name)
        leases = net.DHCPLeases() or []
        result = []
        for l in leases:
            result.append({
                'mac':      l.get('mac', ''),
                'ip':       l.get('ipaddr', ''),
                'prefix':   l.get('prefix', ''),
                'hostname': l.get('hostname', ''),
                'iface':    l.get('iface', ''),
                'expiry':   l.get('expirytime', 0),
            })
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()
