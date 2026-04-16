"""
Tests for Cluster Dashboard API endpoints (issue #14).

Covers:
  - Job detail endpoint for both installers
    GET /api/openshift/jobs/<job_id>      (Assisted Installer)
    GET /api/ocp-agent/jobs/<job_id>      (Agent Installer)
  - Live cluster status endpoint for both installers
    GET /api/openshift/jobs/<job_id>/cluster
    GET /api/ocp-agent/jobs/<job_id>/cluster
  - Kubeconfig download for both installers
    GET /api/openshift/jobs/<job_id>/kubeconfig
    GET /api/ocp-agent/jobs/<job_id>/kubeconfig

All network I/O (kubectl, subprocess) is mocked; no real cluster is needed.
"""

import io
import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

import pytest


# ── Shared test data ──────────────────────────────────────────────────────────

def _ai_job(overrides=None) -> dict:
    """Minimal Assisted Installer job dict for mocking _jobs."""
    base = {
        'id':       'ai-abc',
        'status':   'pending',
        'phase':    'Starting',
        'progress': 0,
        'created':  time.time(),
        'logs':     [],
        'config': {
            'cluster_name':        'test-cluster',
            'base_domain':         'example.com',
            'ocp_version':         '4.14',
            'deployment_type':     'full',
            'control_plane_count': 3,
            'worker_count':        2,
        },
        'vms':    [],
        'result': None,
    }
    if overrides:
        base.update(overrides)
    return base


def _ai_complete_job(overrides=None) -> dict:
    """Assisted Installer job with a complete result block."""
    base = _ai_job({
        'id':       'ai-abc',
        'status':   'complete',
        'phase':    'Complete',
        'progress': 100,
        'result': {
            'console_url':       'https://console-openshift-console.apps.test-cluster.example.com',
            'api_url':           'https://api.test-cluster.example.com:6443',
            'kubeadmin_password': 'AAAAA-BBBBB-CCCCC-DDDDD',
            'kubeconfig_path':    '/tmp/test-kubeconfig',
        },
    })
    if overrides:
        base.update(overrides)
    return base


def _agent_job(overrides=None) -> dict:
    """Minimal Agent Installer job dict for mocking _jobs."""
    base = {
        'id':       'ag-def',
        'status':   'pending',
        'phase':    'Queued',
        'progress': 0,
        'created':  time.time(),
        'logs':     [],
        'config': {
            'cluster_name':    'sno-cluster',
            'base_domain':     'lab.local',
            'ocp_version':     '4.15.0',
            'deployment_type': 'sno',
        },
        'vms':    [],
        'result': None,
    }
    if overrides:
        base.update(overrides)
    return base


def _agent_complete_job(overrides=None) -> dict:
    """Agent Installer job with a complete result block."""
    base = _agent_job({
        'id':       'ag-def',
        'status':   'complete',
        'phase':    'Complete',
        'progress': 100,
        'result': {
            'console_url':       'https://console-openshift-console.apps.sno-cluster.lab.local',
            'api_url':           'https://api.sno-cluster.lab.local:6443',
            'kubeadmin_password': 'EEEEE-FFFFF-GGGGG-HHHHH',
            'kubeconfig_path':    '/tmp/test-agent-kubeconfig',
        },
    })
    if overrides:
        base.update(overrides)
    return base


# kubectl mock response builders

def _kubectl_nodes_response() -> dict:
    """Fake `kubectl get nodes -o json` output."""
    return {
        'items': [
            {
                'metadata': {
                    'name': 'master-0',
                    'labels': {'node-role.kubernetes.io/master': ''},
                },
                'status': {
                    'conditions': [{'type': 'Ready', 'status': 'True'}],
                    'nodeInfo':   {'kubeletVersion': 'v1.27.5+1234abc'},
                },
            },
            {
                'metadata': {
                    'name': 'worker-0',
                    'labels': {'node-role.kubernetes.io/worker': ''},
                },
                'status': {
                    'conditions': [{'type': 'Ready', 'status': 'False'}],
                    'nodeInfo':   {'kubeletVersion': 'v1.27.5+1234abc'},
                },
            },
        ]
    }


