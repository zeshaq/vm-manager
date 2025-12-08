from flask import Blueprint, render_template
import shutil
import os
import subprocess
import getpass

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

def check_haproxy_installed():
    """Checks if HAProxy is installed."""
    return shutil.which('haproxy') is not None

def check_config_dirs():
    """Checks if the required /etc/vm-manager directories exist."""
    return os.path.isdir('/etc/vm-manager/haproxy')

def check_sudo_permissions():
    """Checks if the current user has passwordless sudo for the reload command."""
    command = ['sudo', '-n', '/bin/systemctl', 'reload', 'haproxy']
    # We expect this to fail if HAProxy isn't running, but a non-zero exit code
    # due to a password prompt is what we're looking for.
    # A password prompt typically returns 1.
    try:
        result = subprocess.run(command, capture_output=True)
        # If successful (0) or fails for reasons other than password (e.g., service not found),
        # it means sudo didn't ask for a password.
        return result.returncode != 1 or b'password' not in result.stderr.lower()
    except FileNotFoundError:
        # sudo or systemctl not found
        return False

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