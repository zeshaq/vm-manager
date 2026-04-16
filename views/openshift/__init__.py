"""
views.openshift — OpenShift Deployment package.

Public API re-exports everything tests and app.py need from this package's
sub-modules so that `import views.openshift as ocp; ocp._jobs` etc. all work.

Dependency order (no circular imports):
  constants → job_store → secrets → iso_cache → ai_client
  → vm_ops → deploy → monitoring → routes → __init__
"""

# ── Constants / flags ─────────────────────────────────────────────────────────
from .constants import (
    AI_BASE,
    SSO_URL,
    WORK_DIR,
    _JOBS_FILE,
    _SECRETS_FILE,
    ISO_CACHE_DIR,
    _ISO_CACHE_FILE,
    _LIBVIRT,
    _PSUTIL,
)

# ── Job store ─────────────────────────────────────────────────────────────────
from .job_store import (
    _jobs,
    _lock,
    _running_jobs,
    _stop_jobs,
    _load_jobs,
    _save_jobs,
    _job_log,
    _job_set,
    _job_event,
)

# ── Secrets ───────────────────────────────────────────────────────────────────
from .secrets import (
    _secrets,
    _load_secrets,
    _save_secrets,
    _store_job_secrets,
    _get_job_secrets,
    _delete_job_secrets,
)

# ── ISO cache ─────────────────────────────────────────────────────────────────
from .iso_cache import (
    _iso_cache,
    _iso_lock,
    _iso_fingerprint,
    _load_iso_cache,
    _save_iso_cache,
    _get_cached_iso,
    _store_iso_cache,
)

# ── AI client ─────────────────────────────────────────────────────────────────
from .ai_client import (
    _token_cache,
    _get_access_token,
    _ai,
)

# ── VM operations ─────────────────────────────────────────────────────────────
from .vm_ops import (
    _make_mac,
    _vm_xml,
    _build_nmstate_yaml,
    _eject_cdroms,
    _insert_cdroms,
    _reboot_vms,
)

# ── Deploy ────────────────────────────────────────────────────────────────────
from .deploy import (
    _parse_host_mac,
    _run_deploy,
    _resume_pending_jobs,
)

# ── Monitoring ────────────────────────────────────────────────────────────────
from .monitoring import (
    _collect_credentials,
    _monitor_install_thread,
)

# ── Routes (blueprint) ────────────────────────────────────────────────────────
from .routes import ocp_bp

__all__ = [
    # constants
    'AI_BASE', 'SSO_URL', 'WORK_DIR', '_LIBVIRT', '_PSUTIL',
    # job store
    '_jobs', '_lock', '_running_jobs', '_stop_jobs',
    '_job_log', '_job_set', '_job_event',
    # ai client
    '_token_cache', '_get_access_token', '_ai',
    # vm ops
    '_make_mac', '_eject_cdroms', '_reboot_vms', '_insert_cdroms',
    # deploy
    '_parse_host_mac', '_run_deploy',
    # monitoring
    '_collect_credentials', '_monitor_install_thread',
    # iso cache
    '_iso_fingerprint',
    # blueprint
    'ocp_bp',
]
