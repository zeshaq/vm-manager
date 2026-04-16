"""
Tests for the OpenShift Clusters unified view (issue #13).

Verifies that:
  1. Both job-list endpoints return the expected JSON schema
  2. Jobs from both installers share the required keys for the unified UI
  3. The combined list can be merged, sorted, and filtered correctly
  4. Individual field types are correct (status is a string, progress is numeric, etc.)

No real network calls — all HTTP is mocked.
"""

import pytest
from unittest.mock import patch, MagicMock


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

# Minimum required keys the frontend OpenShiftClusters page consumes.
# NOTE: the two installers use slightly different shapes:
#   Assisted Installer → has 'config' dict
#   Agent Installer    → flattens config into top-level keys (cluster_name, ocp_version, deployment_type)
AI_REQUIRED_KEYS    = {'id', 'status', 'progress', 'phase', 'created', 'config'}
AGENT_REQUIRED_KEYS = {'id', 'status', 'progress', 'phase', 'created',
                       'cluster_name', 'ocp_version', 'deployment_type'}

VALID_STATUSES = {'pending', 'running', 'complete', 'failed'}


def _ai_job(overrides=None):
    """Return a minimal Assisted Installer job dict."""
    base = {
        'id':       'ai-abc12345',
        'status':   'complete',
        'phase':    'Complete',
        'progress': 100,
        'created':  1_713_200_000.0,
        'logs':     [],
        'config': {
            'cluster_name':        'my-cluster',
            'base_domain':         'example.com',
            'ocp_version':         '4.14',
            'deployment_type':     'full',
            'control_plane_count': 3,
            'worker_count':        2,
        },
    }
    if overrides:
        base.update(overrides)
    return base


def _agent_job(overrides=None):
    """Return a minimal Agent-based installer job dict."""
    base = {
        'id':       'ag-def67890',
        'status':   'running',
        'phase':    'Waiting for bootstrap',
        'progress': 60,
        'created':  1_713_100_000.0,
        'config': {
            'cluster_name':        'sno-cluster',
            'base_domain':         'lab.local',
            'ocp_version':         '4.15',
            'deployment_type':     'sno',
            'control_plane_count': 1,
            'worker_count':        0,
        },
    }
    if overrides:
        base.update(overrides)
    return base


# ─────────────────────────────────────────────────────────────────────────────
# Schema validation — Assisted Installer list endpoint
# ─────────────────────────────────────────────────────────────────────────────

