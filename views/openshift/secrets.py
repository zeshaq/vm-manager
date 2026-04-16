"""
OpenShift package — per-job credential secrets store.

Credentials (offline_token, pull_secret) are kept in a separate file with
0600 permissions so they survive service restarts for resume capability,
but are never included in the UI-visible jobs.json config summary.
"""

import json
import os

from .constants import WORK_DIR, _SECRETS_FILE
from .job_store import _lock

# FEATURE: secrets-persistence
_secrets: dict = {}   # job_id → { offline_token, pull_secret }


def _load_secrets():
    global _secrets
    try:
        if _SECRETS_FILE.exists():
            with open(_SECRETS_FILE) as f:
                _secrets = json.load(f)
    except Exception:
        _secrets = {}


def _save_secrets():
    try:
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        with open(_SECRETS_FILE, 'w') as f:
            json.dump(_secrets, f)
        os.chmod(_SECRETS_FILE, 0o600)
    except Exception:
        pass


def _store_job_secrets(job_id: str, offline_token: str, pull_secret: str):
    with _lock:
        _secrets[job_id] = {'offline_token': offline_token, 'pull_secret': pull_secret}
        _save_secrets()


def _get_job_secrets(job_id: str) -> dict:
    return dict(_secrets.get(job_id, {}))


def _delete_job_secrets(job_id: str):
    with _lock:
        _secrets.pop(job_id, None)
        _save_secrets()


# Load on import
_load_secrets()
