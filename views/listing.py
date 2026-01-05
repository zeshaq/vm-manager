import libvirt
import xml.etree.ElementTree as ET
from flask import Blueprint, render_template, request, redirect, url_for, Response, jsonify
import time
from .audit import log_event
from . import project_utils

listing_bp = Blueprint('listing', __name__)

def get_db_connection():
    # Connect to system hypervisor
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

# --- HELPER: FIND HOST GPUS (COMPATIBLE MODE) ---
# --- HELPER: FIND HOST GPUS (Aggressive Scan) ---
# --- HELPER: FIND HOST GPUS (Universal Fix) ---
def get_host_devices():
    """Scans host for all PCI devices using Libvirt NodeDevice API"""
    devices = []
    conn = get_db_connection()
    if not conn:
        print("❌ [Device Scan] Could not connect to Libvirt.")
        return []

    try:
        device_names = conn.listNodeDevices('pci', 0)
        print(f"🔍 [Device Scan] Scanned {len(device_names)} PCI devices.")

        for name in device_names:
            try:
                dev = conn.nodeDeviceLookupByName(name)
                xml_str = dev.XMLDesc()
                tree = ET.fromstring(xml_str)

                # 1. Verify PCI Capability
                pci_cap = tree.find(".//capability[@type='pci']")
                if pci_cap is None:
                    continue
                
                # 2. Get IOMMU Group
                iommu_group = None
                iommu_tag = pci_cap.find("iommuGroup/number")
                if iommu_tag is not None:
                    iommu_group = iommu_tag.text

                # 3. Get Product and Vendor
                product_tag = tree.find(".//product")
                vendor_tag = tree.find(".//vendor")
                product_name = product_tag.text if product_tag is not None and product_tag.text else f"PCI Device ({name})"
                vendor_name = vendor_tag.text if vendor_tag is not None and vendor_tag.text else "Unknown Vendor"

                # 4. Extract Address
                address_tag = pci_cap.find("address")
                if address_tag is None:
                    continue

                domain = f"{int(address_tag.get('domain')):04x}"
                bus = f"{int(address_tag.get('bus')):02x}"
                slot = f"{int(address_tag.get('slot')):02x}"
                function = f"{int(address_tag.get('function')):x}"
                pci_str = f"{domain}:{bus}:{slot}.{function}"

                # 5. Get Vendor and Product ID
                vendor_id = vendor_tag.get('id').replace('0x', '') if vendor_tag is not None and vendor_tag.get('id') else None
                product_id = product_tag.get('id').replace('0x', '') if product_tag is not None and product_tag.get('id') else None

                devices.append({
                    'name': f"{vendor_name} - {product_name}",
                    'pci_id': pci_str,
                    'bus': bus,
                    'slot': slot,
                    'function': function,
                    'iommu_group': iommu_group,
                    'vendor_id': vendor_id,
                    'product_id': product_id
                })
                print(f"✅ [Device Scan] Found: {vendor_name} - {product_name} at {pci_str} (IOMMU: {iommu_group})")

            except Exception as e:
                print(f"⚠️ [Device Scan] Parse Error for {name}: {e}")

    except Exception as e:
        print(f"❌ [Device Scan] Error: {e}")
    finally:
        if conn:
            conn.close()

    devices.sort(key=lambda x: (x['iommu_group'] or 'zzz', x['pci_id']))
    return devices

