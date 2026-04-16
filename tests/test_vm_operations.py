"""
Unit tests for VM-level helper logic.

Tests the pure/stateless functions and mocks libvirt for VM operations.
"""
import re
import xml.etree.ElementTree as ET
from unittest.mock import MagicMock, patch, call

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# VM name validation
# ─────────────────────────────────────────────────────────────────────────────

class TestVMNameValidation:
    """Test the VM_NAME_RE regex used in api.py."""

    VM_NAME_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$')

    def test_valid_names(self):
        valid = [
            'master-0', 'worker-1', 'cluster.node', 'test_vm',
            'MyVM', 'vm123', 'a',
            'a' * 63,  # max length
        ]
        for name in valid:
            assert self.VM_NAME_RE.match(name), f"Expected valid: {name}"

    def test_invalid_names(self):
        invalid = [
            '',           # empty
            '-starts-with-dash',
            '.starts-with-dot',
            '_starts-with-underscore',
            'has space',
            'has/slash',
            'a' * 64,     # too long
            'special!char',
        ]
        for name in invalid:
            assert not self.VM_NAME_RE.match(name), f"Expected invalid: {name}"


# ─────────────────────────────────────────────────────────────────────────────
# Boot order in VM XML
# ─────────────────────────────────────────────────────────────────────────────

class TestBootOrder:
    """Verify VMs are created with disk=1 (boot first), CDROM=2 (fallback)."""

    def _parse_boot_orders(self, xml_str):
        """Return {device_type: order} from VM XML."""
        root = ET.fromstring(xml_str)
        orders = {}
        for disk in root.findall('.//disk'):
            dev_type = disk.get('device')
            boot_el = disk.find('boot')
            if boot_el is not None:
                orders[dev_type] = int(boot_el.get('order', 0))
        return orders

    def test_openshift_vm_xml_boot_order(self):
        """openshift.py VM XML should have disk=1, cdrom=2."""
        import views.openshift as ocp
        import views.openshift.vm_ops as vm_ops_mod

        # Inspect the vm_ops module where _vm_xml is defined
        import inspect
        src = inspect.getsource(vm_ops_mod)

        # Find the XML template fragment
        assert "boot order='1'" in src, "Should have boot order='1'"
        assert "boot order='2'" in src, "Should have boot order='2'"

        # Find the positions to verify disk comes before CDROM in boot order
        # (disk gets order=1, cdrom gets order=2)
        xml_template_start = src.find('<domain type=')
        if xml_template_start > 0:
            xml_section = src[xml_template_start:xml_template_start + 2000]
            disk_pos  = xml_section.find("device='disk'")
            cdrom_pos = xml_section.find("device='cdrom'")
            if disk_pos > 0 and cdrom_pos > 0:
                assert disk_pos < cdrom_pos, \
                    "disk should be defined before cdrom in VM XML (disk boots first)"

    def test_insert_cdroms_uses_boot_order_2(self):
        """_insert_cdroms should set CDROM as boot order 2, not 1."""
        import views.openshift as ocp
        import inspect
        src = inspect.getsource(ocp._insert_cdroms)
        # The insert XML should have boot order='2'
        assert "boot order='2'" in src, \
            "_insert_cdroms should use boot order='2' to keep disk as primary boot"
        assert "boot order='1'" not in src, \
            "_insert_cdroms should NOT set CDROM as boot order='1'"


# ─────────────────────────────────────────────────────────────────────────────
# _eject_cdroms
# ─────────────────────────────────────────────────────────────────────────────

class TestEjectCdroms:
    def test_eject_calls_update_device(self):
        import views.openshift as ocp
        import libvirt as lv

        # Ensure the libvirt stub has the required constants
        lv.VIR_DOMAIN_AFFECT_LIVE   = 1
        lv.VIR_DOMAIN_AFFECT_CONFIG = 2

        mock_domain = MagicMock()
        mock_domain.XMLDesc.return_value = """
        <domain type='kvm'>
          <devices>
            <disk type='file' device='cdrom'>
              <driver name='qemu' type='raw'/>
              <source file='/tmp/discovery.iso'/>
              <target dev='sda' bus='sata'/>
              <boot order='2'/>
            </disk>
          </devices>
        </domain>"""
        mock_domain.updateDeviceFlags.return_value = 0

        mock_conn = MagicMock()
        mock_conn.lookupByName.return_value = mock_domain

        logs = []
        with patch('libvirt.open', return_value=mock_conn):
            ocp._eject_cdroms(['master-0'], lambda msg, lvl='info': logs.append(msg))

        mock_domain.updateDeviceFlags.assert_called_once()
        xml_arg = mock_domain.updateDeviceFlags.call_args[0][0]
        # Ejected CDROM should have no <source> element
        assert '<source' not in xml_arg

    def test_eject_only_targets_specified_vms(self):
        import views.openshift as ocp

        call_log = []
        mock_domain = MagicMock()
        mock_domain.XMLDesc.return_value = """
        <domain type='kvm'>
          <devices>
            <disk type='file' device='cdrom'>
              <source file='/tmp/iso'/>
              <target dev='sda' bus='sata'/>
            </disk>
          </devices>
        </domain>"""

        mock_conn = MagicMock()
        mock_conn.lookupByName.side_effect = lambda name: (
            call_log.append(name) or mock_domain
        )

        with patch('libvirt.open', return_value=mock_conn):
            ocp._eject_cdroms(['master-0'], lambda *a: None)

        # Only master-0 should have been looked up
        assert call_log == ['master-0']


# ─────────────────────────────────────────────────────────────────────────────
# MAC address helpers
# ─────────────────────────────────────────────────────────────────────────────

class TestMacHelpers:
    def test_make_mac_valid_format(self):
        import views.openshift as ocp
        for i in range(10):
            mac = ocp._make_mac('job-test', i)
            assert re.match(r'^([0-9a-f]{2}:){5}[0-9a-f]{2}$', mac), \
                f"Invalid MAC format: {mac}"

    def test_make_mac_no_broadcast(self):
        """Broadcast MAC (ff:ff:ff:ff:ff:ff) should never be generated."""
        import views.openshift as ocp
        for i in range(100):
            mac = ocp._make_mac(f'job-{i}', i)
            assert mac != 'ff:ff:ff:ff:ff:ff'

    def test_make_mac_no_all_zeros(self):
        import views.openshift as ocp
        for i in range(100):
            mac = ocp._make_mac(f'job-{i}', i)
            assert mac != '00:00:00:00:00:00'


# ─────────────────────────────────────────────────────────────────────────────
# VM state string mapping
# ─────────────────────────────────────────────────────────────────────────────

class TestVMStateString:
    def test_known_states(self):
        import libvirt
        from views.listing import get_vm_state_string
        assert get_vm_state_string(libvirt.VIR_DOMAIN_RUNNING)  == 'Running'
        assert get_vm_state_string(libvirt.VIR_DOMAIN_SHUTOFF)  == 'Shutoff'
        assert get_vm_state_string(libvirt.VIR_DOMAIN_PAUSED)   == 'Paused'
        assert get_vm_state_string(libvirt.VIR_DOMAIN_CRASHED)  == 'Crashed'

    def test_unknown_state_returns_unknown(self):
        from views.listing import get_vm_state_string
        assert get_vm_state_string(999) == 'Unknown'
