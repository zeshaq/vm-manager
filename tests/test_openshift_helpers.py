"""
Unit tests for openshift.py helper functions.

Covers:
  - _job_event()        structured event appending + capping at 300
  - _job_set()          job field updates
  - _job_log()          log appending
  - _parse_host_mac()   MAC → VM name lookup
  - _make_mac()         deterministic MAC generation
  - _iso_fingerprint()  cache key stability
"""
import json
import threading
import time
from unittest.mock import patch, MagicMock

import pytest

# ── import the module under test ──────────────────────────────────────────────
import views.openshift as ocp


# ─────────────────────────────────────────────────────────────────────────────
# _job_event
# ─────────────────────────────────────────────────────────────────────────────

class TestJobEvent:
    def setup_method(self):
        """Inject a fresh in-memory job before each test."""
        self.job_id = 'evt-test-001'
        with ocp._lock:
            ocp._jobs[self.job_id] = {
                'logs': [], 'events': [], 'status': 'pending'
            }

    def teardown_method(self):
        with ocp._lock:
            ocp._jobs.pop(self.job_id, None)

    def test_event_appended(self):
        ocp._job_event(self.job_id, 'status_change', status='installing')
        events = ocp._jobs[self.job_id]['events']
        assert len(events) == 1
        assert events[0]['type'] == 'status_change'
        assert events[0]['status'] == 'installing'

    def test_event_has_timestamp(self):
        ocp._job_event(self.job_id, 'stage_change', node='master-0', to_stage='Installing')
        ev = ocp._jobs[self.job_id]['events'][0]
        assert 'ts' in ev
        # HH:MM:SS format
        assert len(ev['ts']) == 8

    def test_events_capped_at_300(self):
        for i in range(350):
            ocp._job_event(self.job_id, 'test', i=i)
        events = ocp._jobs[self.job_id]['events']
        assert len(events) == 300
        # Should keep the newest 300
        assert events[-1]['i'] == 349
        assert events[0]['i'] == 50

    def test_unknown_job_is_noop(self):
        # Should not raise
        ocp._job_event('nonexistent-job', 'status_change', status='x')

    def test_multiple_event_types(self):
        ocp._job_event(self.job_id, 'status_change', status='installing')
        ocp._job_event(self.job_id, 'stage_change', node='n1', to_stage='Writing image to disk')
        ocp._job_event(self.job_id, 'stuck', node='n1', minutes=16)
        events = ocp._jobs[self.job_id]['events']
        assert [e['type'] for e in events] == ['status_change', 'stage_change', 'stuck']


# ─────────────────────────────────────────────────────────────────────────────
# _job_set
# ─────────────────────────────────────────────────────────────────────────────

class TestJobSet:
    def setup_method(self):
        self.job_id = 'set-test-001'
        with ocp._lock:
            ocp._jobs[self.job_id] = {'status': 'pending', 'progress': 0, 'logs': [], 'events': []}

    def teardown_method(self):
        with ocp._lock:
            ocp._jobs.pop(self.job_id, None)

    def test_updates_fields(self):
        ocp._job_set(self.job_id, status='complete', progress=100)
        job = ocp._jobs[self.job_id]
        assert job['status'] == 'complete'
        assert job['progress'] == 100

    def test_partial_update_preserves_other_fields(self):
        ocp._job_set(self.job_id, progress=50)
        assert ocp._jobs[self.job_id]['status'] == 'pending'

    def test_unknown_job_is_noop(self):
        ocp._job_set('no-such-job', status='x')  # should not raise

    def test_sets_nested_value(self):
        ocp._job_set(self.job_id, nodes=[{'id': 'h1', 'stage': 'Installing'}])
        assert ocp._jobs[self.job_id]['nodes'][0]['id'] == 'h1'


# ─────────────────────────────────────────────────────────────────────────────
# _job_log
# ─────────────────────────────────────────────────────────────────────────────