class TestAssistedInstallerJobListSchema:
    """GET /api/openshift/jobs must return {jobs: [...]} with required keys."""

    def test_response_has_jobs_key(self, client):
        with patch('views.openshift.routes._jobs', {'ai-abc12345': _ai_job()}), \
             patch('views.openshift.routes._get_job_secrets', return_value={}):
            r = client.get('/api/openshift/jobs')
        assert r.status_code == 200
        data = r.get_json()
        assert 'jobs' in data, "Response must contain 'jobs' key"

    def test_jobs_is_a_list(self, client):
        with patch('views.openshift.routes._jobs', {'ai-abc12345': _ai_job()}), \
             patch('views.openshift.routes._get_job_secrets', return_value={}):
            r = client.get('/api/openshift/jobs')
        assert isinstance(r.get_json()['jobs'], list)

    def test_each_job_has_required_keys(self, client):
        jobs_store = {
            'ai-abc12345': _ai_job(),
            'ai-bcd23456': _ai_job({'id': 'ai-bcd23456', 'status': 'running'}),
        }
        with patch('views.openshift.routes._jobs', jobs_store), \
             patch('views.openshift.routes._get_job_secrets', return_value={}):
            r = client.get('/api/openshift/jobs')
        for job in r.get_json()['jobs']:
            missing = AI_REQUIRED_KEYS - set(job.keys())
            assert not missing, f"Job missing keys: {missing}"

    def test_status_is_valid_string(self, client):
        with patch('views.openshift.routes._jobs', {'ai-abc12345': _ai_job()}), \
             patch('views.openshift.routes._get_job_secrets', return_value={}):
            jobs = client.get('/api/openshift/jobs').get_json()['jobs']
        for j in jobs:
            assert isinstance(j['status'], str)
            assert j['status'] in VALID_STATUSES, f"Unexpected status: {j['status']}"

    def test_progress_is_numeric(self, client):
        with patch('views.openshift.routes._jobs', {'ai-abc12345': _ai_job()}), \
             patch('views.openshift.routes._get_job_secrets', return_value={}):
            jobs = client.get('/api/openshift/jobs').get_json()['jobs']
        for j in jobs:
            assert isinstance(j['progress'], (int, float)), \
                f"progress must be numeric, got {type(j['progress'])}"
            assert 0 <= j['progress'] <= 100

    def test_empty_list_when_no_jobs(self, client):
        with patch('views.openshift.routes._jobs', {}), \
             patch('views.openshift.routes._get_job_secrets', return_value={}):
            r = client.get('/api/openshift/jobs')
        assert r.get_json() == {'jobs': []}

    def test_config_has_cluster_name(self, client):
        with patch('views.openshift.routes._jobs', {'ai-abc12345': _ai_job()}), \
             patch('views.openshift.routes._get_job_secrets', return_value={}):
            jobs = client.get('/api/openshift/jobs').get_json()['jobs']
        for j in jobs:
            assert 'config' in j
            assert 'cluster_name' in j['config'], "config must contain cluster_name"

    def test_unauthenticated_returns_401(self, anon_client):
        r = anon_client.get('/api/openshift/jobs')
        assert r.status_code == 401


# ─────────────────────────────────────────────────────────────────────────────
# Schema validation — Agent installer list endpoint
# ─────────────────────────────────────────────────────────────────────────────

class TestAgentInstallerJobListSchema:
    """GET /api/ocp-agent/jobs must return {jobs: [...]} with required keys."""

    def test_response_has_jobs_key(self, client):
        import views.openshift_agent as agent
        with patch.object(agent, '_jobs', {'ag-def67890': _agent_job()}):
            r = client.get('/api/ocp-agent/jobs')
        assert r.status_code == 200
        assert 'jobs' in r.get_json()

    def test_jobs_is_a_list(self, client):
        import views.openshift_agent as agent
        with patch.object(agent, '_jobs', {'ag-def67890': _agent_job()}):
            r = client.get('/api/ocp-agent/jobs')
        assert isinstance(r.get_json()['jobs'], list)

    def test_each_job_has_required_keys(self, client):
        import views.openshift_agent as agent
        jobs_store = {
            'ag-def67890': _agent_job(),
            'ag-efg01234': _agent_job({'id': 'ag-efg01234', 'status': 'complete'}),
        }
        with patch.object(agent, '_jobs', jobs_store):
            r = client.get('/api/ocp-agent/jobs')
        for job in r.get_json()['jobs']:
            missing = AGENT_REQUIRED_KEYS - set(job.keys())
            assert not missing, f"Agent job missing keys: {missing}"

    def test_status_is_valid_string(self, client):
        import views.openshift_agent as agent
        with patch.object(agent, '_jobs', {'ag-def67890': _agent_job()}):
            jobs = client.get('/api/ocp-agent/jobs').get_json()['jobs']
        for j in jobs:
            assert isinstance(j['status'], str)
            assert j['status'] in VALID_STATUSES

    def test_progress_is_numeric(self, client):
        import views.openshift_agent as agent
        with patch.object(agent, '_jobs', {'ag-def67890': _agent_job()}):
            jobs = client.get('/api/ocp-agent/jobs').get_json()['jobs']
        for j in jobs:
            assert isinstance(j['progress'], (int, float))
            assert 0 <= j['progress'] <= 100

    def test_empty_list_when_no_jobs(self, client):
        import views.openshift_agent as agent
        with patch.object(agent, '_jobs', {}):
            r = client.get('/api/ocp-agent/jobs')
        assert r.get_json() == {'jobs': []}

    def test_unauthenticated_returns_401(self, anon_client):
        r = anon_client.get('/api/ocp-agent/jobs')
        assert r.status_code == 401


