"""
OpenShift package — shared constants and capability flags.
"""

# FEATURE: api-endpoints
from pathlib import Path
import threading

try:
    import libvirt as _libvirt_mod
    _LIBVIRT = True
except ImportError:
    _LIBVIRT = False

try:
    import psutil as _psutil_mod
    _PSUTIL = True
except ImportError:
    _PSUTIL = False

# Red Hat Assisted Installer + SSO endpoints
AI_BASE = 'https://api.openshift.com/api/assisted-install/v2'
SSO_URL = (
    'https://sso.redhat.com/auth/realms/redhat-external'
    '/protocol/openid-connect/token'
)

# FEATURE: filesystem-paths
WORK_DIR        = Path.home() / 'hypercloud' / 'openshift'
_JOBS_FILE      = WORK_DIR / 'jobs.json'
_SECRETS_FILE   = WORK_DIR / '.job_secrets'
ISO_CACHE_DIR   = WORK_DIR / 'iso-cache'
_ISO_CACHE_FILE = ISO_CACHE_DIR / 'cache.json'
