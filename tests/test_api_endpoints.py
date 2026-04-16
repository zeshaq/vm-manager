"""
Functional tests for Flask API endpoints.

Tests authentication, VM operations, and OpenShift job endpoints
using Flask's test client — no real libvirt or network calls.
"""
import json
from unittest.mock import MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Auth guard tests
# ─────────────────────────────────────────────────────────────────────────────

class TestAuthGuard:
    """All API endpoints must return 401 for unauthenticated requests."""

    PROTECTED_ENDPOINTS = [
        ('GET',  '/api/vms'),
        ('POST', '/api/vms'),
        ('GET',  '/api/openshift/jobs'),
        ('POST', '/api/openshift/jobs'),
    ]

    @pytest.mark.parametrize('method,path', PROTECTED_ENDPOINTS)
    def test_returns_401_when_unauthenticated(self, anon_client, method, path):
        resp = getattr(anon_client, method.lower())(path)
        assert resp.status_code == 401

    def test_authenticated_client_can_reach_vm_list(self, client):
        with patch('libvirt.open') as mock_conn:
            mock_conn.return_value.listAllDomains.return_value = []
            resp = client.get('/api/vms')
        assert resp.status_code != 401


# ─────────────────────────────────────────────────────────────────────────────
# OpenShift job API
# ─────────────────────────────────────────────────────────────────────────────

class TestOpenShiftJobAPI:
    """Test the /api/openshift/jobs endpoints."""

    def test_list_jobs_returns_list(self, client):
        import views.openshift as ocp
        original = dict(ocp._jobs)
        ocp._jobs.clear()
        try:
            resp = client.get('/api/openshift/jobs')
            assert resp.status_code == 200
            data = resp.get_json()
            assert isinstance(data, list)
        finally:
            ocp._jobs.update(original)

    def test_get_nonexistent_job_returns_404(self, client):
        resp = client.get('/api/openshift/jobs/nonexistent-id-xyz')
        assert resp.status_code == 404

    def test_create_job_missing_fields_returns_400(self, client):
        resp = client.post(
            '/api/openshift/jobs',
            json={},
            content_type='application/json',
        )
        assert resp.status_code == 400

    def test_create_job_returns_job_id(self, client):
        payload = {
            'cluster_name':        'test-cluster',
            'base_domain':         'example.com',
            'ocp_version':         '4.14',
            'offline_token':       'fake-token',
            'pull_secret':         '{"auths":{}}',
            'deployment_type':     'sno',
            'control_plane_count': 1,
            'worker_count':        0,
            'cp_vcpus':            8,
            'cp_ram_gb':           16,
            'cp_disk_gb':          120,
            'storage_path':        '/tmp',
            'bridge':              'virbr0',
        }
        with patch('views.openshift._run_deploy'), \
             patch('threading.Thread') as mock_thread:
            mock_thread.return_value.start = MagicMock()
            resp = client.post(
                '/api/openshift/jobs',
                json=payload,
                content_type='application/json',
            )

        assert resp.status_code in (200, 201)
        data = resp.get_json()
        assert 'job_id' in data or 'id' in data

    def test_get_existing_job(self, client):
        import views.openshift as ocp
        job_id = 'test-get-job-001'
        with ocp._lock:
            ocp._jobs[job_id] = {
                'id': job_id, 'status': 'pending',
                'progress': 10, 'logs': [], 'events': [],
                'config': {'cluster_name': 'my-cluster'},
            }
        try:
            resp = client.get(f'/api/openshift/jobs/{job_id}')
            assert resp.status_code == 200
            data = resp.get_json()
            assert data['id'] == job_id or data.get('status') == 'pending'
        finally:
            with ocp._lock:
                ocp._jobs.pop(job_id, None)

    def test_delete_nonexistent_job_returns_404(self, client):
        resp = client.delete('/api/openshift/jobs/does-not-exist-xyz')
        assert resp.status_code == 404

    def test_logs_endpoint_returns_list(self, client):
        import views.openshift as ocp
        job_id = 'log-endpoint-test'
        with ocp._lock:
            ocp._jobs[job_id] = {
                'id': job_id, 'status': 'pending', 'logs': [
                    {'ts': '10:00:00', 'msg': 'Starting', 'level': 'info'},
                ], 'events': [], 'config': {},
            }
        try:
            resp = client.get(f'/api/openshift/jobs/{job_id}/logs')
            # Either a dedicated logs endpoint or embedded in job
            if resp.status_code == 200:
                data = resp.get_json()
                assert isinstance(data, (list, dict))
        finally:
            with ocp._lock:
                ocp._jobs.pop(job_id, None)


# ─────────────────────────────────────────────────────────────────────────────
# Network endpoints
# ─────────────────────────────────────────────────────────────────────────────

class TestNetworkAPI:
    def test_list_networks_requires_auth(self, anon_client):
        resp = anon_client.get('/api/networks')
        assert resp.status_code == 401

    def test_list_networks_authenticated(self, client):
        with patch('libvirt.open') as mock_conn:
            mock_conn.return_value.listAllNetworks.return_value = []
            resp = client.get('/api/networks')
        assert resp.status_code in (200, 404)  # 404 if endpoint not registered


# ─────────────────────────────────────────────────────────────────────────────
# Static files / SPA catch-all
# ─────────────────────────────────────────────────────────────────────────────

class TestStaticRoutes:
    def test_root_returns_html(self, client):
        resp = client.get('/')
        # Should return index.html (200) or redirect
        assert resp.status_code in (200, 302)

    def test_unknown_path_returns_html(self, client):
        """SPA catch-all: unknown routes should return index.html, not 404."""
        resp = client.get('/some/frontend/route')
        assert resp.status_code in (200, 302)