@listing_bp.route('/list', methods=['GET', 'POST'])
def list_vms():
    conn = get_db_connection()
    if not conn:
        return "Error connecting to hypervisor"

    project_filter = request.args.get('project')

    if request.method == 'POST':
        uuids = request.form.getlist('vm_uuids')
        action = request.form.get('action')

        for uuid in uuids:
            try:
                dom = conn.lookupByUUIDString(uuid)
                if action == 'start':
                    if not dom.isActive():
                        dom.create()
                        log_event('VM Started', target_uuid=uuid, target_name=dom.name())
                elif action == 'stop':
                    if dom.isActive():
                        dom.destroy()
                        log_event('VM Stopped', target_uuid=uuid, target_name=dom.name())
                elif action == 'delete':
                    vm_name = dom.name()
                    if dom.isActive():
                        dom.destroy()
                    dom.undefine()
                    log_event('VM Deleted', target_uuid=uuid, target_name=vm_name)
            except libvirt.libvirtError as e:
                print(f"Error performing action {action} on VM {uuid}: {e}")
        
        conn.close()
        return redirect(url_for('listing.list_vms'))

    # GET request logic
    vms_list = []
    try:
        domains = conn.listAllDomains(0)
        for domain in domains:
            xml_str = domain.XMLDesc(0)
            tree = ET.fromstring(xml_str)
            project_tag = tree.find('metadata/project')
            project = project_tag.text if project_tag is not None else 'N/A'

            if project_filter and project != project_filter:
                continue

            info = domain.info()
            vms_list.append({
                'uuid': domain.UUIDString(),
                'name': domain.name(),
                'project': project,
                'state': get_vm_state_string(info[0]),
                'state_code': info[0],
                'memory_mb': int(info[1] / 1024),
                'vcpus': info[3],
                'terminal_url': url_for('terminal.terminal', vm_name=domain.name())
            })
        vms_list.sort(key=lambda x: x['name'])
    except libvirt.libvirtError as e:
        print(f"Error: {e}")
    finally:
        conn.close()

    projects = project_utils.load_projects()
    return render_template('list.html', vms=vms_list, project_filter=project_filter, projects=projects)

@listing_bp.route('/projects')
def list_projects():
    conn = get_db_connection()
    if not conn:
        return "Error connecting to hypervisor"

    projects = set()
    try:
        domains = conn.listAllDomains(0)
        for domain in domains:
            xml_str = domain.XMLDesc(0)
            tree = ET.fromstring(xml_str)
            project_tag = tree.find('metadata/project')
            if project_tag is not None and project_tag.text:
                projects.add(project_tag.text)
    except libvirt.libvirtError as e:
        print(f"Error: {e}")
    finally:
        conn.close()
    
    sorted_projects = sorted(list(projects))
    return render_template('projects.html', projects=sorted_projects)

@listing_bp.route('/project/delete', methods=['POST'])
def delete_project():
    project_to_delete = request.form.get('project_name')
    if not project_to_delete:
        return redirect(url_for('listing.list_projects'))

    conn = get_db_connection()
    if not conn:
        return "Error connecting to hypervisor"

    try:
        domains = conn.listAllDomains(0)
        for domain in domains:
            xml_str = domain.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
            tree = ET.fromstring(xml_str)
            
            meta = tree.find('metadata')
            if meta is not None:
                project_tag = meta.find('project')
                if project_tag is not None and project_tag.text == project_to_delete:
                    meta.remove(project_tag)
                    # If metadata is now empty, remove it
                    if not list(meta):
                        tree.remove(meta)
                    
                    # Redefine the VM with the modified XML
                    conn.defineXML(ET.tostring(tree).decode())
                    log_event('Project Removed', target_uuid=domain.UUIDString(), target_name=domain.name(), details=f"Project '{project_to_delete}' was removed.")

    except libvirt.libvirtError as e:
        print(f"Error during project deletion: {e}")
    finally:
        conn.close()
    
    return redirect(url_for('listing.list_projects'))


# --- VIEW & PARSING LOGIC ---

