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

@listing_bp.route('/view/<uuid>')
def view_vm(uuid):
    conn = get_db_connection()
    vm_details = {}
    
    if conn:
        try:
            dom = conn.lookupByUUIDString(uuid)
            info = dom.info()
            
            # Use INACTIVE XML to see configuration
            xml_str = dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
            tree = ET.fromstring(xml_str)
            
            # --- 1. Disks & Boot Order ---
            disks = []
            current_boot = {'1': None, '2': None}

            for disk in tree.findall('devices/disk'):
                disk_data = {
                    'device': disk.get('device'),
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
                
                # Check for per-device boot order
                boot = disk.find('boot')
                if boot is not None:
                    order = boot.get('order')
                    if order in ['1', '2']:
                        current_boot[order] = f"disk|{disk_data['target']}"

                disks.append(disk_data)

            # --- 2. Interfaces & Boot Order ---
            interfaces = []
            for iface in tree.findall('devices/interface'):
                mac = iface.find('mac').get('address') if iface.find('mac') is not None else 'N/A'
                model = iface.find('model').get('type') if iface.find('model') is not None else 'Default'
                
                net_source = "Unknown"
                source = iface.find('source')
                if source is not None:
                    net_source = source.get('network') or source.get('bridge') or source.get('dev') or "Unknown"

                boot = iface.find('boot')
                if boot is not None:
                    order = boot.get('order')
                    if order in ['1', '2']:
                        current_boot[order] = "network"

                interfaces.append({
                    'mac': mac,
                    'model': model,
                    'network': net_source,
                    'type': iface.get('type'),
                    'ips': []
                })

            # --- 3. Live IPs ---
            if info[0] == libvirt.VIR_DOMAIN_RUNNING:
                try:
                    live_dom = conn.lookupByUUIDString(uuid)
                    ifaces_info = live_dom.interfaceAddresses(libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_LEASE, 0)
                    for _, val in ifaces_info.items():
                        for k_iface in interfaces:
                            if k_iface['mac'] == val.get('hwaddr'):
                                k_iface['ips'] = [ip['addr'] for ip in val.get('addrs', [])]
                except: pass

            # --- 4. Legacy Boot Fallback ---
            if not current_boot['1']:
                os_boot = tree.findall('os/boot')
                if len(os_boot) > 0:
                    dev = os_boot[0].get('dev')
                    if dev == 'hd': current_boot['1'] = 'hd_generic'
                    elif dev == 'cdrom': current_boot['1'] = 'cdrom_generic'
                    elif dev == 'network': current_boot['1'] = 'network'
                if len(os_boot) > 1:
                    dev = os_boot[1].get('dev')
                    if dev == 'hd': current_boot['2'] = 'hd_generic'
                    elif dev == 'cdrom': current_boot['2'] = 'cdrom_generic'
                    elif dev == 'network': current_boot['2'] = 'network'

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
                'current_boot': current_boot
            }
        except libvirt.libvirtError as e:
            return f"Error: {e}"
        finally:
            conn.close()
            
    return render_template('view.html', vm=vm_details)

# --- STORAGE MANAGEMENT (UPDATED) ---

@listing_bp.route('/disk/add/<uuid>', methods=['POST'])
def add_disk(uuid):
    conn = get_db_connection()
    if conn:
        try:
            dom = conn.lookupByUUIDString(uuid)
            file_path = request.form['file_path']
            target_dev = request.form['target_dev']
            
            # 1. AUTO-DETECT ISO vs DISK
            is_iso = file_path.lower().endswith('.iso')
            
            if is_iso:
                # Configuration for CDROM/ISO
                # Use raw driver, sata bus, and readonly tag
                xml = f"""
                <disk type='file' device='cdrom'>
                  <driver name='qemu' type='raw'/>
                  <source file='{file_path}'/>
                  <target dev='{target_dev}' bus='sata'/>
                  <readonly/>
                </disk>
                """
            else:
                # Configuration for Standard Disk (qcow2)
                # Use qcow2 driver, virtio bus
                xml = f"""
                <disk type='file' device='disk'>
                  <driver name='qemu' type='qcow2'/>
                  <source file='{file_path}'/>
                  <target dev='{target_dev}' bus='virtio'/>
                </disk>
                """
            
            flags = libvirt.VIR_DOMAIN_AFFECT_CONFIG
            if dom.isActive(): flags |= libvirt.VIR_DOMAIN_AFFECT_LIVE
            dom.attachDeviceFlags(xml, flags)
            
        except libvirt.libvirtError as e: return f"Error: {e}"
        finally: conn.close()
    return redirect(url_for('listing.view_vm', uuid=uuid))

@listing_bp.route('/disk/delete/<uuid>', methods=['POST'])
def delete_disk(uuid):
    target_dev = request.form['target_dev']
    conn = get_db_connection()
    if conn:
        try:
            dom = conn.lookupByUUIDString(uuid)
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
                if dom.isActive(): flags |= libvirt.VIR_DOMAIN_AFFECT_LIVE
                dom.detachDeviceFlags(disk_xml, flags)
        except libvirt.libvirtError as e: return f"Error: {e}"
        finally: conn.close()
    return redirect(url_for('listing.view_vm', uuid=uuid))