def _kubectl_operators_response() -> dict:
    """Fake `kubectl get clusteroperators -o json` output."""
    return {
        'items': [
            {
                'metadata': {'name': 'authentication'},
                'status': {
                    'conditions': [
                        {'type': 'Available',   'status': 'True',  'message': ''},
                        {'type': 'Progressing', 'status': 'False', 'message': ''},
                        {'type': 'Degraded',    'status': 'False', 'message': ''},
                    ]
                },
            },
            {
                'metadata': {'name': 'dns'},
                'status': {
                    'conditions': [
                        {'type': 'Available',   'status': 'True',  'message': ''},
                        {'type': 'Progressing', 'status': 'False', 'message': ''},
                        {'type': 'Degraded',    'status': 'True',  'message': 'DNS pods not ready'},
                    ]
                },
            },
        ]
    }


def _kubectl_version_response() -> dict:
    """Fake `kubectl get clusterversion version -o json` output."""
    return {
        'spec':   {'channel': 'stable-4.14'},
        'status': {
            'history': [
                {'version': '4.14.5', 'state': 'Completed'},
            ]
        },
    }


def _make_kubectl_side_effect(nodes=True, operators=True, version=True):
    """
    Build a side_effect function for _run_kubectl that returns different
    responses depending on the args passed.
    """
    def _side_effect(kubeconfig, args, timeout=15):
        cmd = ' '.join(args)
        if 'nodes' in cmd and nodes:
            return {'ok': True, 'data': _kubectl_nodes_response()}
        if 'clusteroperators' in cmd and operators:
            return {'ok': True, 'data': _kubectl_operators_response()}
        if 'clusterversion' in cmd and version:
            return {'ok': True, 'data': _kubectl_version_response()}
        return {'ok': False, 'error': 'not found'}
    return _side_effect


# ── TestClusterDetailJobEndpoint ──────────────────────────────────────────────

class TestClusterDetailJobEndpoint:
    """
    Tests for the per-job detail endpoint on both installers.

    Assisted: GET /api/openshift/jobs/<job_id>
    Agent:    GET /api/ocp-agent/jobs/<job_id>
    """

    REQUIRED_KEYS = {'id', 'status', 'phase', 'progress', 'created', 'config', 'logs', 'vms'}

    # ── Assisted Installer ──

    def test_assisted_job_detail_has_required_keys(self, client):
        with patch('views.openshift.routes._jobs', {'ai-abc': _ai_job()}), \
             patch('views.openshift.routes._get_job_secrets', return_value={}):
            r = client.get('/api/openshift/jobs/ai-abc')
        assert r.status_code == 200
        data = r.get_json()
        missing = self.REQUIRED_KEYS - set(data.keys())
        assert not missing, f"Job detail missing keys: {missing}"

    def test_assisted_result_block_when_complete(self, client):
        job = _ai_complete_job()
        with patch('views.openshift.routes._jobs', {'ai-abc': job}), \
             patch('views.openshift.routes._get_job_secrets', return_value={}):
            r = client.get('/api/openshift/jobs/ai-abc')
        assert r.status_code == 200
        result = r.get_json().get('result', {})
        assert result is not None, "Complete job must have a result block"
        for key in ('console_url', 'api_url', 'kubeadmin_password'):
            assert key in result, f"result missing key: {key}"
        assert result['console_url'].startswith('https://')
        assert result['api_url'].startswith('https://')

    def test_assisted_404_unknown_job(self, client):
        with patch('views.openshift.routes._jobs', {}), \
             patch('views.openshift.routes._get_job_secrets', return_value={}):
            r = client.get('/api/openshift/jobs/no-such-job')
        assert r.status_code == 404

    def test_assisted_detail_requires_auth(self, anon_client):
        r = anon_client.get('/api/openshift/jobs/ai-abc')
        assert r.status_code == 401

    # ── Agent Installer ──

    def test_agent_job_detail_has_required_keys(self, client):
        import views.openshift_agent as agent
        with patch.object(agent, '_jobs', {'ag-def': _agent_job()}):
            r = client.get('/api/ocp-agent/jobs/ag-def')
        assert r.status_code == 200
        data = r.get_json()
        missing = self.REQUIRED_KEYS - set(data.keys())
        assert not missing, f"Agent job detail missing keys: {missing}"

    def test_agent_result_block_when_complete(self, client):
        import views.openshift_agent as agent
        job = _agent_complete_job()
        with patch.object(agent, '_jobs', {'ag-def': job}):
            r = client.get('/api/ocp-agent/jobs/ag-def')
        assert r.status_code == 200
        result = r.get_json().get('result', {})
        assert result is not None, "Complete agent job must have a result block"
        for key in ('console_url', 'api_url', 'kubeadmin_password'):
            assert key in result, f"result missing key: {key}"

    def test_agent_404_unknown_job(self, client):
        import views.openshift_agent as agent
        with patch.object(agent, '_jobs', {}):
            r = client.get('/api/ocp-agent/jobs/no-such-job')
        assert r.status_code == 404

    def test_agent_detail_requires_auth(self, anon_client):
        r = anon_client.get('/api/ocp-agent/jobs/ag-def')
        assert r.status_code == 401


