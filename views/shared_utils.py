import shutil
import os
import subprocess

def check_haproxy_installed():
    """Checks if HAProxy is installed."""
    return shutil.which('haproxy') is not None

def check_config_dirs():
    """Checks if the required /etc/vm-manager directories exist."""
    return os.path.isdir('/etc/vm-manager/haproxy')

def check_sudo_permissions():
    """Checks if the current user has passwordless sudo for the reload command."""
    command = ['sudo', '-n', '/bin/systemctl', 'reload', 'haproxy']
    try:
        result = subprocess.run(command, capture_output=True)
        return result.returncode != 1 or b'password' not in result.stderr.lower()
    except FileNotFoundError:
        return False
