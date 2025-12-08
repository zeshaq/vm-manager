import datetime
import json
from flask import session, Blueprint, render_template

audit_bp = Blueprint('audit', __name__)

LOG_FILE = 'logs/audit_trail.log'

def log_event(action, target_uuid=None, target_name=None, details=None):
    """Records an audit trail event."""
    try:
        username = session.get('username', 'System')
        log_entry = {
            'timestamp': datetime.datetime.utcnow().isoformat(),
            'username': username,
            'action': action,
            'target_uuid': target_uuid,
            'target_name': target_name,
            'details': details
        }
        with open(LOG_FILE, 'a') as f:
            f.write(json.dumps(log_entry) + '\n')
    except Exception as e:
        print(f"Failed to write to audit log: {e}")

def get_audit_logs():
    """Retrieves all audit trail entries."""
    logs = []
    try:
        with open(LOG_FILE, 'r') as f:
            for line in f:
                if line.strip():
                    logs.append(json.loads(line))
    except FileNotFoundError:
        return [] # Return empty list if log file doesn't exist yet
    except Exception as e:
        print(f"Failed to read audit log: {e}")
    # Return logs in reverse chronological order
    return sorted(logs, key=lambda x: x['timestamp'], reverse=True)

@audit_bp.route('/audit')
def show_audit_log():
    logs = get_audit_logs()
    return render_template('audit.html', logs=logs)
