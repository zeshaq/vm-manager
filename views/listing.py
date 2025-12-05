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
            
            # Use INACTIVE XML to see boot order for next start
            # Use ACTIVE XML (0) to see current interfaces/disks if running
            xml_str = dom.XMLDesc(0)
            tree = ET.fromstring(xml_str)
            
            # --- 1. Network Interfaces Parsing ---
            interfaces = []
            for iface in tree.findall('devices/interface'):
                iface_data = {
                    'type': iface.get('type'),
                    'mac': iface.find('mac').get('address') if iface.find('mac') is not None else 'N/A',
                    'network': 'Unknown',
                    'model': iface.find('model').get('type') if iface.find('model') is not None else 'Default',
                    'ips': []
                }
                source = iface.find('source')
                if source is not None:
                    iface_data['network'] = source.get('network') or source.get('bridge') or source.get('dev')
                interfaces.append(iface_data)

            # Get Live IPs
            if info[0] == libvirt.VIR_DOMAIN_RUNNING:
                try:
                    ifaces_info = dom.interfaceAddresses(libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_LEASE, 0)
                    for _, val in ifaces_info.items():
                        for k_iface in interfaces:
                            if k_iface['mac'] == val.get('hwaddr'):
                                k_iface['ips'] = [ip['addr'] for ip in val.get('addrs', [])]
                except: pass

            # --- 2. Disks Parsing ---
            disks = []
            for disk in tree.findall('devices/disk'):
                disk_data = {
                    'device': disk.get('device'), # disk or cdrom
                    'file': 'N/A',
                    'target': 'N/A',
                    'type': disk.get('type')
                }
                source = disk.find('source')
                if source is not None:
                    disk_data['file'] = source.get('file')
                target = disk.find('target')
                if target is not None:
                    disk_data['target'] = target.get('dev')
                disks.append(disk_data)

            # --- 3. Boot Order Parsing ---
            # Boot order is best read from the config (INACTIVE) to see what happens next boot
            xml_cfg = dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
            tree_cfg = ET.fromstring(xml_cfg)
            boot_order = []
            os_node = tree_cfg.find('os')
            if os_node is not None:
                for b in os_node.findall('boot'):
                    boot_order.append(b.get('dev'))

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
                'disks': disks,
                'boot_order': boot_order,
                'xml': xml_str
            }
        except libvirt.libvirtError as e:
            return f"Error: {e}"
        finally:
            conn.close()
            
    return render_template('view.html', vm=vm_details)

# --- STORAGE MANAGEMENT ---

@listing_bp.route('/disk/add/<uuid>', methods=['POST'])
def add_disk(uuid):
    conn = get_db_connection()
    if conn:
        try:
            dom = conn.lookupByUUIDString(uuid)
            file_path = request.form['file_path']
            target_dev = request.form['target_dev'] # e.g., vdb
            
            xml = f"""
            <disk type='file' device='disk'>
              <driver name='qemu' type='qcow2'/>
              <source file='{file_path}'/>
              <target dev='{target_dev}' bus='virtio'/>
            </disk>
            """
            
            flags = libvirt.VIR_DOMAIN_AFFECT_CONFIG
            if dom.isActive():
                flags |= libvirt.VIR_DOMAIN_AFFECT_LIVE

            dom.attachDeviceFlags(xml, flags)
        except libvirt.libvirtError as e:
            return f"Error adding disk: {e}"
        finally:
            conn.close()
    return redirect(url_for('listing.view_vm', uuid=uuid))

@listing_bp.route('/disk/delete/<uuid>', methods=['POST'])
def delete_disk(uuid):
    target_dev = request.form['target_dev']
    conn = get_db_connection()
    if conn:
        try:
            dom = conn.lookupByUUIDString(uuid)
            
            # Find the full XML definition of the disk to detach
            xml_str = dom.XMLDesc(0) if dom.isActive() else dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
            tree = ET.fromstring(xml_str)
            disk_xml = None
            
            for disk in tree.findall('devices/disk'):
                tgt = disk.find('target')
                if tgt is not None and tgt.get('dev') == target_dev:
                    disk_xml = ET.tostring(disk).decode()
                    break
            
            if disk_xml:
                flags = libvirt.VIR_DOMAIN_AFFECT_CONFIG
                if dom.isActive():
                    flags |= libvirt.VIR_DOMAIN_AFFECT_LIVE
                dom.detachDeviceFlags(disk_xml, flags)
                
        except libvirt.libvirtError as e:
            return f"Error removing disk: {e}"
        finally:
            conn.close()
    return redirect(url_for('listing.view_vm', uuid=uuid))

# --- BOOT MANAGEMENT ---

@listing_bp.route('/boot_order/<uuid>', methods=['POST'])
def update_boot_order(uuid):
    conn = get_db_connection()
    if conn:
        try:
            dom = conn.lookupByUUIDString(uuid)
            device1 = request.form.get('boot1')
            device2 = request.form.get('boot2')
            
            # modifying boot order requires redefining the XML
            xml_str = dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
            tree = ET.fromstring(xml_str)
            
            os_node = tree.find('os')
            # Remove existing boot tags
            for boot in os_node.findall('boot'):
                os_node.remove(boot)
            
            # Add new boot tags
            if device1:
                ET.SubElement(os_node, 'boot', {'dev': device1})
            if device2 and device2 != 'none':
                ET.SubElement(os_node, 'boot', {'dev': device2})
                
            new_xml = ET.tostring(tree).decode()
            dom.defineXML(new_xml)
            
        except libvirt.libvirtError as e:
            return f"Error updating boot order: {e}"
        finally:
            conn.close()
    return redirect(url_for('listing.view_vm', uuid=uuid))