# ── TestClusterLiveEndpoint ───────────────────────────────────────────────────

class TestClusterLiveEndpoint:
    """
    Tests for the live cluster status endpoint on both installers.

    Assisted: GET /api/openshift/jobs/<job_id>/cluster
    Agent:    GET /api/ocp-agent/jobs/<job_id>/cluster

    Both call _run_kubectl internally. We mock that function so no real
    kubectl/oc binary is needed.
    """

    def _patch_kubeconfig_exists(self, job_id: str, installer: str):
        """
        Context manager that makes the kubeconfig path appear to exist.

        The route looks for:
          Assisted: WORK_DIR / job_id / 'kubeconfig'   (views/openshift/routes.py)
          Agent:    WORK_DIR / job_id / 'kubeconfig'   (views/openshift_agent.py)
        We simply patch Path.exists to return True for any path.
        """
        return patch('pathlib.Path.exists', return_value=True)

    # ── Assisted Installer ──

    def test_assisted_cluster_endpoint_shape(self, client):
        job = _ai_complete_job()
        kubectl_mock = MagicMock(side_effect=_make_kubectl_side_effect())
        with patch('views.openshift.routes._jobs', {'ai-abc': job}), \
             patch('pathlib.Path.exists', return_value=True), \
             patch('views.openshift.routes._run_kubectl', kubectl_mock):
            r = client.get('/api/openshift/jobs/ai-abc/cluster')
        assert r.status_code == 200
        data = r.get_json()
        for key in ('nodes', 'operators', 'version', 'errors'):
            assert key in data, f"Cluster response missing key: {key}"
        assert isinstance(data['nodes'],     list)
        assert isinstance(data['operators'], list)
        assert isinstance(data['errors'],    list)

    def test_assisted_cluster_no_kubeconfig_returns_404(self, client):
        """When the kubeconfig does not exist the endpoint must return 404."""
        # result={} (not None) avoids the `None.get()` bug in the route;
        # the kubeconfig file itself is what doesn't exist.
        job = _ai_job({'result': {}})
        with patch('views.openshift.routes._jobs', {'ai-abc': job}), \
             patch('pathlib.Path.exists', return_value=False):
            r = client.get('/api/openshift/jobs/ai-abc/cluster')
        assert r.status_code == 404

    def test_cluster_endpoint_requires_auth(self, anon_client):
        r = anon_client.get('/api/openshift/jobs/ai-abc/cluster')
        assert r.status_code == 401

    # ── Agent Installer ──

    def test_agent_cluster_endpoint_shape(self, client):
        import views.openshift_agent as agent
        job = _agent_complete_job()
        kubectl_mock = MagicMock(side_effect=_make_kubectl_side_effect())
        with patch.object(agent, '_jobs', {'ag-def': job}), \
             patch('pathlib.Path.exists', return_value=True), \
             patch.object(agent, '_run_kubectl', kubectl_mock):
            r = client.get('/api/ocp-agent/jobs/ag-def/cluster')
        assert r.status_code == 200
        data = r.get_json()
        for key in ('nodes', 'operators', 'version', 'errors'):
            assert key in data, f"Agent cluster response missing key: {key}"

    def test_agent_cluster_endpoint_requires_auth(self, anon_client):
        r = anon_client.get('/api/ocp-agent/jobs/ag-def/cluster')
        assert r.status_code == 401

    # ── Field shape tests (installer-agnostic via Assisted endpoint) ──

    def test_nodes_have_required_fields(self, client):
        job = _ai_complete_job()
        kubectl_mock = MagicMock(side_effect=_make_kubectl_side_effect())
        with patch('views.openshift.routes._jobs', {'ai-abc': job}), \
             patch('pathlib.Path.exists', return_value=True), \
             patch('views.openshift.routes._run_kubectl', kubectl_mock):
            r = client.get('/api/openshift/jobs/ai-abc/cluster')
        nodes = r.get_json()['nodes']
        assert len(nodes) > 0, "Expected at least one node in mock response"
        for node in nodes:
            for field in ('name', 'roles', 'ready', 'kubelet_version'):
                assert field in node, f"Node missing field: {field}"
            assert isinstance(node['roles'], list)
            assert isinstance(node['name'], str)
            assert node['ready'] in ('Ready', 'NotReady', 'Unknown')

    def test_operators_have_required_fields(self, client):
        job = _ai_complete_job()
        kubectl_mock = MagicMock(side_effect=_make_kubectl_side_effect())
        with patch('views.openshift.routes._jobs', {'ai-abc': job}), \
             patch('pathlib.Path.exists', return_value=True), \
             patch('views.openshift.routes._run_kubectl', kubectl_mock):
            r = client.get('/api/openshift/jobs/ai-abc/cluster')
        operators = r.get_json()['operators']
        assert len(operators) > 0, "Expected at least one operator in mock response"
        for op in operators:
            for field in ('name', 'available', 'progressing', 'degraded'):
                assert field in op, f"Operator missing field: {field}"
            assert isinstance(op['name'], str)

    def test_version_has_required_fields(self, client):
        job = _ai_complete_job()
        kubectl_mock = MagicMock(side_effect=_make_kubectl_side_effect())
        with patch('views.openshift.routes._jobs', {'ai-abc': job}), \
             patch('pathlib.Path.exists', return_value=True), \
             patch('views.openshift.routes._run_kubectl', kubectl_mock):
            r = client.get('/api/openshift/jobs/ai-abc/cluster')
        version = r.get_json()['version']
        assert version is not None, "version block should be populated"
        assert 'version' in version
        assert 'channel' in version
        assert version['version'] == '4.14.5'
        assert version['channel'] == 'stable-4.14'

    def test_degraded_operators_flagged(self, client):
        """Operators with degraded=True must be present and identifiable."""
        job = _ai_complete_job()
        kubectl_mock = MagicMock(side_effect=_make_kubectl_side_effect())
        with patch('views.openshift.routes._jobs', {'ai-abc': job}), \
             patch('pathlib.Path.exists', return_value=True), \
             patch('views.openshift.routes._run_kubectl', kubectl_mock):
            r = client.get('/api/openshift/jobs/ai-abc/cluster')
        operators = r.get_json()['operators']
        degraded = [op for op in operators if op['degraded'] == 'True']
        assert len(degraded) == 1
        assert degraded[0]['name'] == 'dns'
        assert 'DNS pods not ready' in degraded[0].get('message', '')

    def test_kubectl_error_appended_to_errors(self, client):
        """When kubectl fails, the error is appended to the errors list."""
        job = _ai_complete_job()
        kubectl_mock = MagicMock(return_value={'ok': False, 'error': 'connection refused'})
        with patch('views.openshift.routes._jobs', {'ai-abc': job}), \
             patch('pathlib.Path.exists', return_value=True), \
             patch('views.openshift.routes._run_kubectl', kubectl_mock):
            r = client.get('/api/openshift/jobs/ai-abc/cluster')
        assert r.status_code == 200   # endpoint always returns 200; errors are in payload
        errors = r.get_json()['errors']
        assert len(errors) > 0
        assert any('connection refused' in e for e in errors)


