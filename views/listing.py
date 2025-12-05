===== /Users/ze/vm-manager/views/listing.py =====
import libvirt
import xml.etree.ElementTree as ET
from flask import Blueprint, render_template, request, redirect, url_for, Response

listing_bp = Blueprint('listing', __name__)

def get_db_connection():
    # Connect to system hypervisor (Read/Write)
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

@listing_bp.route('/view/<uuid>')
def view_vm(uuid):
    conn = get_db_connection()
    vm_details = {}
    
    if conn:
        try:
            dom = conn.lookupByUUIDString(uuid)
            info = dom.info() # [state, maxmem, mem, vcpus, cputime]
            
            # --- Network Interface Logic ---
            xml_str = dom.XMLDesc(0)
            tree = ET.fromstring(xml_str)
            interfaces = []

            # 1. Parse XML to get configuration (MAC, Model, Network Source)
            for iface in tree.findall('devices/interface'):
                iface_data = {
                    'type': iface.get('type'),
                    'mac': 'N/A',
                    'network': 'N/A',
                    'model': 'Default',
                    'ips': []
                }

                # Get MAC
                mac_node = iface.find('mac')
                if mac_node is not None:
                    iface_data['mac'] = mac_node.get('address')

                # Get Source (e.g., network='default', bridge='br0', or dev='eth0')
                source_node = iface.find('source')
                if source_node is not None:
                    iface_data['network'] = (
                        source_node.get('network') or 
                        source_node.get('bridge') or 
                        source_node.get('dev') or "Unknown"
                    )

                # Get Model (e.g., virtio)
                model_node = iface.find('model')
                if model_node is not None:
                    iface_data['model'] = model_node.get('type')

                interfaces.append(iface_data)

            # 2. Try to get IP addresses (Only works if VM is running)
            if info[0] == libvirt.VIR_DOMAIN_RUNNING:
                try:
                    # Fetch IPs from DHCP leases or Guest Agent
                    ifaces_info = dom.interfaceAddresses(libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_LEASE, 0)
                    
                    # Match IPs to the interfaces we found via XML based on MAC address
                    for iface_name, val in ifaces_info.items():
                        hwaddr = val.get('hwaddr')
                        for known_iface in interfaces:
                            if known_iface['mac'] == hwaddr:
                                # Extract IP list
                                known_iface['ips'] = [ip['addr'] for ip in val.get('addrs', [])]
                except libvirt.libvirtError:
                    # Likely means guest agent not running or no leases found yet
                    pass
            
            # --- End Network Logic ---

            vm_details = {
                'uuid': dom.UUIDString(),
                'name': dom.name(),
                'state': get_vm_state_string(info[0]),
                'state_code': info[0],
                'memory_mb': int(info[1] / 1024),
                'max_memory_mb': int(info[1] / 1024),
                'vcpus': info[3],
                'os_type': dom.OSType(),
                'interfaces': interfaces,
                'xml': xml_str
            }
        except libvirt.libvirtError as e:
            return f"Error: {e}"
        finally:
            conn.close()
            
    return render_template('view.html', vm=vm_details)


@listing_bp.route('/start/<uuid>')
def start_vm(uuid):
    conn = get_db_connection()
    if conn:
        try:
            dom = conn.lookupByUUIDString(uuid)
            dom.create()
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
            if dom.isActive():
                dom.destroy() # Force power off
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
            if dom.isActive():
                dom.destroy()
            dom.undefine()
        except libvirt.libvirtError as e:
            print(f"Error deleting VM: {e}")
        finally:
            conn.close()
    return redirect(url_for('listing.list_vms'))

# --- CONSOLE LOGIC ---

@listing_bp.route('/console/<uuid>')
def console_vm(uuid):
    conn = get_db_connection()
    port = None
    
    if conn:
        try:
            dom = conn.lookupByUUIDString(uuid)
            # Get XML description of the running domain to find the VNC port
            xml_str = dom.XMLDesc(0)
            tree = ET.fromstring(xml_str)
            
            # Look for the VNC graphics node
            graphics = tree.find("./devices/graphics[@type='vnc']")
            if graphics is not None:
                port = graphics.get('port')
        except Exception as e:
            print(f"Console Error: {e}")
        finally:
            conn.close()

    # If VM is off or no VNC configured
    if not port or port == '-1':
        return "<h1>VM Not Running</h1><p>Please start the VM before opening the console.</p><a href='/list'>Back</a>"

    # Detect the IP address the user is using to access this web app
    host_ip = request.host.split(':')[0]

    # Content of the .vv file
    vv_content = f"""[virt-viewer]
type=vnc
host={host_ip}
port={port}
delete-this-file=1
title=Console-{uuid}
"""

    return Response(
        vv_content,
        mimetype="application/x-virt-viewer",
        headers={"Content-disposition": f"attachment; filename=console-{uuid}.vv"}
    )

@listing_bp.route('/edit/<uuid>', methods=['GET', 'POST'])
def edit_vm(uuid):
    conn = get_db_connection()
    if not conn:
        return "Could not connect to Hypervisor"

    try:
        dom = conn.lookupByUUIDString(uuid)
        
        if request.method == 'POST':
            # 1. Get new values
            new_cpu = int(request.form['cpu'])
            new_ram_mb = int(request.form['ram'])
            new_ram_kib = new_ram_mb * 1024

            # 2. Get the current XML configuration (INACTIVE definition)
            xml_str = dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
            tree = ET.fromstring(xml_str)

            # 3. Update Memory
            mem_node = tree.find('memory')
            curr_mem_node = tree.find('currentMemory')
            
            if mem_node is not None:
                mem_node.text = str(new_ram_kib)
                mem_node.set('unit', 'KiB')
            
            if curr_mem_node is not None:
                curr_mem_node.text = str(new_ram_kib)
                curr_mem_node.set('unit', 'KiB')

            # 4. Update vCPUs
            vcpu_node = tree.find('vcpu')
            if vcpu_node is not None:
                vcpu_node.text = str(new_cpu)

            # 5. Redefine the VM with the updated XML
            new_xml = ET.tostring(tree).decode()
            conn.defineXML(new_xml)

            conn.close()
            return redirect(url_for('listing.view_vm', uuid=uuid))

        # --- GET Request (Render the form) ---
        info = dom.info()
        vm_data = {
            'uuid': dom.UUIDString(),
            'name': dom.name(),
            'ram': int(info[1] / 1024), # Max memory
            'cpu': info[3]              # vCPUs
        }
        conn.close()
        return render_template('edit.html', vm=vm_data)

    except libvirt.libvirtError as e:
        if conn: conn.close()
        return f"Error editing VM: {e}"