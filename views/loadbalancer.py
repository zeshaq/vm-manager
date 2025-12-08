from flask import Blueprint, render_template, request, redirect, url_for, flash
import json
import libvirt
import os
import subprocess
from .setup import check_haproxy_installed, check_config_dirs, check_sudo_permissions

# --- Blueprint Setup ---
lb_bp = Blueprint('loadbalancer', __name__)


# --- Constants ---
ROUTES_FILE = os.path.join('data', 'routes.json')
HAPROXY_CONFIG = '/etc/vm-manager/haproxy/haproxy.cfg'

# --- Helper Functions ---

def read_routes():
    """Reads the routing rules from the JSON file."""
    if not os.path.exists(ROUTES_FILE):
        return []
    try:
        with open(ROUTES_FILE, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return []

def write_routes(routes):
    """Writes the routing rules to the JSON file."""
    with open(ROUTES_FILE, 'w') as f:
        json.dump(routes, f, indent=4)

def get_vm_ip(vm_uuid):
    """Gets the IP address of a VM given its UUID."""
    conn = libvirt.open('qemu:///system')
    if not conn:
        return None
    try:
        dom = conn.lookupByUUIDString(vm_uuid)
        if not dom.isActive():
            return None
        
        ifaces = dom.interfaceAddresses(libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_LEASE, 0)
        for _, val in ifaces.items():
            if val['addrs']:
                return val['addrs'][0]['addr']
    except libvirt.libvirtError:
        return None
    finally:
        if conn:
            conn.close()
    return None

def generate_haproxy_config():
    """Generates a new haproxy.cfg from the routes and reloads the service."""
    # --- Pre-flight checks ---
    if not all([check_haproxy_installed(), check_config_dirs(), check_sudo_permissions()]):
        flash("Setup is incomplete. Please resolve all issues on the Setup page before managing routes.", "error")
        return redirect(url_for('setup.setup_page'))

    routes = read_routes()
    
    # --- Base HAProxy Config ---
    config_lines = [
        "global",
        "    log /dev/log    local0",
        "    chroot /var/lib/haproxy",
        "    stats socket /run/haproxy/admin.sock mode 660 level admin expose-fd listeners",
        "    stats timeout 30s",
        "    user haproxy",
        "    group haproxy",
        "    daemon",
        "",
        "defaults",
        "    log     global",
        "    mode    http",
        "    option  httplog",
        "    option  dontlognull",
        "    timeout connect 5000",
        "    timeout client  50000",
        "    timeout server  50000",
        "",
        "frontend http_front",
        "    bind *:80",
        "    mode http",
    ]

    # --- Dynamic Frontend ACLs and Backends ---
    backends = []
    for i, route in enumerate(routes):
        vm_ip = get_vm_ip(route['vm_uuid'])
        if not vm_ip:
            # Skip route if VM is off or has no IP
            continue

        frontend_name = route['frontend_host'].replace('.', '_')
        
        # Add ACL for routing
        config_lines.append(f"    acl host_{frontend_name} hdr(host) -i {route['frontend_host']}")
        config_lines.append(f"    use_backend backend_{frontend_name} if host_{frontend_name}")

        # Add Backend definition
        backends.extend([
            "",
            f"backend backend_{frontend_name}",
            "    mode http",
            "    balance roundrobin",
            f"    server {route['vm_name']}_{i} {vm_ip}:{route['backend_port']} check",
        ])

    config_lines.extend(backends)

    # --- Stats Page ---
    config_lines.extend([
        "",
        "listen stats",
        "    bind *:8404",
        "    stats enable",
        "    stats uri /",
        "    stats refresh 10s",
        "    stats admin if TRUE"
    ])

    # --- Write and Reload ---
    try:
        # Use sudo to write the config file to the protected directory
        config_content = "\n".join(config_lines)
        write_cmd = ['sudo', 'tee', HAPROXY_CONFIG]
        subprocess.run(write_cmd, input=config_content.encode(), check=True, capture_output=True)

        # Reload HAProxy using systemctl
        reload_cmd = ['sudo', 'systemctl', 'reload', 'haproxy']
        subprocess.run(reload_cmd, check=True)
        
        flash("HAProxy configuration updated and reloaded successfully.", "success")
    except (IOError, subprocess.CalledProcessError) as e:
        error_msg = e.stderr.decode() if hasattr(e, 'stderr') else str(e)
        flash(f"Error updating HAProxy: {error_msg}", "error")
        print(f"❌ Error updating HAProxy: {error_msg}")


# --- Routes ---

@lb_bp.route('/loadbalancer')
def manage_loadbalancer():
    conn = libvirt.open('qemu:///system')
    vms = []
    if conn:
        try:
            # Get only running domains, as only they can be routed to
            domains = conn.listAllDomains(libvirt.VIR_DOMAIN_RUNNING)
            for dom in domains:
                vms.append({'uuid': dom.UUIDString(), 'name': dom.name()})
        finally:
            conn.close()
    
    routes = read_routes()
    setup_complete = all([check_haproxy_installed(), check_config_dirs(), check_sudo_permissions()])
    
    return render_template('loadbalancer.html', routes=routes, vms=vms, setup_complete=setup_complete)

@lb_bp.route('/loadbalancer/add', methods=['POST'])
def add_route():
    routes = read_routes()
    vm_uuid, vm_name = request.form['vm_selection'].split('|')
    
    new_route = {
        'frontend_host': request.form['frontend_host'],
        'vm_uuid': vm_uuid,
        'vm_name': vm_name,
        'backend_port': request.form['backend_port']
    }
    routes.append(new_route)
    write_routes(routes)
    generate_haproxy_config()
    
    return redirect(url_for('loadbalancer.manage_loadbalancer'))

@lb_bp.route('/loadbalancer/delete', methods=['POST'])
def delete_route():
    routes = read_routes()
    frontend_host = request.form['frontend_host']
    
    # Filter out the route to be deleted
    routes = [r for r in routes if r['frontend_host'] != frontend_host]
    
    write_routes(routes)
    generate_haproxy_config()

    return redirect(url_for('loadbalancer.manage_loadbalancer'))