@listing_bp.route('/view/<uuid>')
def view_vm(uuid):
    conn = get_db_connection()
    vm_details = {}
    
    if conn:
        try:
            dom = conn.lookupByUUIDString(uuid)
            info = dom.info()
            
            # Get Host GPUs for the dropdown
            available_devices = get_host_devices()

            # Use INACTIVE XML to see configuration
            xml_str = dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
            tree = ET.fromstring(xml_str)
            
            # --- 0. Project ---
            project_tag = tree.find('metadata/project')
            project = project_tag.text if project_tag is not None else 'N/A'

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

            # --- 3. Host GPUs (Passthrough) ---
            hostdevs = []
            for hdev in tree.findall('devices/hostdev'):
                if hdev.get('type') == 'pci':
                    src = hdev.find('source/address')
                    if src is not None:
                        # Reconstruct the PCI address string
                        bus = src.get('bus').replace('0x', '')
                        slot = src.get('slot').replace('0x', '')
                        func = src.get('function').replace('0x', '')
                        
                        pci_str = f"0000:{bus}:{slot}.{func}"
                        name = "Unknown PCI Device"
                        
                        # Match against our scanned list to get the real name (e.g. NVIDIA L40S)
                        for g in available_devices:
                            if g['pci_id'].endswith(f":{bus}:{slot}.{func}"):
                                name = g['name']
                                
                        hostdevs.append({
                            'name': name,
                            'pci_id': pci_str,
                            'bus': f"0x{bus}",
                            'slot': f"0x{slot}",
                            'function': f"0x{func}"
                        })

            # --- 4. Live IPs ---
            if info[0] == libvirt.VIR_DOMAIN_RUNNING:
                try:
                    live_dom = conn.lookupByUUIDString(uuid)
                    ifaces_info = live_dom.interfaceAddresses(libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_LEASE, 0)
                    for _, val in ifaces_info.items():
                        for k_iface in interfaces:
                            if k_iface['mac'] == val.get('hwaddr'):
                                k_iface['ips'] = [ip['addr'] for ip in val.get('addrs', [])]
                except: pass

            # --- 5. Build Boot Device List (for drag-and-drop) ---
            all_boot_options = []
            # Add network first
            all_boot_options.append({'value': 'network', 'text': 'Network (PXE)'})
            # Add disks
            for disk in disks:
                all_boot_options.append({
                    'value': f"disk|{disk['target']}",
                    'text': f"Disk ({disk['target']}) - {disk['file'].split('/')[-1]}"
                })
            
            # Create a map for quick lookups
            option_map = {opt['value']: opt for opt in all_boot_options}
            
            # Get the ordered list based on current_boot
            boot_devices = []
            if current_boot['1'] and current_boot['1'] in option_map:
                boot_devices.append(option_map[current_boot['1']])
            if current_boot['2'] and current_boot['2'] in option_map:
                boot_devices.append(option_map[current_boot['2']])
            
            # Add remaining, non-booted devices
            booted_values = {dev['value'] for dev in boot_devices}
            for option in all_boot_options:
                if option['value'] not in booted_values:
                    boot_devices.append(option)


            # --- 6. Snapshots ---
            snapshots = []
            try:
                snapshot_names = dom.snapshotListNames(0)
                snapshots = [{'name': name} for name in snapshot_names]
            except libvirt.libvirtError:
                pass # Snapshots not supported or no snapshots

            vm_details = {
                'uuid': dom.UUIDString(),
                'name': dom.name(),
                'project': project,
                'state': get_vm_state_string(info[0]),
                'state_code': info[0],
                'memory_mb': int(info[1] / 1024),
                'max_memory_mb': int(info[1] / 1024),
                'vcpus': info[3],
                'os_type': dom.OSType(),
                'interfaces': interfaces,
                'disks': disks,
                'host_devices': hostdevs,
                'available_devices': available_devices, 
                'boot_devices': boot_devices,
                'snapshots': snapshots,
                'current_boot': current_boot # Keep for reference if needed elsewhere
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
            
            # --- AUTO-DETERMINE TARGET DEVICE ---
            xml_str = dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
            tree = ET.fromstring(xml_str)
            
            existing_devs = set()
            for disk in tree.findall('devices/disk'):
                target = disk.find('target')
                if target is not None:
                    existing_devs.add(target.get('dev'))

            # AUTO-DETECT ISO vs DISK & Set Prefix
            is_iso = file_path.lower().endswith('.iso')
            prefix = 'sd' if is_iso else 'vd'

            target_dev = ''
            for letter in 'abcdefghijklmnopqrstuvwxyz':
                dev_name = f"{prefix}{letter}"
                if dev_name not in existing_devs:
                    target_dev = dev_name
                    break
            
            if not target_dev:
                raise Exception("No available disk device names left.")
            # --- END AUTO-DETERMINE ---

            is_iso = file_path.lower().endswith('.iso')
            is_block_device = file_path.startswith('/dev/')

            if is_iso:
                xml = f"""
                <disk type='file' device='cdrom'>
                  <driver name='qemu' type='raw'/>
                  <source file='{file_path}'/>
                  <target dev='{target_dev}' bus='sata'/>
                  <readonly/>
                </disk>
                """
            elif is_block_device:
                xml = f"""
                <disk type='block' device='disk'>
                  <driver name='qemu' type='raw' cache='none' io='native'/>
                  <source dev='{file_path}'/>
                  <target dev='{target_dev}' bus='virtio'/>
                </disk>
                """
            else:
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

@listing_bp.route('/vm/<uuid>/boot', methods=['POST'])
def update_boot_order(uuid):
    conn = get_db_connection()
    if not conn:
        return jsonify({'success': False, 'error': 'Could not connect to hypervisor'})
    try:
        dom = conn.lookupByUUIDString(uuid)
        boot1 = request.form.get('boot1')
        boot2 = request.form.get('boot2')
        
        xml_str = dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
        tree = ET.fromstring(xml_str)
        
        # Clear all existing boot orders
        os_node = tree.find('os')
        for b in os_node.findall('boot'):
            os_node.remove(b)
        for d in tree.findall('.//disk'):
            b = d.find('boot')
            if b is not None: d.remove(b)
        for i in tree.findall('.//interface'):
            b = i.find('boot')
            if b is not None: i.remove(b)
        
        def apply_order(selection, num):
            if not selection or selection == 'none':
                return
            
            type_, _, value = selection.partition('|')

            if type_ == 'disk':
                for d in tree.findall('.//disk'):
                    t = d.find('target')
                    if t is not None and t.get('dev') == value:
                        ET.SubElement(d, 'boot', {'order': str(num)})
                        break
            elif type_ == 'network':
                iface = tree.find('.//interface')
                if iface is not None:
                    ET.SubElement(iface, 'boot', {'order': str(num)})
        
        apply_order(boot1, 1)
        apply_order(boot2, 2)
        
        conn.defineXML(ET.tostring(tree).decode())
        log_event('Boot Order Updated', target_uuid=uuid, target_name=dom.name(), details=f"1st: {boot1}, 2nd: {boot2}")
        return jsonify({'success': True})

    except libvirt.libvirtError as e:
        return jsonify({'success': False, 'error': str(e)})
    finally:
        if conn:
            conn.close()

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

# --- PCI PASSTHROUGH MANAGEMENT ---

@listing_bp.route('/device/attach/<uuid>', methods=['POST'])
def attach_device(uuid):
    pci_id = request.form.get('pci_id') # Expects format "0000:8a:00.0"
    if not pci_id: return "No device selected"
    
    # Parse the string back to hex components
    parts = pci_id.split(':')
    bus = f"0x{parts[1]}"
    slot_func = parts[2].split('.')
    slot = f"0x{slot_func[0]}"
    function = f"0x{slot_func[1]}"
    
    # Managed='yes' means Libvirt will detach from host driver automatically
    xml = f"""
    <hostdev mode='subsystem' type='pci' managed='yes'>
      <source>
        <address domain='0x0000' bus='{bus}' slot='{slot}' function='{function}'/>
      </source>
    </hostdev>
    """
    
    conn = get_db_connection()
    if conn:
        try:
            dom = conn.lookupByUUIDString(uuid)
            flags = libvirt.VIR_DOMAIN_AFFECT_CONFIG
            if dom.isActive(): flags |= libvirt.VIR_DOMAIN_AFFECT_LIVE
            
            dom.attachDeviceFlags(xml, flags)
        except libvirt.libvirtError as e:
            return f"<h1>Error Attaching Device</h1><p>{e}</p><p>Ensure IOMMU is enabled and device is not in use.</p><a href='/view/{uuid}'>Back</a>"
        finally:
            conn.close()
            
    return redirect(url_for('listing.view_vm', uuid=uuid))

@listing_bp.route('/device/detach/<uuid>', methods=['POST'])
def detach_device(uuid):
    pci_id = request.form.get('pci_id')
    if not pci_id: return "No device selected"

    parts = pci_id.split(':')
    bus = f"0x{parts[1]}"
    slot_func = parts[2].split('.')
    slot = f"0x{slot_func[0]}"
    function = f"0x{slot_func[1]}"
    
    xml = f"""
    <hostdev mode='subsystem' type='pci' managed='yes'>
      <source>
        <address domain='0x0000' bus='{bus}' slot='{slot}' function='{function}'/>
      </source>
    </hostdev>
    """
    
    conn = get_db_connection()
    if conn:
        try:
            dom = conn.lookupByUUIDString(uuid)
            flags = libvirt.VIR_DOMAIN_AFFECT_CONFIG
            if dom.isActive(): flags |= libvirt.VIR_DOMAIN_AFFECT_LIVE
            dom.detachDeviceFlags(xml, flags)
        except libvirt.libvirtError as e:
            return f"Error: {e}"
        finally:
            conn.close()
            
    return redirect(url_for('listing.view_vm', uuid=uuid))

# --- ACTIONS ---

@listing_bp.route('/start/<uuid>')
def start_vm(uuid):
    conn = get_db_connection()
    if conn:
        try:
            dom = conn.lookupByUUIDString(uuid)
            dom.create()
            log_event('VM Started', target_uuid=uuid, target_name=dom.name())
        except libvirt.libvirtError as e:
            print(f"Error starting VM: {e}")
        finally:
            conn.close()
    return redirect(url_for('listing.view_vm', uuid=uuid))

@listing_bp.route('/stop/<uuid>')
def stop_vm(uuid):
    conn = get_db_connection()
    if conn:
        try:
            dom = conn.lookupByUUIDString(uuid)
            if dom.isActive():
                dom.destroy()
                log_event('VM Stopped', target_uuid=uuid, target_name=dom.name())
        except libvirt.libvirtError as e:
            print(f"Error stopping VM: {e}")
        finally:
            conn.close()
    return redirect(url_for('listing.view_vm', uuid=uuid))

@listing_bp.route('/stop_all', methods=['POST'])
def stop_all_vms():
    conn = get_db_connection()
    if conn:
        try:
            domains = conn.listAllDomains(0)
            for domain in domains:
                if domain.isActive():
                    domain.destroy()
                    log_event('VM Stopped', target_uuid=domain.UUIDString(), target_name=domain.name(), details="Part of Stop All")
        except libvirt.libvirtError as e:
            print(f"Error stopping all VMs: {e}")
        finally:
            conn.close()
    return redirect(url_for('listing.list_vms'))

@listing_bp.route('/delete/<uuid>')
def delete_vm(uuid):
    conn = get_db_connection()
    if conn:
        try:
            dom = conn.lookupByUUIDString(uuid)
            vm_name = dom.name()
            if dom.isActive():
                dom.destroy()
            dom.undefine()
            log_event('VM Deleted', target_uuid=uuid, target_name=vm_name)
        except libvirt.libvirtError as e:
            print(f"Error deleting VM: {e}")
        finally:
            conn.close()
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
        new_project = request.form.get('project')

        xml_str = dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
        tree = ET.fromstring(xml_str)
        
        # Resources
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
        
        # Metadata / Project
        meta = tree.find('metadata')
        if meta is None and new_project:
            meta = ET.SubElement(tree, 'metadata')

        project_tag = meta.find('project') if meta is not None else None
        
        if new_project:
            if project_tag is not None:
                project_tag.text = new_project
            else:
                ET.SubElement(meta, 'project').text = new_project
        elif project_tag is not None:
            meta.remove(project_tag)

        conn.defineXML(ET.tostring(tree).decode())
        conn.close()
        return redirect(url_for('listing.view_vm', uuid=uuid))

    # GET Request
    info = dom.info()
    xml_str = dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
    tree = ET.fromstring(xml_str)
    project_tag = tree.find('metadata/project')
    project = project_tag.text if project_tag is not None else ''

    vm_data = {
        'uuid': dom.UUIDString(), 
        'name': dom.name(), 
        'ram': int(info[1] / 1024), 
        'cpu': info[3],
        'project': project
    }
    conn.close()
    return render_template('edit.html', vm=vm_data)

@listing_bp.route('/monitor/<uuid>')
def monitor_vm(uuid):
    conn = get_db_connection()
    vm_details = {}
    if conn:
        try:
            dom = conn.lookupByUUIDString(uuid)
            vm_details = {
                'uuid': dom.UUIDString(),
                'name': dom.name(),
            }
        except libvirt.libvirtError as e:
            return f"Error: {e}"
        finally:
            conn.close()
    return render_template('monitor.html', vm=vm_details)

@listing_bp.route('/api/stats/<uuid>')
def vm_stats(uuid):
    conn = get_db_connection()
    if not conn:
        return jsonify({'error': 'Could not connect to libvirt'})
    
    try:
        dom = conn.lookupByUUIDString(uuid)
        if not dom.isActive():
            return jsonify({'error': 'VM is not running.'})

        # --- Dynamic Device Discovery ---
        xml_str = dom.XMLDesc(0)
        tree = ET.fromstring(xml_str)
        
        disk_target = None
        disk_devices = tree.findall('devices/disk')
        for disk in disk_devices:
            if disk.get('device') == 'disk':
                target = disk.find('target')
                if target is not None:
                    disk_target = target.get('dev')
                    break
        
        net_target = None
        net_interfaces = tree.findall('devices/interface')
        if net_interfaces:
            target = net_interfaces[0].find('target')
            if target is not None:
                net_target = target.get('dev')

        # --- CPU Stats ---
        t1 = time.time()
        c1 = dom.info()[4]
        time.sleep(1)
        t2 = time.time()
        c2 = dom.info()[4]
        cpu_usage = (c2 - c1) * 100 / ((t2 - t1) * dom.info()[3] * 1e9)
        
        # --- Memory Stats ---
        mem_stats = dom.memoryStats()
        mem_used = mem_stats.get('actual', 0) / 1024 # Use .get for safety

        # --- Disk Stats ---
        disk_read_bytes, disk_write_bytes = 0, 0
        if disk_target:
            try:
                disk_stats = dom.blockStats(disk_target)
                disk_read_bytes = disk_stats[1]
                disk_write_bytes = disk_stats[3]
            except libvirt.libvirtError:
                pass # Device might not be hot-pluggable or ready

        # --- Network Stats ---
        net_rx_bytes, net_tx_bytes = 0, 0
        if net_target:
            try:
                net_stats = dom.interfaceStats(net_target)
                net_rx_bytes = net_stats[0]
                net_tx_bytes = net_stats[4]
            except libvirt.libvirtError:
                pass # Interface might not be up

        stats = {
            'cpu_usage': round(cpu_usage, 2),
            'mem_used': round(mem_used, 2),
            'disk_read': disk_read_bytes,
            'disk_write': disk_write_bytes,
            'net_rx': net_rx_bytes,
            'net_tx': net_tx_bytes
        }
        return jsonify(stats)

    except libvirt.libvirtError as e:
        return jsonify({'error': str(e)})
    finally:
            conn.close()
            
@listing_bp.route('/snapshot/create/<uuid>', methods=['POST'])
def create_snapshot(uuid):
    snapshot_name = request.form['snapshot_name']
    conn = get_db_connection()
    if conn:
        try:
            dom = conn.lookupByUUIDString(uuid)
            xml = f"<domainsnapshot><name>{snapshot_name}</name></domainsnapshot>"
            dom.snapshotCreateXML(xml, 0)
            log_event('Snapshot Created', target_uuid=uuid, target_name=dom.name(), details=f"Snapshot: {snapshot_name}")
        except libvirt.libvirtError as e:
            return f"Error creating snapshot: {e}"
        finally:
            conn.close()
    return redirect(url_for('listing.view_vm', uuid=uuid))

@listing_bp.route('/snapshot/revert/<uuid>', methods=['POST'])
def revert_snapshot(uuid):
    snapshot_name = request.form['snapshot_name']
    conn = get_db_connection()
    if conn:
        try:
            dom = conn.lookupByUUIDString(uuid)
            snapshot = dom.snapshotLookupByName(snapshot_name, 0)
            dom.revertToSnapshot(snapshot, 0)
            log_event('Snapshot Reverted', target_uuid=uuid, target_name=dom.name(), details=f"Snapshot: {snapshot_name}")
        except libvirt.libvirtError as e:
            return f"Error reverting to snapshot: {e}"
        finally:
            conn.close()
    return redirect(url_for('listing.view_vm', uuid=uuid))

@listing_bp.route('/snapshot/delete/<uuid>', methods=['POST'])
def delete_snapshot(uuid):
    snapshot_name = request.form['snapshot_name']
    conn = get_db_connection()
    if conn:
        try:
            dom = conn.lookupByUUIDString(uuid)
            snapshot = dom.snapshotLookupByName(snapshot_name, 0)
            snapshot.delete(0)
            log_event('Snapshot Deleted', target_uuid=uuid, target_name=dom.name(), details=f"Snapshot: {snapshot_name}")
        except libvirt.libvirtError as e:
            return f"Error deleting snapshot: {e}"
        finally:
            conn.close()
    return redirect(url_for('listing.view_vm', uuid=uuid))