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
    """Checks if the current user has passwordless sudo for the restart command."""
    command = ['sudo', '-n', '/bin/systemctl', 'restart', 'haproxy']
    try:
        result = subprocess.run(command, capture_output=True)
        # We are checking if sudo is asking for a password.
        # A password prompt typically returns 1 and mentions 'password'.
        return result.returncode != 1 or b'password' not in result.stderr.lower()
    except FileNotFoundError:
        return False
