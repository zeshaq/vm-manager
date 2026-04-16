"""
OpenShift package — libvirt VM operations (CDROM management, reboot, XML generation).
"""

import hashlib
import uuid
import xml.etree.ElementTree as ET

from .constants import _LIBVIRT

if _LIBVIRT:
    import libvirt

# FEATURE: mac-generation

def _make_mac(job_id: str, node_idx: int) -> str:
    """Deterministic KVM MAC address (52:54:00:XX:XX:XX) for a deployment node.
    Same job+index always produces the same MAC so nmstate static config
    generated at infra-env creation time matches the actual VM NIC.
    """
    h = hashlib.md5(f'{job_id}:{node_idx}'.encode()).hexdigest()
    return f'52:54:00:{h[0:2]}:{h[2:4]}:{h[4:6]}'


# FEATURE: vm-xml-generation

def _vm_xml(name: str, vcpus: int, ram_mb: int, disk_path: str,
            iso_path: str, network: str = 'default',
            host_bridge: bool = False, extra_disks: list = None,
            mac_address: str = None) -> str:
    # libvirt-managed network vs host bridge (e.g. br-real) need different XML
    mac_xml = f"\n      <mac address='{mac_address}'/>" if mac_address else ''
    if host_bridge:
        iface_xml = f"""<interface type='bridge'>
      <source bridge='{network}'/>{mac_xml}
      <model type='virtio'/>
    </interface>"""
    else:
        iface_xml = f"""<interface type='network'>
      <source network='{network}'/>{mac_xml}
      <model type='virtio'/>
    </interface>"""

    # Build extra disk XML (vdb, vdc, …)
    extra_disks_xml = ''
    for idx, ep in enumerate(extra_disks or []):
        dev = 'vd' + chr(ord('b') + idx)   # vdb, vdc, vdd …
        extra_disks_xml += f"""
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2' cache='none'/>
      <source file='{ep}'/>
      <target dev='{dev}' bus='virtio'/>
    </disk>"""

    return f"""
<domain type='kvm'>
  <name>{name}</name>
  <uuid>{uuid.uuid4()}</uuid>
  <memory unit='MiB'>{ram_mb}</memory>
  <vcpu>{vcpus}</vcpu>
  <os>
    <type arch='x86_64' machine='q35'>hvm</type>
  </os>
  <features><acpi/><apic/></features>
  <cpu mode='host-passthrough'/>
  <clock offset='utc'/>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2' cache='none'/>
      <source file='{disk_path}'/>
      <target dev='vda' bus='virtio'/>
      <boot order='1'/>
    </disk>
    <disk type='file' device='cdrom'>
      <driver name='qemu' type='raw'/>
      <source file='{iso_path}'/>
      <target dev='sda' bus='sata'/>
      <readonly/>
      <boot order='2'/>
    </disk>{extra_disks_xml}
    {iface_xml}
    <graphics type='vnc' port='-1' autoport='yes' listen='127.0.0.1'>
      <listen type='address' address='127.0.0.1'/>
    </graphics>
    <video><model type='vga' vram='16384' heads='1'/></video>
    <console type='pty'/>
  </devices>
</domain>"""


# FEATURE: nmstate-yaml

def _build_nmstate_yaml(mac: str, ip: str, prefix_len: int,
                        gateway: str, dns_list: list) -> str:
    """Build nmstate YAML for one node (Assisted Installer static_network_config)."""
    dns_entries = ''.join(f'\n      - {d}' for d in dns_list)
    routes_section = ''
    if gateway:
        routes_section = (
            f'routes:\n'
            f'  config:\n'
            f'    - destination: 0.0.0.0/0\n'
            f'      next-hop-address: {gateway}\n'
            f'      next-hop-interface: eth0\n'
        )
    return (
        f'interfaces:\n'
        f'  - name: eth0\n'
        f'    type: ethernet\n'
        f'    state: up\n'
        f'    mac-address: "{mac}"\n'
        f'    ipv4:\n'
        f'      enabled: true\n'
        f'      dhcp: false\n'
        f'      address:\n'
        f'        - ip: {ip}\n'
        f'          prefix-length: {prefix_len}\n'
        f'dns-resolver:\n'
        f'  config:\n'
        f'    server:{dns_entries}\n'
        + routes_section
    )