# --- BOOT MANAGEMENT ---

@listing_bp.route('/boot_order/<uuid>', methods=['POST'])
def update_boot_order(uuid):
    conn = get_db_connection()
    if conn:
        try:
            dom = conn.lookupByUUIDString(uuid)
            boot1 = request.form.get('boot1')
            boot2 = request.form.get('boot2')
            
            xml_str = dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
            tree = ET.fromstring(xml_str)
            
            os_node = tree.find('os')
            for b in os_node.findall('boot'): os_node.remove(b)
            for d in tree.findall('devices/disk'):
                b = d.find('boot')
                if b is not None: d.remove(b)
            for i in tree.findall('devices/interface'):
                b = i.find('boot')
                if b is not None: i.remove(b)
            
            def apply_order(selection, num):
                if not selection or selection == 'none': return
                parts = selection.split('|')
                type_ = parts[0]
                if type_ == 'disk':
                    target = parts[1]
                    for d in tree.findall('devices/disk'):
                        t = d.find('target')
                        if t is not None and t.get('dev') == target:
                            ET.SubElement(d, 'boot', {'order': str(num)})
                            break
                elif type_ == 'network':
                    iface = tree.find('devices/interface')
                    if iface is not None:
                        ET.SubElement(iface, 'boot', {'order': str(num)})
                elif type_ in ['hd_generic', 'cdrom_generic']:
                    dev_map = {'hd_generic': 'hd', 'cdrom_generic': 'cdrom'}
                    ET.SubElement(os_node, 'boot', {'dev': dev_map[type_]})
            
            apply_order(boot1, 1)
            apply_order(boot2, 2)
            
            conn.defineXML(ET.tostring(tree).decode())
            
        except libvirt.libvirtError as e: return f"Error: {e}"
        finally: conn.close()
    return redirect(url_for('listing.view_vm', uuid=uuid))

# --- NETWORK MANAGEMENT ---

@listing_bp.route('/interface/add/<uuid>', methods=['POST'])
def add_interface(uuid):
    conn = get_db_connection()
    if conn:
        try:
            dom = conn.lookupByUUIDString(uuid)
            mode = request.form.get('mode')
            source = request.form.get('source')
            if mode == 'bridge':
                xml = f"<interface type='bridge'><source bridge='{source}'/><model type='virtio'/></interface>"
            else:
                xml = f"<interface type='network'><source network='{source}'/><model type='virtio'/></interface>"
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

# --- ACTIONS ---

@listing_bp.route('/start/<uuid>')
def start_vm(uuid):
    conn = get_db_connection()
    if conn:
        try:
            dom = conn.lookupByUUIDString(uuid)
            dom.create()
        except: pass
        finally: conn.close()
    return redirect(url_for('listing.view_vm', uuid=uuid))

@listing_bp.route('/stop/<uuid>')
def stop_vm(uuid):
    conn = get_db_connection()
    if conn:
        try:
            dom = conn.lookupByUUIDString(uuid)
            if dom.isActive(): dom.destroy()
        except: pass
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
        except: pass
        finally: conn.close()
    return redirect(url_for('listing.list_vms'))

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
            if graphics is not None: port = graphics.get('port')
        except: pass
        finally: conn.close()
    if not port or port == '-1': return "<h1>VM Not Running</h1><p>Start VM first.</p><a href='/list'>Back</a>"
    host_ip = request.host.split(':')[0]
    vv_content = f"[virt-viewer]\ntype=vnc\nhost={host_ip}\nport={port}\ndelete-this-file=1\ntitle=Console-{uuid}\n"
    return Response(vv_content, mimetype="application/x-virt-viewer", headers={"Content-disposition": f"attachment; filename=console-{uuid}.vv"})

@listing_bp.route('/edit/<uuid>', methods=['GET', 'POST'])
def edit_vm(uuid):
    conn = get_db_connection()
    if not conn: return "Error"
    dom = conn.lookupByUUIDString(uuid)
    if request.method == 'POST':
        new_cpu = int(request.form['cpu'])
        new_ram_mb = int(request.form['ram'])
        xml_str = dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
        tree = ET.fromstring(xml_str)
        mem = tree.find('memory')
        curr = tree.find('currentMemory')
        if mem is not None: 
            mem.text = str(new_ram_mb * 1024)
            mem.set('unit', 'KiB')
        if curr is not None: 
            curr.text = str(new_ram_mb * 1024)
            curr.set('unit', 'KiB')
        vcpu = tree.find('vcpu')
        if vcpu is not None: vcpu.text = str(new_cpu)
        conn.defineXML(ET.tostring(tree).decode())
        conn.close()
        return redirect(url_for('listing.view_vm', uuid=uuid))
    info = dom.info()
    vm_data = {'uuid': dom.UUIDString(), 'name': dom.name(), 'ram': int(info[1] / 1024), 'cpu': info[3]}
    conn.close()
    return render_template('edit.html', vm=vm_data)