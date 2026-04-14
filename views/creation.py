import libvirt
from flask import Blueprint
from .listing import get_host_devices, parse_pci_id

# VM creation is handled entirely through POST /api/vms (views/api.py).
# This blueprint is kept so the import in app.py remains valid.
# The helper functions below are used by views/api.py.
creation_bp = Blueprint('creation', __name__)


def generate_vm_xml(name, memory_mb, vcpus, project=None, host_cpu=False, devices=None, disk_path=None):
    """Generate libvirt domain XML for a new VM.

    disk_path: optional path to an OS image that will be attached at definition time.
      - .iso  → attached as a SATA cdrom (read-only)
      - anything else → attached as a virtio disk (qcow2 overlay expected)
    """
    memory_kib = int(memory_mb) * 1024

    cpu_xml = ""
    if host_cpu:
        cpu_xml = "<cpu mode='host-passthrough' check='none'/>"

    meta_xml = ""
    if project:
        meta_xml = f"""
        <metadata>
            <project>{project}</project>
        </metadata>
        """

    pci_xml = ""
    if devices:
        for pci_id in devices:
            bus, slot, function = parse_pci_id(pci_id)
            pci_xml += f"""
            <hostdev mode='subsystem' type='pci' managed='yes'>
              <source>
                <address domain='0x0000' bus='{bus}' slot='{slot}' function='{function}'/>
              </source>
            </hostdev>
            """

    disk_xml = ""
    if disk_path:
        if disk_path.lower().endswith('.iso'):
            disk_xml = f"""
        <!-- OS ISO (cdrom) -->
        <disk type='file' device='cdrom'>
          <driver name='qemu' type='raw'/>
          <source file='{disk_path}'/>
          <target dev='sda' bus='sata'/>
          <readonly/>
        </disk>"""
        else:
            disk_xml = f"""
        <!-- OS Disk -->
        <disk type='file' device='disk'>
          <driver name='qemu' type='qcow2' cache='none'/>
          <source file='{disk_path}'/>
          <target dev='vda' bus='virtio'/>
        </disk>"""

    # Boot order: prefer hard disk → cdrom → network
    boot_xml = """
        <boot dev='hd'/>
        <boot dev='cdrom'/>
        <boot dev='network'/>"""

    return f"""
    <domain type='kvm'>
      <name>{name}</name>
      {meta_xml}
      <memory unit='KiB'>{memory_kib}</memory>
      <vcpu placement='static'>{vcpus}</vcpu>
      {cpu_xml}
      <os>
        <type arch='x86_64' machine='pc-q35-6.2'>hvm</type>
        {boot_xml}
      </os>
      <features><acpi/><apic/></features>
      <devices>
        <emulator>/usr/bin/qemu-system-x86_64</emulator>
        {disk_xml}

        <!-- Network Interface (Default NAT) -->
        <interface type='network'>
          <source network='default'/>
          <model type='virtio'/>
        </interface>

        <!-- VNC console (noVNC / virt-viewer compatible) -->
        <graphics type='vnc' port='-1' autoport='yes' listen='127.0.0.1'>
          <listen type='address' address='127.0.0.1'/>
        </graphics>
        <video>
          <model type='vga' vram='16384' heads='1'/>
        </video>
        {pci_xml}
      </devices>
    </domain>
    """