# FEATURE: cdrom-eject

def _eject_cdroms(vm_names: list, log_fn):
    """Eject the discovery ISO from all VMs so the next reboot boots from disk.

    Called right after installation is triggered — the ISO is no longer needed
    and leaving it as boot-order-1 causes the Assisted Installer 'pending user
    action: expected to boot from disk' error.
    """
    try:
        conn = libvirt.open('qemu:///system')
    except Exception as e:
        log_fn(f'  Cannot open libvirt for CDROM eject: {e}', 'warn')
        return

    for vm_name in vm_names:
        try:
            dom = conn.lookupByName(vm_name)
            xml_str = dom.XMLDesc(0)
            root    = ET.fromstring(xml_str)
            for disk in root.findall('.//disk'):
                if disk.get('device') != 'cdrom':
                    continue
                target_el = disk.find('target')
                if target_el is None:
                    continue
                dev = target_el.get('dev', 'sda')
                bus = target_el.get('bus', 'sata')
                # Empty CDROM (no <source>) = ejected; also clear boot order
                empty_xml = (
                    f"<disk type='file' device='cdrom'>"
                    f"<driver name='qemu' type='raw'/>"
                    f"<target dev='{dev}' bus='{bus}'/>"
                    f"<readonly/>"
                    f"</disk>"
                )
                try:
                    dom.updateDeviceFlags(
                        empty_xml,
                        libvirt.VIR_DOMAIN_AFFECT_LIVE |
                        libvirt.VIR_DOMAIN_AFFECT_CONFIG,
                    )
                    log_fn(f'  Ejected ISO from {vm_name} ({dev}) ✓')
                except libvirt.libvirtError as e:
                    log_fn(f'  CDROM eject warning ({vm_name}): {e}', 'warn')
        except libvirt.libvirtError:
            pass  # VM not found — skip

    conn.close()


# FEATURE: cdrom-insert

def _insert_cdroms(vm_names: list, iso_path: str, log_fn):
    """Re-insert the discovery ISO into all VMs (used when retrying after error).

    Also sets the CDROM as boot-order-1 so the next reboot boots from ISO.
    """
    try:
        conn = libvirt.open('qemu:///system')
    except Exception as e:
        log_fn(f'  Cannot open libvirt for CDROM insert: {e}', 'warn')
        return

    for vm_name in vm_names:
        try:
            dom = conn.lookupByName(vm_name)
            xml_str = dom.XMLDesc(0)
            root    = ET.fromstring(xml_str)
            for disk in root.findall('.//disk'):
                if disk.get('device') != 'cdrom':
                    continue
                target_el = disk.find('target')
                if target_el is None:
                    continue
                dev = target_el.get('dev', 'sda')
                bus = target_el.get('bus', 'sata')
                insert_xml = (
                    f"<disk type='file' device='cdrom'>"
                    f"<driver name='qemu' type='raw'/>"
                    f"<source file='{iso_path}'/>"
                    f"<target dev='{dev}' bus='{bus}'/>"
                    f"<readonly/>"
                    f"<boot order='2'/>"
                    f"</disk>"
                )
                try:
                    dom.updateDeviceFlags(
                        insert_xml,
                        libvirt.VIR_DOMAIN_AFFECT_LIVE |
                        libvirt.VIR_DOMAIN_AFFECT_CONFIG,
                    )
                    log_fn(f'  Inserted ISO into {vm_name} ({dev}) ✓')
                except libvirt.libvirtError as e:
                    log_fn(f'  CDROM insert warning ({vm_name}): {e}', 'warn')
        except libvirt.libvirtError:
            pass

    conn.close()


# FEATURE: vm-reboot

def _reboot_vms(vm_names: list, log_fn):
    """Soft-reboot VMs (used to recover from pending-user-action)."""
    try:
        conn = libvirt.open('qemu:///system')
    except Exception:
        return
    for vm_name in vm_names:
        try:
            dom = conn.lookupByName(vm_name)
            if dom.isActive():
                dom.reboot(0)
                log_fn(f'  Rebooted {vm_name} ✓')
        except libvirt.libvirtError as e:
            log_fn(f'  Reboot warning ({vm_name}): {e}', 'warn')
    conn.close()
