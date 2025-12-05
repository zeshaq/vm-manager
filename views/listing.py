import libvirt
import xml.etree.ElementTree as ET
from flask import Blueprint, render_template, request, redirect, url_for, flash

listing_bp = Blueprint('listing', __name__)

def get_db_connection():
    # 'qemu:///system' gives Read/Write access (required for start/stop/delete)
    return libvirt.open('qemu:///system')

def get_vm_state_string(state_int):
    states = {
        libvirt.VIR_DOMAIN_NOSTATE: "No State",
        libvirt.VIR_DOMAIN_RUNNING: "Running",
        libvirt.VIR_DOMAIN_BLOCKED: "Blocked",
        libvirt.VIR_DOMAIN_PAUSED: "Paused",
        libvirt.VIR_DOMAIN_SHUTDOWN: "Shutting Down",
        libvirt.VIR_DOMAIN_SHUTOFF: "Shutoff",
        libvirt.VIR_DOMAIN_CRASHED: "Crashed",
        libvirt.VIR_DOMAIN_PMSUSPENDED: "Suspended",
    }
    return states.get(state_int, "Unknown")

@listing_bp.route('/list')
def list_vms():
    vms_list = []
    conn = get_db_connection()
    if conn:
        try:
            domains = conn.listAllDomains(0)
            for domain in domains:
                info = domain.info()
                vms_list.append({
                    'uuid': domain.UUIDString(),
                    'name': domain.name(),
                    'state': get_vm_state_string(info[0]),
                    'state_code': info[0],
                    'memory_mb': int(info[1] / 1024),
                    'vcpus': info[3]
                })
        except libvirt.libvirtError as e:
            print(f"Error: {e}")
        finally:
            conn.close()
    return render_template('list.html', vms=vms_list)

# --- ACTIONS ---

@listing_bp.route('/start/<uuid>')
def start_vm(uuid):
    conn = get_db_connection()
    if conn:
        try:
            dom = conn.lookupByUUIDString(uuid)
            dom.create() # Boot the VM
        except libvirt.libvirtError as e:
            print(f"Error starting VM: {e}")
        finally:
            conn.close()
    return redirect(url_for('listing.list_vms'))

@listing_bp.route('/stop/<uuid>')
def stop_vm(uuid):
    conn = get_db_connection()
    if conn:
        try:
            dom = conn.lookupByUUIDString(uuid)
            # destroy() is hard power-off (like pulling the plug). 
            # Use shutdown() for soft ACPI shutdown (might not work if OS hangs).
            if dom.isActive():
                dom.destroy() 
        except libvirt.libvirtError as e:
            print(f"Error stopping VM: {e}")
        finally:
            conn.close()
    return redirect(url_for('listing.list_vms'))

@listing_bp.route('/delete/<uuid>')
def delete_vm(uuid):
    conn = get_db_connection()
    if conn:
        try:
            dom = conn.lookupByUUIDString(uuid)
            # Stop it first if running
            if dom.isActive():
                dom.destroy()
            # Undefine removes the XML configuration
            dom.undefine()
        except libvirt.libvirtError as e:
            print(f"Error deleting VM: {e}")
        finally:
            conn.close()
    return redirect(url_for('listing.list_vms'))

@listing_bp.route('/view/<uuid>')
def view_vm(uuid):
    vm_details = {}
    conn = get_db_connection()
    if conn:
        try:
            dom = conn.lookupByUUIDString(uuid)
            info = dom.info()
            vm_details = {
                'uuid': dom.UUIDString(),
                'name': dom.name(),
                'state': get_vm_state_string(info[0]),
                'memory_mb': int(info[1] / 1024),
                'vcpus': info[3],
                'os_type': dom.OSType(),
                'xml': dom.XMLDesc() # Raw XML for debugging
            }
        finally:
            conn.close()
    return render_template('view.html', vm=vm_details)

@listing_bp.route('/edit/<uuid>', methods=['GET', 'POST'])
def edit_vm(uuid):
    conn = get_db_connection()
    if not conn:
        return "Failed to connect to Hypervisor"

    try:
        dom = conn.lookupByUUIDString(uuid)
        
        if request.method == 'POST':
            # 1. Parse existing XML
            xml_str = dom.XMLDesc()
            tree = ET.fromstring(xml_str)
            
            # 2. Update values from Form
            new_ram_kib = int(request.form['ram']) * 1024
            new_cpu = request.form['cpu']
            
            # Find and update <memory> and <currentMemory>
            for mem_tag in tree.findall('memory'):
                mem_tag.text = str(new_ram_kib)
            for mem_tag in tree.findall('currentMemory'):
                mem_tag.text = str(new_ram_kib)
                
            # Find and update <vcpu>
            vcpu_tag = tree.find('vcpu')
            if vcpu_tag is not None:
                vcpu_tag.text = str(new_cpu)
            
            # 3. Define the new XML (updates the persistent config)
            new_xml = ET.tostring(tree).decode()
            conn.defineXML(new_xml)
            
            return redirect(url_for('listing.list_vms'))
        
        # GET Request: Show form with current values
        info = dom.info()
        current_data = {
            'uuid': uuid,
            'name': dom.name(),
            'ram': int(info[1] / 1024),
            'cpu': info[3]
        }
        return render_template('edit.html', vm=current_data)
        
    except libvirt.libvirtError as e:
        return f"Libvirt Error: {e}"
    finally:
        conn.close()