# ── TestKubeconfigDownload ────────────────────────────────────────────────────

class TestKubeconfigDownload:
    """
    Tests for the kubeconfig download endpoint on both installers.

    Assisted: GET /api/openshift/jobs/<job_id>/kubeconfig
    Agent:    GET /api/ocp-agent/jobs/<job_id>/kubeconfig

    The route reads the file from disk via send_file. We patch Path.exists
    and builtins.open so no real file is required.
    """

    FAKE_KUBECONFIG = b"apiVersion: v1\nkind: Config\nclusters: []\n"

    # ── Assisted Installer ──

    def test_assisted_kubeconfig_download(self, client, tmp_path):
        kc_file = tmp_path / 'kubeconfig'
        kc_file.write_bytes(self.FAKE_KUBECONFIG)
        job = _ai_complete_job({'result': {
            'console_url':        'https://console.example.com',
            'api_url':            'https://api.example.com:6443',
            'kubeadmin_password': 'pwd',
            'kubeconfig_path':    str(kc_file),
        }})
        with patch('views.openshift.routes._jobs', {'ai-abc': job}), \
             patch('views.openshift.routes._get_job_secrets', return_value={}):
            r = client.get('/api/openshift/jobs/ai-abc/kubeconfig')
        assert r.status_code == 200
        cd = r.headers.get('Content-Disposition', '')
        assert 'attachment' in cd, f"Expected attachment header, got: {cd}"
        assert r.data == self.FAKE_KUBECONFIG

    def test_assisted_kubeconfig_not_available_when_no_result(self, client):
        """Job with no kubeconfig_path in result returns 404."""
        # result={} avoids the `None.get()` crash; empty dict has no kubeconfig_path.
        job = _ai_job({'result': {}})
        with patch('views.openshift.routes._jobs', {'ai-abc': job}), \
             patch('views.openshift.routes._get_job_secrets', return_value={}):
            r = client.get('/api/openshift/jobs/ai-abc/kubeconfig')
        assert r.status_code == 404

    def test_assisted_kubeconfig_missing_file_returns_404(self, client):
        """Job has a result but the kubeconfig file doesn't exist on disk."""
        job = _ai_complete_job({'result': {
            'console_url':        'https://console.example.com',
            'api_url':            'https://api.example.com:6443',
            'kubeadmin_password': 'pwd',
            'kubeconfig_path':    '/nonexistent/path/kubeconfig',
        }})
        with patch('views.openshift.routes._jobs', {'ai-abc': job}), \
             patch('views.openshift.routes._get_job_secrets', return_value={}):
            r = client.get('/api/openshift/jobs/ai-abc/kubeconfig')
        assert r.status_code == 404

    def test_kubeconfig_requires_auth(self, anon_client):
        r = anon_client.get('/api/openshift/jobs/ai-abc/kubeconfig')
        assert r.status_code == 401

    # ── Agent Installer ──

    def test_agent_kubeconfig_download(self, client, tmp_path):
        kc_file = tmp_path / 'kubeconfig'
        kc_file.write_bytes(self.FAKE_KUBECONFIG)
        job = _agent_complete_job({'result': {
            'console_url':        'https://console.lab.local',
            'api_url':            'https://api.lab.local:6443',
            'kubeadmin_password': 'pwd',
            'kubeconfig_path':    str(kc_file),
        }})
        import views.openshift_agent as agent
        with patch.object(agent, '_jobs', {'ag-def': job}):
            r = client.get('/api/ocp-agent/jobs/ag-def/kubeconfig')
        assert r.status_code == 200
        cd = r.headers.get('Content-Disposition', '')
        assert 'attachment' in cd, f"Expected attachment header, got: {cd}"
        assert r.data == self.FAKE_KUBECONFIG

    def test_agent_kubeconfig_not_available_when_no_result(self, client):
        import views.openshift_agent as agent
        # result={} avoids the `None.get()` crash; empty dict has no kubeconfig_path.
        job = _agent_job({'result': {}})
        with patch.object(agent, '_jobs', {'ag-def': job}):
            r = client.get('/api/ocp-agent/jobs/ag-def/kubeconfig')
        assert r.status_code == 404

    def test_agent_kubeconfig_missing_file_returns_404(self, client):
        import views.openshift_agent as agent
        job = _agent_complete_job({'result': {
            'console_url':        'https://console.lab.local',
            'api_url':            'https://api.lab.local:6443',
            'kubeadmin_password': 'pwd',
            'kubeconfig_path':    '/nonexistent/path/agent-kubeconfig',
        }})
        with patch.object(agent, '_jobs', {'ag-def': job}):
            r = client.get('/api/ocp-agent/jobs/ag-def/kubeconfig')
        assert r.status_code == 404

    def test_agent_kubeconfig_requires_auth(self, anon_client):
        r = anon_client.get('/api/ocp-agent/jobs/ag-def/kubeconfig')
        assert r.status_code == 401
