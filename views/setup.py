from flask import Blueprint, render_template, redirect, url_for, flash
import shutil
import os
import subprocess
import getpass
from .shared_utils import check_haproxy_installed, check_config_dirs, check_sudo_permissions

# --- Blueprint Setup ---
setup_bp = Blueprint('setup', __name__)


# --- Helper Functions ---

def get_package_manager():
    """Detects the system's package manager."""
    if shutil.which('apt-get'):
        return 'apt-get'
    if shutil.which('dnf'):
        return 'dnf'
    if shutil.which('yum'):
        return 'yum'
    return None

# --- Route ---

@setup_bp.route('/setup')
def setup_page():
    # The commands the user might need to run
    username = getpass.getuser()
    package_manager = get_package_manager()
    
    install_command = "echo 'Unsupported package manager. Please install HAProxy manually.'"
    if package_manager == 'apt-get':
        install_command = 'sudo apt-get update && sudo apt-get install -y haproxy'
    elif package_manager in ['dnf', 'yum']:
        install_command = f'sudo {package_manager} install -y haproxy'

    sudoers_command = f"echo '{username} ALL=(ALL) NOPASSWD: /bin/systemctl reload haproxy' | sudo tee /etc/sudoers.d/vm-manager"

    # Perform all checks
    checks = {
        'haproxy_installed': {
            'pass': check_haproxy_installed(),
            'message': 'HAProxy is installed',
            'fix_command': install_command,
            'fix_description': 'To install HAProxy, run this command in your terminal:'
        },
        'config_dirs_exist': {
            'pass': check_config_dirs(),
            'message': 'Configuration directory exists',
            'fix_command': f'sudo mkdir -p /etc/vm-manager/haproxy && sudo chown -R {username}:{username} /etc/vm-manager',
            'fix_description': 'To create the necessary directories, run this command:'
        },
        'sudo_permissions': {
            'pass': check_sudo_permissions(),
            'message': 'Passwordless sudo for HAProxy reload is configured',
            'fix_command': sudoers_command,
            'fix_description': 'To grant the app permission to reload HAProxy, run this command:'
        }
    }
    
    all_passed = all(c['pass'] for c in checks.values())

    return render_template('setup.html', checks=checks, all_passed=all_passed)

@setup_bp.route('/setup/initialize')
def initialize_haproxy():
    """Route to trigger the initial creation of the haproxy.cfg."""
    from .loadbalancer import generate_haproxy_config
    generate_haproxy_config()
    flash("Attempted to create initial HAProxy configuration and reload the service.", "success")
    return redirect(url_for('setup.setup_page'))