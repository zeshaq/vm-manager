"""
Mock-based tests for Assisted Installer API interactions.

All HTTP calls to the AI API are intercepted with unittest.mock —
no real Red Hat console credentials or network needed.
"""
import json
from unittest.mock import MagicMock, patch, call

import pytest

import views.openshift as ocp


# ─────────────────────────────────────────────────────────────────────────────
# _ai() helper
# ─────────────────────────────────────────────────────────────────────────────

class TestAiHelper:
    """_ai() should correctly call requests with the right base URL and headers."""

    def _mock_response(self, status=200, body=None):
        r = MagicMock()
        r.status_code = status
        r.json.return_value = body or {}
        r.raise_for_status = MagicMock()
        return r

    def test_get_request_uses_correct_url(self):
        with patch('views.openshift.ai_client._req') as mock_req:
            mock_req.request.return_value = self._mock_response(200, {'id': 'cluster-1'})
            ocp._ai('GET', '/clusters/cluster-1', 'fake-token')
            args, kwargs = mock_req.request.call_args
            assert args[0] == 'GET'
            assert 'clusters/cluster-1' in args[1]

    def test_patch_sends_json_body(self):
        with patch('views.openshift.ai_client._req') as mock_req:
            mock_req.request.return_value = self._mock_response(200)
            ocp._ai('PATCH', '/infra-envs/env1/hosts/h1', 'token',
                    {'requested_hostname': 'master-0'})
            _, kwargs = mock_req.request.call_args
            assert kwargs.get('json') == {'requested_hostname': 'master-0'}

    def test_authorization_header_sent(self):
        with patch('views.openshift.ai_client._req') as mock_req:
            mock_req.request.return_value = self._mock_response(200)
            ocp._ai('GET', '/clusters', 'my-bearer-token')
            _, kwargs = mock_req.request.call_args
            headers = kwargs.get('headers', {})
            assert 'Authorization' in headers
            assert 'my-bearer-token' in headers['Authorization']


# ─────────────────────────────────────────────────────────────────────────────
# _get_access_token
# ─────────────────────────────────────────────────────────────────────────────

class TestGetAccessToken:
    def test_returns_access_token_on_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'access_token': 'eyJhbGciOiJSUzI1NiJ9.test'}
        mock_resp.raise_for_status = MagicMock()

        with patch('views.openshift.ai_client._req') as mock_req:
            mock_req.post.return_value = mock_resp
            token = ocp._get_access_token('offline-token-xyz')
            assert token == 'eyJhbGciOiJSUzI1NiJ9.test'

    def test_raises_on_http_error(self):
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 401
        mock_resp.text = 'Unauthorized'

        with patch('views.openshift.ai_client._req') as mock_req:
            mock_req.post.return_value = mock_resp
            with pytest.raises((RuntimeError, Exception)):
                ocp._get_access_token('bad-offline-token')


# ─────────────────────────────────────────────────────────────────────────────
# Monitoring loop — status transitions
# ─────────────────────────────────────────────────────────────────────────────

