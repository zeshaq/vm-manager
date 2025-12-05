import libvirt
from flask import Blueprint, render_template, request, redirect, url_for

creation_bp = Blueprint('creation', __name__)

def generate_vm_xml(name, memory_mb, vcpus, disk_path, iso_path):
    memory_kib = int(memory_mb) * 1024
    return f"""
    <domain type='kvm'>
      <name>{name}</name>
      <memory unit='KiB'>{memory_kib}</memory>
      <vcpu placement='static'>{vcpus}</vcpu>
      <os>
        <type arch='x86_64' machine='pc-q35-6.2'>hvm</type>
        <boot dev='hd'/>
        <boot dev='cdrom'/>
      </os>
      <features><acpi/><apic/></features>
      <devices>
        <emulator>/usr/bin/qemu-system-x86_64</emulator>
        <disk type='file' device='disk'>
          <driver name='qemu' type='qcow2'/>
          <source file='{disk_path}'/>
          <target dev='vda' bus='virtio'/>
        </disk>
        <disk type='file' device='cdrom'>
          <driver name='qemu' type='raw'/>
          <source file='{iso_path}'/>
          <target dev='sda' bus='sata'/>
          <readonly/>
        </disk>
        <interface type='network'>
          <source network='default'/>
          <model type='virtio'/>
        </interface>
        <graphics type='vnc' port='-1' autoport='yes' listen='0.0.0.0'/>
      </devices>
    </domain>
    """

@creation_bp.route('/create', methods=['GET', 'POST'])
def create_vm():
    if request.method == 'POST':
        try:
            name = request.form['name']
            ram = request.form['ram']
            cpu = request.form['cpu']
            disk = request.form['disk_path']
            iso = request.form['iso_path']

            xml_config = generate_vm_xml(name, ram, cpu, disk, iso)

            conn = libvirt.open('qemu:///system')
            if conn:
                conn.defineXML(xml_config)
                conn.close()
                return redirect(url_for('listing.list_vms'))
                
        except libvirt.libvirtError as e:
            return f"<h1>Error Creating VM</h1><p>{e}</p><a href='/create'>Try Again</a>"

    return render_template('create.html')