# ─────────────────────────────────────────────────────────────────────────────
# Unified merge logic (pure Python — no HTTP, no Flask)
# Tests the data-merge logic the frontend performs client-side
# ─────────────────────────────────────────────────────────────────────────────

class TestUnifiedClusterMerge:
    """
    The frontend merges two job lists into one unified list.
    These tests verify the merge/sort/filter logic in pure Python,
    mirroring what the React component does.
    """

    def _merge(self, ai_jobs, agent_jobs):
        """
        Replicate the frontend merge:
          - tag each job with its installer source
          - combine and sort by created descending
        """
        tagged = (
            [{**j, '_source': 'assisted'} for j in ai_jobs] +
            [{**j, '_source': 'agent'}    for j in agent_jobs]
        )
        return sorted(tagged, key=lambda j: j.get('created') or 0, reverse=True)

    def test_merge_combines_both_lists(self):
        merged = self._merge([_ai_job()], [_agent_job()])
        assert len(merged) == 2

    def test_merge_sorted_newest_first(self):
        older = _ai_job({'created': 1_000_000.0})
        newer = _agent_job({'created': 2_000_000.0})
        merged = self._merge([older], [newer])
        assert merged[0]['_source'] == 'agent', "Newer job should be first"

    def test_merge_empty_ai(self):
        merged = self._merge([], [_agent_job()])
        assert len(merged) == 1
        assert merged[0]['_source'] == 'agent'

    def test_merge_empty_agent(self):
        merged = self._merge([_ai_job()], [])
        assert len(merged) == 1
        assert merged[0]['_source'] == 'assisted'

    def test_merge_both_empty(self):
        assert self._merge([], []) == []

    def test_source_tag_preserved(self):
        merged = self._merge([_ai_job()], [_agent_job()])
        sources = {j['_source'] for j in merged}
        assert sources == {'assisted', 'agent'}

    def test_filter_by_status_running(self):
        jobs = self._merge(
            [_ai_job({'status': 'complete'}), _ai_job({'id': 'x', 'status': 'running'})],
            [_agent_job({'status': 'running'})],
        )
        running = [j for j in jobs if j['status'] == 'running']
        assert len(running) == 2

    def test_stat_counts(self):
        jobs = self._merge(
            [_ai_job({'status': 'complete'}), _ai_job({'id': 'x2', 'status': 'failed'})],
            [_agent_job({'status': 'running'}), _agent_job({'id': 'y2', 'status': 'complete'})],
        )
        assert sum(1 for j in jobs if j['status'] == 'complete') == 2
        assert sum(1 for j in jobs if j['status'] == 'running')  == 1
        assert sum(1 for j in jobs if j['status'] == 'failed')   == 1

    def test_detail_route_per_source(self):
        """Each merged job must resolve to the correct detail URL."""
        def detail_url(job):
            if job['_source'] == 'assisted':
                return f"/openshift/jobs/{job['id']}"
            return f"/ocp-agent/jobs/{job['id']}"

        merged = self._merge([_ai_job()], [_agent_job()])
        urls = [detail_url(j) for j in merged]
        assert any('/openshift/jobs/' in u for u in urls)
        assert any('/ocp-agent/jobs/'  in u for u in urls)

    def test_cluster_name_accessible_from_config(self):
        merged = self._merge([_ai_job()], [_agent_job()])
        for j in merged:
            name = j.get('config', {}).get('cluster_name')
            assert name, f"cluster_name missing or empty in job {j['id']}"

    def test_deployment_type_accessible(self):
        merged = self._merge([_ai_job()], [_agent_job()])
        for j in merged:
            dtype = j.get('config', {}).get('deployment_type')
            assert dtype in ('sno', 'compact', 'full', 'multi'), \
                f"Unexpected deployment_type: {dtype}"
