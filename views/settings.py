"""
System-wide settings — secure credential / token store.

FEATURE: system-settings

Stores sensitive values (pull secret, SSH key, RH offline token,
Cloudflare token) in a JSON file with 0o600 permissions so that
installer forms can pre-populate them automatically.

Routes
------
GET  /api/settings          — which keys are set + masked previews
GET  /api/settings/reveal   — plaintext values (auth-gated, Settings page only)
POST /api/settings          — save / update one or more keys
DELETE /api/settings/<key>  — clear a single key

Internal
--------
``get_secret(key)``  — read a single secret from other modules
"""

import json
import os

from flask import Blueprint, jsonify, request, session

settings_bp = Blueprint('settings', __name__)

# ── File location ─────────────────────────────────────────────────────────────
# Stored at the project root (next to app.py), not inside views/
_APP_DIR      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SECRETS_FILE = os.path.join(_APP_DIR, 'secrets.json')

# All recognised key names (order controls UI display)
SETTINGS_KEYS = (
    'pull_secret',
    'ssh_public_key',
    'rh_offline_token',
    'cloudflare_token',
)

# ── Internal helpers ──────────────────────────────────────────────────────────

def _load() -> dict:
    if os.path.exists(_SECRETS_FILE):
        try:
            with open(_SECRETS_FILE) as fh:
                return json.load(fh)
        except Exception:
            pass
    return {}


def _save(data: dict) -> None:
    """Atomic write + restrict permissions."""
    tmp = _SECRETS_FILE + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, _SECRETS_FILE)
    try:
        os.chmod(_SECRETS_FILE, 0o600)
    except OSError:
        pass


def _mask(v: str) -> str:
    """Return a printable preview without leaking the real value."""
    if not v:
        return ''
    n = len(v)
    if n <= 8:
        return '•' * n
    return v[:4] + '•' * min(n - 8, 24) + v[-4:]


# ── Public helper used by other modules ───────────────────────────────────────

def get_secret(key: str, default: str = '') -> str:
    """Return a stored secret, or *default* if the key is not set."""
    return _load().get(key, default)


# ── Auth guard ────────────────────────────────────────────────────────────────

def _auth():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    return None


# ── Routes ────────────────────────────────────────────────────────────────────

@settings_bp.route('/api/settings', methods=['GET'])
def get_settings():
    """Return which keys are set and masked previews (safe to show in UI)."""
    err = _auth()
    if err:
        return err
    data = _load()
    return jsonify({
        'has':    {k: bool(data.get(k)) for k in SETTINGS_KEYS},
        'masked': {k: _mask(data.get(k, '')) for k in SETTINGS_KEYS},
    })


@settings_bp.route('/api/settings/reveal', methods=['GET'])
def reveal_settings():
    """Return actual plaintext values — only called by the Settings page."""
    err = _auth()
    if err:
        return err
    data = _load()
    return jsonify({k: data.get(k, '') for k in SETTINGS_KEYS})


@settings_bp.route('/api/settings', methods=['POST'])
def save_settings():
    """Save / update one or more keys.  Empty string clears the key."""
    err = _auth()
    if err:
        return err
    body    = request.get_json(force=True) or {}
    current = _load()
    changed = []
    for key in SETTINGS_KEYS:
        if key not in body:
            continue
        val = (body[key] or '').strip()
        if val:
            current[key] = val
            changed.append(key)
        else:
            if key in current:
                del current[key]
                changed.append(key)
    _save(current)
    return jsonify({'ok': True, 'updated': changed})


@settings_bp.route('/api/settings/<key>', methods=['DELETE'])
def clear_setting(key):
    """Delete a single key."""
    err = _auth()
    if err:
        return err
    if key not in SETTINGS_KEYS:
        return jsonify({'error': f'Unknown key: {key}'}), 400
    current = _load()
    if key in current:
        del current[key]
        _save(current)
    return jsonify({'ok': True})
