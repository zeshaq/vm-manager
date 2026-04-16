"""
OpenShift package — in-memory job store and persistence helpers.
"""

import json
import threading
import time

from .constants import WORK_DIR, _JOBS_FILE

# FEATURE: job-persistence
# per-job dict: job_id → { status, logs, progress, phase, result, config }
_jobs: dict = {}
_lock = threading.Lock()
_running_jobs: set = set()   # job_ids with active deploy threads in this process
_stop_jobs: set   = set()    # job_ids whose threads should exit at the next safe point


def _load_jobs():
    """Load persisted jobs from disk on startup."""
    global _jobs
    try:
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        if _JOBS_FILE.exists():
            with open(_JOBS_FILE) as f:
                _jobs = json.load(f)
    except Exception:
        _jobs = {}


def _save_jobs():
    """Persist current jobs dict to disk (called under _lock)."""
    try:
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _JOBS_FILE.with_suffix('.tmp')
        with open(tmp, 'w') as f:
            json.dump(_jobs, f)
        tmp.replace(_JOBS_FILE)
    except Exception:
        pass


# FEATURE: job-logging

def _job_log(job_id: str, msg: str, level: str = 'info'):
    ts = time.strftime('%H:%M:%S')
    with _lock:
        if job_id in _jobs:
            _jobs[job_id]['logs'].append({'ts': ts, 'msg': msg, 'level': level})
            _save_jobs()


# FEATURE: job-events

def _job_set(job_id: str, **kw):
    with _lock:
        if job_id in _jobs:
            _jobs[job_id].update(kw)
            _save_jobs()


def _job_event(job_id: str, event_type: str, **kw):
    """Append a structured event to job['events'] (max 300, newest last)."""
    ts = time.strftime('%H:%M:%S')
    ev = {'type': event_type, 'ts': ts, **kw}
    with _lock:
        if job_id in _jobs:
            evs = _jobs[job_id].setdefault('events', [])
            evs.append(ev)
            if len(evs) > 300:
                _jobs[job_id]['events'] = evs[-300:]
            _save_jobs()


# Load on import
_load_jobs()
