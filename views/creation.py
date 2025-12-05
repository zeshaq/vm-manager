import libvirt
from flask import Blueprint, render_template, request, redirect, url_for

creation_bp = Blueprint('creation', __name__)

def generate_vm_xml(name, memory_mb, vcpus, host_cpu=False):
    # Convert MB to KiB
    memory_kib = int(memory_mb) * 1024
    
    # CPU Model Configuration
    cpu_xml = ""
    if host_cpu:
        cpu_xml = "<cpu mode='host-passthrough' check='none'/>"
        
    return f"""
    <domain type='kvm'>
      <name>{name}</name>
      <memory unit='KiB'>{memory_kib}</memory>
      <vcpu placement='static'>{vcpus}</vcpu>
      {cpu_xml}
      <os>
        <type arch='x86_64' machine='pc-q35-6.2'>hvm</type>
        <boot dev='hd'/>
        <boot dev='cdrom'/>
        <boot dev='network'/>
      </os>
      <features><acpi/><apic/></features>
      <devices>
        <emulator>/usr/bin/qemu-system-x86_64</emulator>
        
        <!-- Network Interface (Default NAT) -->
        <interface type='network'>
          <source network='default'/>
          <model type='virtio'/>
        </interface>
        
        <!-- Graphics -->
        <graphics type='vnc' port='-1' autoport='yes' listen='0.0.0.0'/>
      </devices>
    </domain>
    """

@creation_bp.route('/create', methods=['GET', 'POST'])
def create_vm():
    if request.method == 'POST':
        try:
            name = request.form['name']
            ram = request.form['ram'] # Value is in MB
            cpu = request.form['cpu']
            
            # CPU Passthrough Checkbox
            use_host_cpu = request.form.get('host_cpu') == 'on'

            # Generate XML
            xml_config = generate_vm_xml(name, ram, cpu, use_host_cpu)

            conn = libvirt.open('qemu:///system')
            if conn:
                # defineXML returns the Domain object
                dom = conn.defineXML(xml_config)
                
                # Get UUID for redirection
                new_uuid = dom.UUIDString()
                conn.close()
                
                # Redirect directly to the View page
                return redirect(url_for('listing.view_vm', uuid=new_uuid))
                
        except Exception as e:
            return f"<h1>Error Creating VM</h1><p>{e}</p><a href='/create'>Try Again</a>"

    return render_template('create.html')