class TestJobLog:
    def setup_method(self):
        self.job_id = 'log-test-001'
        with ocp._lock:
            ocp._jobs[self.job_id] = {'logs': [], 'events': []}

    def teardown_method(self):
        with ocp._lock:
            ocp._jobs.pop(self.job_id, None)

    def test_log_appended(self):
        ocp._job_log(self.job_id, 'Hello world')
        logs = ocp._jobs[self.job_id]['logs']
        assert len(logs) == 1
        assert logs[0]['msg'] == 'Hello world'
        assert logs[0]['level'] == 'info'

    def test_log_warn_level(self):
        ocp._job_log(self.job_id, 'Something wrong', 'warn')
        assert ocp._jobs[self.job_id]['logs'][0]['level'] == 'warn'

    def test_log_has_timestamp(self):
        ocp._job_log(self.job_id, 'ts test')
        assert 'ts' in ocp._jobs[self.job_id]['logs'][0]


# ─────────────────────────────────────────────────────────────────────────────
# _parse_host_mac
# ─────────────────────────────────────────────────────────────────────────────

class TestParseHostMac:
    def _host(self, macs, extra=None):
        """Build a minimal host dict with given MAC addresses."""
        nics = [{'mac_address': m, 'name': f'enp{i}s0'} for i, m in enumerate(macs)]
        h = {'id': 'h1', 'inventory': json.dumps({'interfaces': nics})}
        if extra:
            h.update(extra)
        return h

    def test_single_mac_match(self):
        mac_to_vm = {'52:54:00:aa:bb:cc': 'master-0'}
        host = self._host(['52:54:00:aa:bb:cc'])
        assert ocp._parse_host_mac(host, mac_to_vm) == 'master-0'

    def test_case_insensitive_match(self):
        mac_to_vm = {'52:54:00:aa:bb:cc': 'master-1'}
        host = self._host(['52:54:00:AA:BB:CC'])
        assert ocp._parse_host_mac(host, mac_to_vm) == 'master-1'

    def test_first_matching_nic_wins(self):
        mac_to_vm = {'52:54:00:00:00:01': 'worker-0', '52:54:00:00:00:02': 'worker-1'}
        host = self._host(['52:54:00:00:00:01', '52:54:00:00:00:02'])
        assert ocp._parse_host_mac(host, mac_to_vm) == 'worker-0'

    def test_no_match_returns_none(self):
        mac_to_vm = {'52:54:00:aa:bb:cc': 'master-0'}
        host = self._host(['52:54:00:ff:ff:ff'])
        assert ocp._parse_host_mac(host, mac_to_vm) is None

    def test_empty_inventory(self):
        mac_to_vm = {'52:54:00:aa:bb:cc': 'master-0'}
        host = {'id': 'h1', 'inventory': '{}'}
        assert ocp._parse_host_mac(host, mac_to_vm) is None

    def test_malformed_inventory_json(self):
        mac_to_vm = {'52:54:00:aa:bb:cc': 'master-0'}
        host = {'id': 'h1', 'inventory': 'NOT JSON'}
        assert ocp._parse_host_mac(host, mac_to_vm) is None

    def test_inventory_as_dict(self):
        """inventory field can also be a pre-parsed dict (not a JSON string)."""
        mac_to_vm = {'52:54:00:aa:bb:cc': 'master-2'}
        host = {'id': 'h1', 'inventory': {'interfaces': [{'mac_address': '52:54:00:aa:bb:cc'}]}}
        assert ocp._parse_host_mac(host, mac_to_vm) == 'master-2'

    def test_alternate_key_macaddress(self):
        """Some API versions use 'macAddress' (camelCase)."""
        mac_to_vm = {'52:54:00:aa:bb:cc': 'master-0'}
        host = {'id': 'h1', 'inventory': json.dumps({
            'interfaces': [{'macAddress': '52:54:00:aa:bb:cc'}]
        })}
        assert ocp._parse_host_mac(host, mac_to_vm) == 'master-0'

    def test_nics_key_fallback(self):
        """Older API uses 'nics' instead of 'interfaces'."""
        mac_to_vm = {'52:54:00:aa:bb:cc': 'worker-0'}
        host = {'id': 'h1', 'inventory': json.dumps({
            'nics': [{'mac_address': '52:54:00:aa:bb:cc'}]
        })}
        assert ocp._parse_host_mac(host, mac_to_vm) == 'worker-0'

    def test_empty_mac_to_vm(self):
        host = {'id': 'h1', 'inventory': json.dumps({
            'interfaces': [{'mac_address': '52:54:00:aa:bb:cc'}]
        })}
        assert ocp._parse_host_mac(host, {}) is None


