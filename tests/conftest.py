"""
Shared pytest fixtures for VM Manager tests.
"""
import json
import sys
import types
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── Stub heavy native dependencies before any import ──────────────────────────
# libvirt, psutil, simplepam are not available in CI / unit-test environments.

def _make_libvirt_stub():
    lv = types.ModuleType('libvirt')
    lv.open = MagicMock(return_value=MagicMock())
    lv.libvirtError = Exception
    # Real libvirt integer values — must match to pass state-string tests
    lv.VIR_DOMAIN_NOSTATE    = 0
    lv.VIR_DOMAIN_RUNNING    = 1
    lv.VIR_DOMAIN_BLOCKED    = 2
    lv.VIR_DOMAIN_PAUSED     = 3
    lv.VIR_DOMAIN_SHUTDOWN   = 4
    lv.VIR_DOMAIN_SHUTOFF    = 5
    lv.VIR_DOMAIN_CRASHED    = 6
    lv.VIR_DOMAIN_PMSUSPENDED = 7
    lv.VIR_DOMAIN_AFFECT_LIVE   = 1
    lv.VIR_DOMAIN_AFFECT_CONFIG = 2
    return lv


def _make_flask_limiter_stub():
    """Stub flask_limiter so tests run without the package installed."""
    mod = types.ModuleType('flask_limiter')
    # Limiter must be callable and return a decorator-like object
    limiter_instance = MagicMock()
    limiter_instance.limit = lambda *a, **kw: (lambda f: f)  # pass-through decorator
    limiter_instance.init_app = MagicMock()
    mod.Limiter = MagicMock(return_value=limiter_instance)
    # flask_limiter.util submodule
    util_mod = types.ModuleType('flask_limiter.util')
    util_mod.get_remote_address = MagicMock(return_value='127.0.0.1')
    sys.modules['flask_limiter.util'] = util_mod
    return mod


def _make_docker_stub():
    """Stub docker + docker.errors so tests run without docker-py installed."""
    docker_mod = types.ModuleType('docker')
    docker_mod.from_env = MagicMock(return_value=MagicMock())
    docker_mod.DockerClient = MagicMock()
    errors_mod = types.ModuleType('docker.errors')
    errors_mod.DockerException = Exception
    errors_mod.NotFound        = Exception
    errors_mod.APIError        = Exception
    sys.modules['docker.errors'] = errors_mod
    docker_mod.errors = errors_mod
    return docker_mod


for mod_name in ('libvirt', 'psutil', 'simplepam', 'flask_limiter', 'docker'):
    if mod_name not in sys.modules:
        if mod_name == 'libvirt':
            sys.modules[mod_name] = _make_libvirt_stub()
        elif mod_name == 'flask_limiter':
            sys.modules[mod_name] = _make_flask_limiter_stub()
        elif mod_name == 'docker':
            sys.modules[mod_name] = _make_docker_stub()
        else:
            sys.modules[mod_name] = MagicMock()

# ── Flask app fixture ─────────────────────────────────────────────────────────

@pytest.fixture(scope='session')
def app():
    """Create a Flask test application with all blueprints registered."""
    # Patch libvirt.open at app-init time so blueprints don't crash
    with patch('libvirt.open', return_value=MagicMock()):
        import app as app_module
        flask_app = app_module.app
        flask_app.config.update({
            'TESTING': True,
            'SECRET_KEY': 'test-secret-key',
            'WTF_CSRF_ENABLED': False,
        })
        yield flask_app


@pytest.fixture
def client(app):
    """Flask test client with an authenticated session."""
    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess['username'] = 'testuser'
        yield c


@pytest.fixture
def anon_client(app):
    """Flask test client with NO session (unauthenticated)."""
    with app.test_client() as c:
        yield c


# ── OpenShift job helpers ─────────────────────────────────────────────────────

@pytest.fixture
def ocp_job():
    """A minimal in-memory OpenShift job dict."""
    return {
        'id':        'test-job-001',
        'status':    'pending',
        'phase':     'Starting',
        'progress':  0,
        'logs':      [],
        'events':    [],
        'nodes':     [],
        'vms':       [],
        'mac_map':   {},
        'config':    {
            'cluster_name':   'test-cluster',
            'base_domain':    'example.com',
            'ocp_version':    '4.14',
            'deployment_type': 'multi',
            'control_plane_count': 3,
            'worker_count':   2,
        },
    }


@pytest.fixture
def sample_host():
    """A minimal Assisted Installer host dict with inventory."""
    return {
        'id':                 'host-uuid-001',
        'requested_hostname': '',
        'status':             'known',
        'role':               'master',
        'created_at':         '2026-04-16T10:00:00Z',
        'inventory': json.dumps({
            'interfaces': [
                {'name': 'enp1s0', 'mac_address': '52:54:00:aa:bb:cc'},
            ]
        }),
        'progress': {
            'current_stage':           'Installing',
            'installation_percentage': 50,
            'stage_updated_at':        '2026-04-16T10:05:00Z',
        },
    }