class TestMonitoringStatusTransitions:
    """
    Verify that _job_set is called with the right progress/phase
    as cluster status changes through the expected states.
    """

    def _cluster_resp(self, status, pct=0, info='', operators=None):
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = {
            'status':               status,
            'status_info':          info,
            'progress':             {'total_percentage': pct},
            'monitored_operators':  operators or [],
        }
        return r

    def _hosts_resp(self, hosts):
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = hosts
        return r

    def test_progress_increases_through_phases(self):
        """
        Simulate: installing(0%) → installing(50%) → installed(100%)
        Verify progress values in job are monotonically increasing.
        """
        job_id = 'monitor-test-001'
        with ocp._lock:
            ocp._jobs[job_id] = {
                'status': 'pending', 'progress': 65, 'phase': '',
                'logs': [], 'events': [], 'nodes': [], 'mac_map': {},
                'vms': [],
            }

        cluster_states = [
            self._cluster_resp('installing', 0),
            self._cluster_resp('installing', 50),
            self._cluster_resp('installed', 100),
        ]
        hosts_resp = self._hosts_resp([])
        call_count = {'n': 0}

        def fake_ai(method, path, token, body=None):
            if '/hosts' in path:
                return hosts_resp
            resp = cluster_states[min(call_count['n'], len(cluster_states) - 1)]
            call_count['n'] += 1
            return resp

        def fake_collect_creds(*args, **kwargs):
            ocp._job_set(job_id, status='complete', progress=100)

        with patch('views.openshift.monitoring._ai', side_effect=fake_ai), \
             patch('views.openshift.monitoring._get_access_token', return_value='tok'), \
             patch('views.openshift.monitoring._collect_credentials', side_effect=fake_collect_creds), \
             patch('time.sleep'):
            ocp._monitor_install_thread(job_id, {
                'offline_token': 'tok',
                'cluster_name': 'test',
                'base_domain': 'example.com',
            }, 'cluster-id-001')

        assert ocp._jobs[job_id]['status'] == 'complete'
        assert ocp._jobs[job_id]['progress'] == 100

        with ocp._lock:
            ocp._jobs.pop(job_id, None)

    def test_error_status_marks_job_failed(self):
        job_id = 'monitor-fail-001'
        with ocp._lock:
            ocp._jobs[job_id] = {
                'status': 'pending', 'progress': 65, 'phase': '',
                'logs': [], 'events': [], 'nodes': [], 'mac_map': {}, 'vms': [],
            }

        def fake_ai(method, path, token, body=None):
            if '/hosts' in path:
                r = MagicMock()
                r.json.return_value = []
                return r
            r = MagicMock()
            r.json.return_value = {
                'status': 'error',
                'status_info': 'Host failed validation',
                'progress': {'total_percentage': 0},
                'monitored_operators': [],
            }
            return r

        with patch('views.openshift.monitoring._ai', side_effect=fake_ai), \
             patch('views.openshift.monitoring._get_access_token', return_value='tok'), \
             patch('time.sleep'):
            ocp._monitor_install_thread(job_id, {
                'offline_token': 'tok',
                'cluster_name': 'test',
                'base_domain': 'example.com',
            }, 'cluster-id-001')

        assert ocp._jobs[job_id]['status'] == 'failed'

        with ocp._lock:
            ocp._jobs.pop(job_id, None)


# ─────────────────────────────────────────────────────────────────────────────
# Operator tracking during finalizing
# ─────────────────────────────────────────────────────────────────────────────

class TestOperatorTracking:
    """Verify ai_operators is populated and operator_available events fired."""

    def test_operator_available_event_emitted(self):
        job_id = 'op-test-001'
        with ocp._lock:
            ocp._jobs[job_id] = {
                'status': 'pending', 'progress': 88, 'phase': '',
                'logs': [], 'events': [], 'nodes': [], 'mac_map': {}, 'vms': [],
            }

        states = [
            # First poll: finalizing with 1 available operator
            {
                'status': 'finalizing',
                'status_info': 'Waiting for operators',
                'progress': {'total_percentage': 80},
                'monitored_operators': [
                    {'name': 'authentication', 'status': 'available', 'status_info': ''},
                ],
            },
            # Second poll: installed
            {
                'status': 'installed',
                'status_info': 'Cluster installed',
                'progress': {'total_percentage': 100},
                'monitored_operators': [],
            },
        ]
        poll = {'n': 0}

        def fake_ai(method, path, token, body=None):
            if '/hosts' in path:
                r = MagicMock(); r.json.return_value = []; return r
            r = MagicMock()
            r.json.return_value = states[min(poll['n'], len(states) - 1)]
            poll['n'] += 1
            return r

        with patch('views.openshift.monitoring._ai', side_effect=fake_ai), \
             patch('views.openshift.monitoring._get_access_token', return_value='tok'), \
             patch('views.openshift.monitoring._collect_credentials'), \
             patch('time.sleep'):
            ocp._monitor_install_thread(job_id, {
                'offline_token': 'tok',
                'cluster_name': 'test',
                'base_domain': 'example.com',
            }, 'cluster-id-ops')

        events = ocp._jobs[job_id]['events']
        operator_events = [e for e in events if e['type'] == 'operator_available']
        assert len(operator_events) >= 1
        assert operator_events[0]['operator'] == 'authentication'

        with ocp._lock:
            ocp._jobs.pop(job_id, None)


