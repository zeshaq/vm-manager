import libvirt
from flask import Blueprint
from .listing import get_host_devices, parse_pci_id

# VM creation is handled entirely through POST /api/vms (views/api.py).
# This blueprint is kept so the import in app.py remains valid.
# The helper functions below are used by views/api.py.
creation_bp = Blueprint('creation', __name__)


def generate_vm_xml(name, memory_mb, vcpus, project=None, host_cpu=False, devices=None, disk_path=None, disks=None):
    """Generate libvirt domain XML for a new VM.

    disks: list of resolved disk paths (strings).
      - .iso  → attached as a SATA cdrom (read-only); targets sda, sdb, ...
      - anything else → attached as a virtio disk; targets vda, vdb, ...

    disk_path: legacy single-disk shorthand (converted to a 1-element disks list).
    Both can be provided; disk_path is prepended.
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

    # Build the combined disk list
    all_disks = []
    if disk_path:
        all_disks.append(disk_path)
    if disks:
        all_disks.extend(disks)

    # Generate XML for each disk with sequential target device names
    disk_xml = ""
    virtio_idx = 0  # vda, vdb, vdc ...
    sata_idx   = 0  # sda, sdb, sdc ...
    for path in all_disks:
        if path.lower().endswith('.iso'):
            target = 'sd' + chr(ord('a') + sata_idx)
            sata_idx += 1
            disk_xml += f"""
        <!-- ISO (cdrom) -->
        <disk type='file' device='cdrom'>
          <driver name='qemu' type='raw'/>
          <source file='{path}'/>
          <target dev='{target}' bus='sata'/>
          <readonly/>
        </disk>"""
        else:
            target = 'vd' + chr(ord('a') + virtio_idx)
            virtio_idx += 1
            disk_xml += f"""
        <!-- Disk {target} -->
        <disk type='file' device='disk'>
          <driver name='qemu' type='qcow2' cache='none'/>
          <source file='{path}'/>
          <target dev='{target}' bus='virtio'/>
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