# --- NETWORK MANAGEMENT (Existing) ---

@listing_bp.route('/interface/add/<uuid>', methods=['POST'])
def add_interface(uuid):
    conn = get_db_connection()
    if conn:
        try:
            dom = conn.lookupByUUIDString(uuid)
            network_name = request.form.get('network', 'default')
            xml = f"<interface type='network'><source network='{network_name}'/><model type='virtio'/></interface>"
            flags = libvirt.VIR_DOMAIN_AFFECT_CONFIG
            if dom.isActive(): flags |= libvirt.VIR_DOMAIN_AFFECT_LIVE
            dom.attachDeviceFlags(xml, flags)
        except libvirt.libvirtError as e: return f"Error: {e}"
        finally: conn.close()
    return redirect(url_for('listing.view_vm', uuid=uuid))

@listing_bp.route('/interface/delete/<uuid>', methods=['POST'])
def delete_interface(uuid):
    mac = request.form.get('mac')
    conn = get_db_connection()
    if conn:
        try:
            dom = conn.lookupByUUIDString(uuid)
            xml_str = dom.XMLDesc(0) if dom.isActive() else dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
            tree = ET.fromstring(xml_str)
            iface_xml = None
            for iface in tree.findall('devices/interface'):
                m = iface.find('mac')
                if m is not None and m.get('address') == mac:
                    iface_xml = ET.tostring(iface).decode()
                    break
            if iface_xml:
                flags = libvirt.VIR_DOMAIN_AFFECT_CONFIG
                if dom.isActive(): flags |= libvirt.VIR_DOMAIN_AFFECT_LIVE
                dom.detachDeviceFlags(iface_xml, flags)
        except libvirt.libvirtError as e: return f"Error: {e}"
        finally: conn.close()
    return redirect(url_for('listing.view_vm', uuid=uuid))

# --- STANDARD ACTIONS (Start/Stop/Delete/Edit/Console) ---
# (Keep your existing Start, Stop, Delete, Console, Edit functions here exactly as they were)
# I am omitting them here for brevity, but they MUST remain in the file.
@listing_bp.route('/start/<uuid>')
def start_vm(uuid):
    conn = get_db_connection()
    if conn:
        try:
            dom = conn.lookupByUUIDString(uuid)
            dom.create()
        except libvirt.libvirtError as e: print(f"{e}")
        finally: conn.close()
    return redirect(url_for('listing.view_vm', uuid=uuid))

@listing_bp.route('/stop/<uuid>')
def stop_vm(uuid):
    conn = get_db_connection()
    if conn:
        try:
            dom = conn.lookupByUUIDString(uuid)
            if dom.isActive(): dom.destroy()
        except libvirt.libvirtError as e: print(f"{e}")
        finally: conn.close()
    return redirect(url_for('listing.view_vm', uuid=uuid))

@listing_bp.route('/delete/<uuid>')
def delete_vm(uuid):
    conn = get_db_connection()
    if conn:
        try:
            dom = conn.lookupByUUIDString(uuid)
            if dom.isActive(): dom.destroy()
            dom.undefine()
        except libvirt.libvirtError as e: print(f"{e}")
        finally: conn.close()
    return redirect(url_for('listing.list_vms'))

@listing_bp.route('/console/<uuid>')
def console_vm(uuid):
    # (Copy the console logic from previous step)
    conn = get_db_connection()
    port = None
    if conn:
        try:
            dom = conn.lookupByUUIDString(uuid)
            xml_str = dom.XMLDesc(0)
            tree = ET.fromstring(xml_str)
            graphics = tree.find("./devices/graphics[@type='vnc']")
            if graphics is not None: port = graphics.get('port')
        except: pass
        finally: conn.close()
    if not port or port == '-1': return "VM Not Running"
    host_ip = request.host.split(':')[0]
    vv = f"[virt-viewer]\ntype=vnc\nhost={host_ip}\nport={port}\ndelete-this-file=1\ntitle=Console-{uuid}\n"
    return Response(vv, mimetype="application/x-virt-viewer", headers={"Content-disposition": f"attachment; filename=console-{uuid}.vv"})

@listing_bp.route('/edit/<uuid>', methods=['GET', 'POST'])
def edit_vm(uuid):
    # (Copy edit logic from previous step)
    conn = get_db_connection()
    if not conn: return "Error"
    dom = conn.lookupByUUIDString(uuid)
    if request.method == 'POST':
        # ... logic to update ram/cpu ...
        new_cpu = int(request.form['cpu'])
        new_ram_mb = int(request.form['ram'])
        new_ram_kib = new_ram_mb * 1024
        xml_str = dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
        tree = ET.fromstring(xml_str)
        
        mem_node = tree.find('memory')
        if mem_node is not None:
            mem_node.text = str(new_ram_kib)
            mem_node.set('unit', 'KiB')
        curr_mem_node = tree.find('currentMemory')
        if curr_mem_node is not None:
            curr_mem_node.text = str(new_ram_kib)
            curr_mem_node.set('unit', 'KiB')
        vcpu_node = tree.find('vcpu')
        if vcpu_node is not None:
            vcpu_node.text = str(new_cpu)
            
        dom.defineXML(ET.tostring(tree).decode())
        conn.close()
        return redirect(url_for('listing.view_vm', uuid=uuid))

    info = dom.info()
    vm_data = {'uuid': dom.UUIDString(), 'name': dom.name(), 'ram': int(info[1] / 1024), 'cpu': info[3]}
    conn.close()
    return render_template('edit.html', vm=vm_data)