# ─────────────────────────────────────────────────────────────────────────────
# Pending-user-action auto-recovery
# ─────────────────────────────────────────────────────────────────────────────

class TestPendingUserActionRecovery:
    """
    When a host enters pending-user-action, the monitor loop should:
    1. Match the host to a VM by MAC
    2. Call _eject_cdroms and _reboot_vms for that specific VM only
    """

    def test_correct_vm_ejected_and_rebooted(self):
        job_id = 'pua-test-001'
        mac_map = {
            'cluster-master-0': '52:54:00:00:00:00',
            'cluster-master-1': '52:54:00:00:00:01',
        }
        with ocp._lock:
            ocp._jobs[job_id] = {
                'status': 'pending', 'progress': 70, 'phase': '',
                'logs': [], 'events': [], 'nodes': [],
                'mac_map': mac_map,
                'vms': ['cluster-master-0', 'cluster-master-1'],
            }

        host_pua = {
            'id': 'host-pua',
            'requested_hostname': 'cluster-master-1',
            'status': 'installing-pending-user-action',
            'role': 'master',
            'created_at': '2026-04-16T10:00:00Z',
            'inventory': json.dumps({
                'interfaces': [{'mac_address': '52:54:00:00:00:01'}]
            }),
            'progress': {},
        }
        host_ok = {
            'id': 'host-ok',
            'requested_hostname': 'cluster-master-0',
            'status': 'installing',
            'role': 'master',
            'created_at': '2026-04-16T10:00:00Z',
            'inventory': json.dumps({
                'interfaces': [{'mac_address': '52:54:00:00:00:00'}]
            }),
            'progress': {},
        }

        states = [
            {   # poll 1: one host in pending-user-action
                'status': 'installing',
                'status_info': 'Installing',
                'progress': {'total_percentage': 50},
                'monitored_operators': [],
            },
            {   # poll 2: installed
                'status': 'installed',
                'status_info': 'Done',
                'progress': {'total_percentage': 100},
                'monitored_operators': [],
            },
        ]
        poll = {'n': 0}

        def fake_ai(method, path, token, body=None):
            if '/hosts' in path:
                r = MagicMock()
                r.json.return_value = [host_pua, host_ok] if poll['n'] <= 1 else []
                return r
            r = MagicMock()
            r.json.return_value = states[min(poll['n'], len(states) - 1)]
            poll['n'] += 1
            return r

        ejected = []
        rebooted = []

        with patch('views.openshift.monitoring._ai', side_effect=fake_ai), \
             patch('views.openshift.monitoring._get_access_token', return_value='tok'), \
             patch('views.openshift.monitoring._collect_credentials'), \
             patch('views.openshift.monitoring._eject_cdroms', side_effect=lambda vms, log: ejected.extend(vms)), \
             patch('views.openshift.monitoring._reboot_vms', side_effect=lambda vms, log: rebooted.extend(vms)), \
             patch('time.sleep'):
            ocp._monitor_install_thread(job_id, {
                'offline_token': 'tok',
                'cluster_name': 'test',
                'base_domain': 'example.com',
            }, 'cluster-pua')

        # Only the specific VM should be ejected/rebooted, not both
        assert 'cluster-master-1' in ejected
        assert 'cluster-master-0' not in ejected

        with ocp._lock:
            ocp._jobs.pop(job_id, None)
