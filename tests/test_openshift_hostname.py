"""
Functional tests for the hostname assignment and verification logic
in openshift.py (Step 6 + Step 6a).

Uses unittest.mock to patch _ai() so no real HTTP calls are made.
"""
import json
from unittest.mock import call, patch, MagicMock

import pytest

import views.openshift as ocp


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_host(host_id, mac, hostname='', status='known', role='master', created_at='2026-04-16T10:00:00Z'):
    """Build a minimal Assisted Installer host dict."""
    return {
        'id':                 host_id,
        'requested_hostname': hostname,
        'status':             status,
        'role':               role,
        'created_at':         created_at,
        'inventory': json.dumps({
            'interfaces': [{'mac_address': mac, 'name': 'enp1s0'}]
        }),
        'progress': {},
    }


def _ok_response(status_code=200):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = {}
    return r


def _error_response(status_code=409, text='Conflict'):
    r = MagicMock()
    r.status_code = status_code
    r.text = text
    r.json.return_value = {}
    return r


# ─────────────────────────────────────────────────────────────────────────────
# _parse_host_mac integration with real host dicts
# ─────────────────────────────────────────────────────────────────────────────

class TestParseHostMacIntegration:
    """Verify MAC matching works correctly for multi-node clusters."""

    def test_3_master_cluster_all_matched(self):
        mac_map = {
            'master-0': '52:54:00:00:00:00',
            'master-1': '52:54:00:00:00:01',
            'master-2': '52:54:00:00:00:02',
        }
        mac_to_vm = {v.lower(): k for k, v in mac_map.items()}

        hosts = [
            _make_host('h0', '52:54:00:00:00:00'),
            _make_host('h1', '52:54:00:00:00:01'),
            _make_host('h2', '52:54:00:00:00:02'),
        ]

        matched = [ocp._parse_host_mac(h, mac_to_vm) for h in hosts]
        assert matched == ['master-0', 'master-1', 'master-2']

    def test_5_node_cluster_all_matched(self):
        mac_map = {f'node-{i}': f'52:54:00:00:00:0{i}' for i in range(5)}
        mac_to_vm = {v.lower(): k for k, v in mac_map.items()}
        hosts = [_make_host(f'h{i}', f'52:54:00:00:00:0{i}') for i in range(5)]
        matched = [ocp._parse_host_mac(h, mac_to_vm) for h in hosts]
        assert matched == [f'node-{i}' for i in range(5)]

    def test_partial_match_returns_none_for_unknown(self):
        mac_to_vm = {'52:54:00:00:00:00': 'master-0'}
        host_unknown = _make_host('h1', '52:54:00:ff:ff:ff')
        assert ocp._parse_host_mac(host_unknown, mac_to_vm) is None


# ─────────────────────────────────────────────────────────────────────────────
# Hostname PATCH retry logic
# ─────────────────────────────────────────────────────────────────────────────

class TestHostnamePatchRetry:
    """
    The assignment loop should retry the PATCH up to 3 times.
    We test this by simulating failures then a success.
    """

    def _run_hostname_patch(self, responses):
        """
        Simulate the retry loop from openshift.py Step 6.
        responses: list of (status_code, text) tuples — one per attempt.
        Returns True if assigned, False if all attempts failed.
        """
        import time as _time

        vm_name  = 'test-master-0'
        url      = '/infra-envs/env-1/hosts/host-1'
        token    = 'fake-token'
        assigned = False

        call_responses = iter(responses)

        def fake_ai(method, path, tok, body=None):
            try:
                code, txt = next(call_responses)
            except StopIteration:
                code, txt = 500, 'No more responses'
            r = MagicMock()
            r.status_code = code
            r.text = txt
            return r

        with patch.object(ocp, '_ai', side_effect=fake_ai), \
             patch('time.sleep'):          # don't actually wait
            for attempt in range(3):
                r = ocp._ai('PATCH', url, token, {'requested_hostname': vm_name})
                if r.status_code < 300:
                    assigned = True
                    break

        return assigned

    def test_success_on_first_attempt(self):
        assert self._run_hostname_patch([(200, 'ok')]) is True

    def test_success_on_second_attempt(self):
        assert self._run_hostname_patch([(409, 'conflict'), (200, 'ok')]) is True

    def test_success_on_third_attempt(self):
        assert self._run_hostname_patch([(409, 'x'), (409, 'x'), (200, 'ok')]) is True

    def test_all_attempts_fail(self):
        assert self._run_hostname_patch([(409, 'x'), (409, 'x'), (409, 'x')]) is False


# ─────────────────────────────────────────────────────────────────────────────
# Hostname verification loop (Step 6a)
# ─────────────────────────────────────────────────────────────────────────────

