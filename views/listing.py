import libvirt
import xml.etree.ElementTree as ET
from flask import Blueprint, render_template, request, redirect, url_for, Response

listing_bp = Blueprint('listing', __name__)

def get_db_connection():
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
            info = dom.info()
            
            # --- Network Interface Logic ---
            # We use VIR_DOMAIN_XML_SECURE to get full details
            xml_str = dom.XMLDesc(0)
            tree = ET.fromstring(xml_str)
            interfaces = []

            for iface in tree.findall('devices/interface'):
                iface_data = {
                    'type': iface.get('type'),
                    'mac': 'N/A',
                    'network': 'N/A',
                    'model': 'Default',
                    'ips': []
                }

                mac_node = iface.find('mac')
                if mac_node is not None:
                    iface_data['mac'] = mac_node.get('address')

                source_node = iface.find('source')
                if source_node is not None:
                    iface_data['network'] = (
                        source_node.get('network') or 
                        source_node.get('bridge') or 
                        source_node.get('dev') or "Unknown"
                    )

                model_node = iface.find('model')
                if model_node is not None:
                    iface_data['model'] = model_node.get('type')

                interfaces.append(iface_data)

            if info[0] == libvirt.VIR_DOMAIN_RUNNING:
                try:
                    ifaces_info = dom.interfaceAddresses(libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_LEASE, 0)
                    for iface_name, val in ifaces_info.items():
                        hwaddr = val.get('hwaddr')
                        for known_iface in interfaces:
                            if known_iface['mac'] == hwaddr:
                                known_iface['ips'] = [ip['addr'] for ip in val.get('addrs', [])]
                except libvirt.libvirtError:
                    pass
            
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

# --- NETWORK MANAGEMENT ---

@listing_bp.route('/interface/add/<uuid>', methods=['POST'])
def add_interface(uuid):
    conn = get_db_connection()
    if conn:
        try:
            dom = conn.lookupByUUIDString(uuid)
            network_name = request.form.get('network', 'default')
            
            # Simple XML for a virtio network interface attached to a named network
            # We don't specify MAC; libvirt generates it automatically.
            xml = f"""
            <interface type='network'>
              <source network='{network_name}'/>
              <model type='virtio'/>
            </interface>
            """
            
            # Determine flags: Affect CONFIG (next boot) + LIVE (now) if running
            flags = libvirt.VIR_DOMAIN_AFFECT_CONFIG
            if dom.isActive():
                flags |= libvirt.VIR_DOMAIN_AFFECT_LIVE

            dom.attachDeviceFlags(xml, flags)
            
        except libvirt.libvirtError as e:
            return f"Error adding interface: {e}"
        finally:
            conn.close()
    return redirect(url_for('listing.view_vm', uuid=uuid))

@listing_bp.route('/interface/delete/<uuid>', methods=['POST'])
def delete_interface(uuid):
    mac_to_delete = request.form.get('mac')
    conn = get_db_connection()
    
    if conn and mac_to_delete:
        try:
            dom = conn.lookupByUUIDString(uuid)
            
            # 1. Get current config to find the specific XML block for this MAC
            # We need the full XML block to tell Libvirt what to detach.
            xml_str = dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
            if dom.isActive():
                 xml_str = dom.XMLDesc(0) # Get live XML if running
            
            tree = ET.fromstring(xml_str)
            
            # 2. Search for the interface with the matching MAC
            interface_xml_str = None
            
            for iface in tree.findall('devices/interface'):
                mac_node = iface.find('mac')
                if mac_node is not None and mac_node.get('address') == mac_to_delete:
                    # Found it! Convert this element back to string
                    interface_xml_str = ET.tostring(iface).decode()
                    break
            
            if interface_xml_str:
                # 3. Detach the device
                flags = libvirt.VIR_DOMAIN_AFFECT_CONFIG
                if dom.isActive():
                    flags |= libvirt.VIR_DOMAIN_AFFECT_LIVE
                
                dom.detachDeviceFlags(interface_xml_str, flags)
            else:
                return "Interface with that MAC not found."

        except libvirt.libvirtError as e:
            return f"Error deleting interface: {e}"
        finally:
            conn.close()
            
    return redirect(url_for('listing.view_vm', uuid=uuid))

# --- STANDARD VM ACTIONS ---

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
    return redirect(url_for('listing.view_vm', uuid=uuid)) # Redirect to view

@listing_bp.route('/stop/<uuid>')
def stop_vm(uuid):
    conn = get_db_connection()
    if conn:
        try:
            dom = conn.lookupByUUIDString(uuid)
            if dom.isActive():
                dom.destroy()
        except libvirt.libvirtError as e:
            print(f"Error stopping VM: {e}")
        finally:
            conn.close()
    return redirect(url_for('listing.view_vm', uuid=uuid)) # Redirect to view

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

# --- CONSOLE ---

@listing_bp.route('/console/<uuid>')
def console_vm(uuid):
    conn = get_db_connection()
    port = None
    if conn:
        try:
            dom = conn.lookupByUUIDString(uuid)
            xml_str = dom.XMLDesc(0)
            tree = ET.fromstring(xml_str)
            graphics = tree.find("./devices/graphics[@type='vnc']")
            if graphics is not None:
                port = graphics.get('port')
        except Exception as e:
            print(f"Console Error: {e}")
        finally:
            conn.close()

    if not port or port == '-1':
        return "<h1>VM Not Running</h1><p>Please start the VM before opening the console.</p><a href='/list'>Back</a>"

    host_ip = request.host.split(':')[0]
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
    if not conn: return "Could not connect"
    try:
        dom = conn.lookupByUUIDString(uuid)
        if request.method == 'POST':
            new_cpu = int(request.form['cpu'])
            new_ram_mb = int(request.form['ram'])
            new_ram_kib = new_ram_mb * 1024
            xml_str = dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
            tree = ET.fromstring(xml_str)
            
            mem_node = tree.find('memory')
            if mem_node is not None:
                mem_node.text = str(new_ram_kib)
                mem_node.set('unit', 'KiB')
            curr_mem = tree.find('currentMemory')
            if curr_mem is not None:
                curr_mem.text = str(new_ram_kib)
                curr_mem.set('unit', 'KiB')
                
            vcpu_node = tree.find('vcpu')
            if vcpu_node is not None: vcpu_node.text = str(new_cpu)
            
            dom.defineXML(ET.tostring(tree).decode())
            conn.close()
            return redirect(url_for('listing.view_vm', uuid=uuid))

        info = dom.info()
        vm_data = {'uuid': dom.UUIDString(), 'name': dom.name(), 'ram': int(info[1] / 1024), 'cpu': info[3]}
        conn.close()
        return render_template('edit.html', vm=vm_data)
    except libvirt.libvirtError as e:
        if conn: conn.close()
        return f"Error: {e}"