# ─────────────────────────────────────────────────────────────────────────────
# _make_mac
# ─────────────────────────────────────────────────────────────────────────────

class TestMakeMac:
    def test_format(self):
        mac = ocp._make_mac('job-abc', 0)
        parts = mac.split(':')
        assert len(parts) == 6
        for p in parts:
            assert len(p) == 2
            int(p, 16)  # must be valid hex

    def test_locally_administered_bit(self):
        """First byte must have locally administered bit set (bit 1 of byte 0)."""
        mac = ocp._make_mac('job-abc', 0)
        first_byte = int(mac.split(':')[0], 16)
        assert first_byte & 0x02, "Locally administered bit should be set"

    def test_unicast_bit(self):
        """First byte must NOT have multicast bit set (bit 0 of byte 0)."""
        mac = ocp._make_mac('job-abc', 0)
        first_byte = int(mac.split(':')[0], 16)
        assert not (first_byte & 0x01), "Multicast bit should not be set"

    def test_deterministic(self):
        """Same job_id + index always produces the same MAC."""
        assert ocp._make_mac('job-xyz', 2) == ocp._make_mac('job-xyz', 2)

    def test_different_index_gives_different_mac(self):
        assert ocp._make_mac('job-xyz', 0) != ocp._make_mac('job-xyz', 1)

    def test_different_job_gives_different_mac(self):
        assert ocp._make_mac('job-aaa', 0) != ocp._make_mac('job-bbb', 0)

    def test_five_node_cluster_all_unique(self):
        macs = [ocp._make_mac('cluster-test', i) for i in range(5)]
        assert len(set(macs)) == 5


# ─────────────────────────────────────────────────────────────────────────────
# _iso_fingerprint
# ─────────────────────────────────────────────────────────────────────────────

class TestIsoFingerprint:
    def test_same_inputs_same_output(self):
        fp1 = ocp._iso_fingerprint('4.14', 'pull-secret', 'ssh-key')
        fp2 = ocp._iso_fingerprint('4.14', 'pull-secret', 'ssh-key')
        assert fp1 == fp2

    def test_different_version_different_output(self):
        fp1 = ocp._iso_fingerprint('4.14', 'pull-secret', 'ssh-key')
        fp2 = ocp._iso_fingerprint('4.15', 'pull-secret', 'ssh-key')
        assert fp1 != fp2

    def test_returns_16_chars(self):
        fp = ocp._iso_fingerprint('4.14', 'pull-secret', 'ssh-key')
        assert len(fp) == 16

    def test_whitespace_stripped(self):
        """Leading/trailing whitespace in pull_secret or ssh_key must not affect fingerprint."""
        fp1 = ocp._iso_fingerprint('4.14', '  pull-secret  ', '  ssh-key  ')
        fp2 = ocp._iso_fingerprint('4.14', 'pull-secret', 'ssh-key')
        assert fp1 == fp2


# ─────────────────────────────────────────────────────────────────────────────
# Thread safety — concurrent _job_event calls
# ─────────────────────────────────────────────────────────────────────────────

class TestThreadSafety:
    def test_concurrent_events_no_data_loss(self):
        job_id = 'thread-test-001'
        with ocp._lock:
            ocp._jobs[job_id] = {'logs': [], 'events': []}

        def writer(n):
            for i in range(20):
                ocp._job_event(job_id, 'test', thread=n, i=i)

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        events = ocp._jobs[job_id]['events']
        # 5 threads × 20 events = 100, all within cap
        assert len(events) == 100

        with ocp._lock:
            ocp._jobs.pop(job_id, None)