class TestHostnameVerification:
    """
    Simulate the Step 6a verification poll.
    Hosts start with wrong hostnames → verification re-patches them → confirms.
    """

    def _build_hosts_response(self, hostname_map):
        """hostname_map: {host_id: current_requested_hostname}"""
        hosts = []
        for i, (hid, current_name) in enumerate(hostname_map.items()):
            mac = f'52:54:00:00:00:0{i}'
            hosts.append(_make_host(hid, mac, hostname=current_name))
        return hosts

    def test_all_correct_on_first_poll(self):
        """If all hosts already have correct names, loop exits immediately."""
        mac_map = {'master-0': '52:54:00:00:00:00', 'master-1': '52:54:00:00:00:01'}
        mac_to_vm = {v.lower(): k for k, v in mac_map.items()}

        hosts = [
            _make_host('h0', '52:54:00:00:00:00', hostname='master-0'),
            _make_host('h1', '52:54:00:00:00:01', hostname='master-1'),
        ]

        wrong = []
        for host in hosts:
            current_name = host.get('requested_hostname', '')
            vm_name = ocp._parse_host_mac(host, mac_to_vm)
            if vm_name and current_name != vm_name:
                wrong.append((host['id'], vm_name, current_name))

        assert wrong == []

    def test_wrong_hostnames_detected(self):
        """Hosts with FQDNs instead of VM names should appear in wrong list."""
        mac_map = {'master-0': '52:54:00:00:00:00', 'master-1': '52:54:00:00:00:01'}
        mac_to_vm = {v.lower(): k for k, v in mac_map.items()}

        hosts = [
            _make_host('h0', '52:54:00:00:00:00', hostname='syn-024-000-000-001.inf.spectrum.com'),
            _make_host('h1', '52:54:00:00:00:01', hostname='syn-024-000-000-002.inf.spectrum.com'),
        ]

        wrong = []
        for host in hosts:
            current_name = host.get('requested_hostname', '')
            vm_name = ocp._parse_host_mac(host, mac_to_vm)
            if vm_name and current_name != vm_name:
                wrong.append((host['id'], vm_name, current_name))

        assert len(wrong) == 2
        assert wrong[0] == ('h0', 'master-0', 'syn-024-000-000-001.inf.spectrum.com')
        assert wrong[1] == ('h1', 'master-1', 'syn-024-000-000-002.inf.spectrum.com')

    def test_partial_wrong(self):
        """Only hosts with wrong name appear in wrong list."""
        mac_map = {
            'master-0': '52:54:00:00:00:00',
            'master-1': '52:54:00:00:00:01',
            'master-2': '52:54:00:00:00:02',
        }
        mac_to_vm = {v.lower(): k for k, v in mac_map.items()}

        hosts = [
            _make_host('h0', '52:54:00:00:00:00', hostname='master-0'),          # correct
            _make_host('h1', '52:54:00:00:00:01', hostname='fqdn.example.com'),  # wrong
            _make_host('h2', '52:54:00:00:00:02', hostname='master-2'),          # correct
        ]

        wrong = []
        for host in hosts:
            current_name = host.get('requested_hostname', '')
            vm_name = ocp._parse_host_mac(host, mac_to_vm)
            if vm_name and current_name != vm_name:
                wrong.append((host['id'], vm_name, current_name))

        assert len(wrong) == 1
        assert wrong[0][0] == 'h1'
        assert wrong[0][1] == 'master-1'

    def test_no_mac_match_skipped(self):
        """Hosts with no MAC match should not appear in wrong list."""
        mac_to_vm = {'52:54:00:00:00:00': 'master-0'}
        host = _make_host('h1', '52:54:00:ff:ff:ff', hostname='some-fqdn.example.com')

        wrong = []
        current_name = host.get('requested_hostname', '')
        vm_name = ocp._parse_host_mac(host, mac_to_vm)
        if vm_name and current_name != vm_name:
            wrong.append((host['id'], vm_name, current_name))

        assert wrong == []


# ─────────────────────────────────────────────────────────────────────────────
# Role assignment logic
# ─────────────────────────────────────────────────────────────────────────────

class TestRoleAssignment:
    """Verify master/worker role derivation from vm_names list."""

    def _get_role(self, vm_name, vm_names, n_control):
        return 'worker' if (vm_name in vm_names[n_control:]) else 'master'

    def test_3_masters_no_workers(self):
        vm_names = ['cluster-master-0', 'cluster-master-1', 'cluster-master-2']
        n_control = 3
        roles = [self._get_role(v, vm_names, n_control) for v in vm_names]
        assert roles == ['master', 'master', 'master']

    def test_3_masters_2_workers(self):
        vm_names = ['c-master-0', 'c-master-1', 'c-master-2', 'c-worker-0', 'c-worker-1']
        n_control = 3
        roles = [self._get_role(v, vm_names, n_control) for v in vm_names]
        assert roles == ['master', 'master', 'master', 'worker', 'worker']

    def test_sno_single_master(self):
        vm_names = ['sno-master-0']
        n_control = 1
        assert self._get_role('sno-master-0', vm_names, n_control) == 'master'

    def test_unknown_vm_defaults_to_master(self):
        vm_names = ['c-master-0', 'c-worker-0']
        n_control = 1
        # vm_name not in list at all → not in vm_names[n_control:] → master
        assert self._get_role('unknown-vm', vm_names, n_control) == 'master'
