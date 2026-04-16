"""
OpenShift package — ISO cache (fingerprint-keyed, DHCP deployments only).

Static IP deployments must never use the cache — their MAC/IP config
is deployment-specific and embedded in the infra-env/ISO.
"""

import hashlib
import json
import threading
import time
from pathlib import Path

from .constants import ISO_CACHE_DIR, _ISO_CACHE_FILE

# FEATURE: iso-cache-state
_iso_cache: dict = {}   # fingerprint → {infra_env_id, iso_path, ocp_version, downloaded_at}
_iso_lock = threading.Lock()


# FEATURE: iso-fingerprint

def _iso_fingerprint(ocp_version: str, pull_secret: str, ssh_public_key: str) -> str:
    """Stable key for a DHCP (version, pull_secret, ssh_key) combination."""
    raw = f'dhcp|{ocp_version}|{pull_secret.strip()}|{ssh_public_key.strip()}'
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# FEATURE: iso-cache-persistence

def _load_iso_cache():
    global _iso_cache
    try:
        ISO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        if _ISO_CACHE_FILE.exists():
            with open(_ISO_CACHE_FILE) as f:
                _iso_cache = json.load(f)
    except Exception:
        _iso_cache = {}


def _save_iso_cache():
    try:
        ISO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(_ISO_CACHE_FILE, 'w') as f:
            json.dump(_iso_cache, f, indent=2)
    except Exception:
        pass


def _get_cached_iso(fingerprint: str):
    """Return (infra_env_id, iso_path) if a valid cached ISO exists, else (None, None)."""
    with _iso_lock:
        entry = _iso_cache.get(fingerprint)
    if not entry:
        return None, None
    iso_path = Path(entry['iso_path'])
    if not iso_path.exists():
        return None, None
    return entry['infra_env_id'], iso_path


def _store_iso_cache(fingerprint: str, infra_env_id: str, iso_path: Path,
                     ocp_version: str, pull_secret: str, ssh_public_key: str):
    with _iso_lock:
        _iso_cache[fingerprint] = {
            'infra_env_id':   infra_env_id,
            'iso_path':       str(iso_path),
            'ocp_version':    ocp_version,
            'downloaded_at':  time.time(),
            'ps_hint':        pull_secret.strip()[:6] + '…',
            'ssh_hint':       (ssh_public_key.strip()[:30] + '…') if ssh_public_key.strip() else '',
        }
        _save_iso_cache()


# Load on import
_load_iso_cache()
