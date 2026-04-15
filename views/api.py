import libvirt
import xml.etree.ElementTree as ET
import os
import re
import subprocess
import time
import datetime
import psutil
import uuid as uuid_module

from flask import Blueprint, request, jsonify, session, current_app
from werkzeug.utils import secure_filename
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from .listing import get_db_connection, get_vm_state_string, get_host_devices, parse_pci_id
from .creation import generate_vm_xml

api_bp = Blueprint('api', __name__, url_prefix='/api')
limiter = Limiter(key_func=get_remote_address)

STORAGE_PATH       = '/var/lib/libvirt/images'
CLOUD_IMAGES_PATH  = '/var/lib/libvirt/images/cloud-images'
CLOUD_LOCALDS_BIN  = '/usr/bin/cloud-localds'
ALLOWED_EXTENSIONS = {'iso', 'img', 'qcow2'}
ALLOWED_DISK_FORMATS = {'qcow2', 'raw'}
VM_NAME_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$')
PCI_ID_RE  = re.compile(r'^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F]$')
MAC_RE     = re.compile(r'^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def require_auth():
    """Return a 401 response if user is not authenticated, else None."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    return None


def validate_uuid(uuid_str):
    try:
        uuid_module.UUID(uuid_str)
        return True
    except (ValueError, AttributeError):
        return False


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def get_human_readable_size(size_in_bytes):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_in_bytes < 1024.0:
            return f"{size_in_bytes:.2f} {unit}"
        size_in_bytes /= 1024.0
    return f"{size_in_bytes:.2f} TB"


# ---------------------------------------------------------------------------
# Auth endpoints (public)
# ---------------------------------------------------------------------------

@api_bp.route('/login', methods=['POST'])
@limiter.limit("10 per minute; 30 per hour")
def login():
    import simplepam
    data = request.get_json() or {}
    username = str(data.get('username', ''))[:64]
    password = str(data.get('password', ''))[:256]
    if not username or not password:
        return jsonify({'success': False, 'error': 'Invalid credentials'})
    if simplepam.authenticate(username, password, service='vm-manager'):
        session.clear()          # prevent session fixation
        session['username'] = username
        session.permanent = bool(data.get('remember_me'))
        current_app.logger.info(f"Login: {username} from {request.remote_addr}")
        return jsonify({'success': True})
    current_app.logger.warning(f"Failed login for '{username}' from {request.remote_addr}")
    return jsonify({'success': False, 'error': 'Invalid credentials'})


@api_bp.route('/logout', methods=['POST'])
def logout():
    session.pop('username', None)
    return jsonify({'success': True})


@api_bp.route('/auth/check', methods=['GET'])
def auth_check():
    if 'username' in session:
        return jsonify({'authenticated': True, 'username': session['username']})
    return jsonify({'authenticated': False})


# ---------------------------------------------------------------------------
# Host info
# ---------------------------------------------------------------------------

@api_bp.route('/host', methods=['GET'])
def host_info():
    err = require_auth()
    if err:
        return err

    conn = get_db_connection()
    host_info = {'hostname': os.uname().nodename}

    if conn:
        try:
            node_info = conn.getInfo()
            host_info['cpu_cores'] = node_info[2]
            host_info['cpu_percent'] = round(psutil.cpu_percent(interval=0.2), 1)

            la1, la5, la15 = os.getloadavg()
            host_info['load_1'] = round(la1, 2)
            host_info['load_5'] = round(la5, 2)
            host_info['load_15'] = round(la15, 2)

            numa_nodes = int(node_info[4]) if len(node_info) > 4 else 1
            if numa_nodes <= 0:
                numa_nodes = 1

            total_kib = 0
            free_kib = 0
            for cell in range(numa_nodes):
                stats = conn.getMemoryStats(cell)
                total_kib += stats.get('total', 0)
                free_kib += stats.get('free', 0)

            mem_total_gb = total_kib / (1024 ** 2)
            mem_free_gb = free_kib / (1024 ** 2)
            mem_used_gb = mem_total_gb - mem_free_gb

            host_info['mem_total_gb'] = round(mem_total_gb, 2)
            host_info['mem_free_gb'] = round(mem_free_gb, 2)
            host_info['mem_used_gb'] = round(mem_used_gb, 2)
            host_info['mem_percent_used'] = round((mem_used_gb / mem_total_gb) * 100, 1) if mem_total_gb > 0 else 0

            storage_pools = []
            for pool_name in conn.listStoragePools():
                pool = conn.storagePoolLookupByName(pool_name)
                pool.refresh(0)
                info = pool.info()
                storage_pools.append({
                    'name': pool_name,
                    'capacity_gb': round(info[1] / (1024 ** 3), 2),
                    'allocation_gb': round(info[2] / (1024 ** 3), 2),
                    'available_gb': round(info[3] / (1024 ** 3), 2)
                })
            host_info['storage_pools'] = storage_pools

        except libvirt.libvirtError as e:
            return jsonify({'error': str(e)}), 500
        finally:
            conn.close()

    return jsonify(host_info)


# ---------------------------------------------------------------------------
# VMs
# ---------------------------------------------------------------------------

@api_bp.route('/vms', methods=['GET'])
def list_vms():
    err = require_auth()
    if err:
        return err

    conn = get_db_connection()
    if not conn:
        return jsonify({'error': 'Could not connect to hypervisor'}), 500

    vms_list = []
    try:
        domains = conn.listAllDomains(0)
        for domain in domains:
            xml_str = domain.XMLDesc(0)
            tree = ET.fromstring(xml_str)
            info = domain.info()
            vms_list.append({
                'uuid': domain.UUIDString(),
                'name': domain.name(),
                'state': get_vm_state_string(info[0]),
                'state_code': info[0],
                'memory_mb': int(info[1] / 1024),
                'vcpus': info[3],
            })
        vms_list.sort(key=lambda x: x['name'])
    except libvirt.libvirtError as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

    return jsonify(vms_list)


@api_bp.route('/vms', methods=['POST'])
def create_vm():
    err = require_auth()
    if err:
        return err

    data = request.get_json() or {}
    name = str(data.get('name', '')).strip()
    ram = data.get('ram')
    cpu = data.get('cpu')
    host_cpu = data.get('host_cpu', False)
    devices = data.get('devices', [])
    # Multi-disk support: `disks` is a list of {path, size_gb}.
    # Legacy single-disk fields (disk_path / disk_size_gb) are still accepted.
    raw_disks = data.get('disks')
    if raw_disks is None:
        # Legacy path
        legacy_path = str(data.get('disk_path', '')).strip() or None
        legacy_size = data.get('disk_size_gb', 20)
        raw_disks = [{'path': legacy_path, 'size_gb': legacy_size}] if legacy_path else []

    if not name or not ram or not cpu:
        return jsonify({'error': 'Missing required fields: name, ram, cpu'}), 400
    if not VM_NAME_RE.match(name):
        return jsonify({'error': 'Invalid VM name (alphanumeric, dots, dashes, underscores only)'}), 400
    try:
        ram = int(ram)
        cpu = int(cpu)
        if ram < 64 or ram > 1048576:
            return jsonify({'error': 'RAM must be between 64 MB and 1 TB'}), 400
        if cpu < 1 or cpu > 256:
            return jsonify({'error': 'CPU count must be between 1 and 256'}), 400
    except (ValueError, TypeError):
        return jsonify({'error': 'ram and cpu must be integers'}), 400

    # Resolve / create disk images ─────────────────────────────────────────────
    resolved_disks = []   # final paths passed to generate_vm_xml
    overlays_created = [] # track for rollback on failure
    safe_name = re.sub(r'[^a-zA-Z0-9._-]', '_', name)

    for idx, disk_entry in enumerate(raw_disks):
        disk_path = str(disk_entry.get('path', '')).strip()
        if not disk_path:
            continue
        disk_size_gb = int(disk_entry.get('size_gb', 20))
        if disk_size_gb < 1 or disk_size_gb > 65536:
            return jsonify({'error': f'Disk size must be between 1 and 65536 GB (disk {idx+1})'}), 400

        if not os.path.exists(disk_path):
            return jsonify({'error': f'Disk image not found: {disk_path}'}), 422

        if disk_path.lower().endswith('.iso'):
            # ISO → attach directly as cdrom (read-only, safe to share)
            resolved_disks.append(disk_path)
        else:
            # Cloud / raw image → create a per-VM qcow2 overlay so the base
            # image is never modified and multiple VMs can share the same base.
            suffix = '' if idx == 0 else f'-disk{idx+1}'
            overlay_path = os.path.join(STORAGE_PATH, f"{safe_name}{suffix}.qcow2")
            if os.path.exists(overlay_path):
                # Roll back any overlays created so far
                for p in overlays_created:
                    try: os.remove(p)
                    except OSError: pass
                return jsonify({'error': f'Disk already exists: {os.path.basename(overlay_path)}'}), 409
            try:
                cmd = [
                    'qemu-img', 'create',
                    '-f', 'qcow2',
                    '-b', disk_path,
                    '-F', 'qcow2',
                    overlay_path,
                    f"{disk_size_gb}G",
                ]
                subprocess.run(cmd, check=True, capture_output=True, text=True)
                current_app.logger.info(f"Created overlay disk: {overlay_path} backing {disk_path}")
                resolved_disks.append(overlay_path)
                overlays_created.append(overlay_path)
            except subprocess.CalledProcessError as exc:
                for p in overlays_created:
                    try: os.remove(p)
                    except OSError: pass
                return jsonify({'error': f'Failed to create disk overlay: {exc.stderr.strip()}'}), 500

    seed_iso = None

    # Define the VM ────────────────────────────────────────────────────────────
    try:
        xml_config = generate_vm_xml(name, ram, cpu, None, host_cpu, devices, disks=resolved_disks)
        conn = libvirt.open('qemu:///system')
        if not conn:
            raise RuntimeError('Could not connect to hypervisor')
        dom = conn.defineXML(xml_config)
        new_uuid = dom.UUIDString()
        conn.close()
        current_app.logger.info(f"VM created: {name} ({new_uuid}) by {session.get('username')}")
        return jsonify({'uuid': new_uuid}), 201
    except libvirt.libvirtError as e:
        for p in overlays_created:
            try: os.remove(p)
            except OSError: pass
        if seed_iso and os.path.exists(seed_iso):
            try: os.remove(seed_iso)
            except OSError: pass
        return jsonify({'error': str(e)}), 500
    except Exception:
        for p in overlays_created:
            try: os.remove(p)
            except OSError: pass
        if seed_iso and os.path.exists(seed_iso):
            try: os.remove(seed_iso)
            except OSError: pass
        current_app.logger.exception("Unexpected error creating VM")
        return jsonify({'error': 'Failed to create VM'}), 500


@api_bp.route('/vms/<uuid>', methods=['GET'])
def get_vm(uuid):
    err = require_auth()
    if err:
        return err

    conn = get_db_connection()
    if not conn:
        return jsonify({'error': 'Could not connect to hypervisor'}), 500

    try:
        dom = conn.lookupByUUIDString(uuid)
        info = dom.info()
        available_devices = get_host_devices()

        xml_str = dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
        tree = ET.fromstring(xml_str)

        # Disks
        disks = []
        current_boot = {'1': None, '2': None}
        for disk in tree.findall('devices/disk'):
            disk_data = {
                'device': disk.get('device'),
                'file': 'N/A',
                'target': 'N/A',
                'type': disk.get('type'),
                'boot_order': 0,
            }
            source = disk.find('source')
            if source is not None:
                disk_data['file'] = source.get('file') or source.get('dev') or 'N/A'
            target = disk.find('target')
            if target is not None:
                disk_data['target'] = target.get('dev')
            boot = disk.find('boot')
            if boot is not None:
                order = boot.get('order')
                try:
                    disk_data['boot_order'] = int(order)
                except (TypeError, ValueError):
                    pass
                if order in ['1', '2']:
                    current_boot[order] = f"disk|{disk_data['target']}"
            disks.append(disk_data)

        # Interfaces
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

        # Host PCI devices
        hostdevs = []
        for hdev in tree.findall('devices/hostdev'):
            if hdev.get('type') == 'pci':
                src = hdev.find('source/address')
                if src is not None:
                    bus = src.get('bus').replace('0x', '')
                    slot = src.get('slot').replace('0x', '')
                    func = src.get('function').replace('0x', '')
                    pci_str = f"0000:{bus}:{slot}.{func}"
                    name_str = "Unknown PCI Device"
                    for g in available_devices:
                        if g['pci_id'].endswith(f":{bus}:{slot}.{func}"):
                            name_str = g['name']
                    hostdevs.append({'name': name_str, 'pci_id': pci_str})

        # Live IPs
        if info[0] == libvirt.VIR_DOMAIN_RUNNING:
            try:
                ifaces_info = dom.interfaceAddresses(libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_LEASE, 0)
                for _, val in ifaces_info.items():
                    for k_iface in interfaces:
                        if k_iface['mac'] == val.get('hwaddr'):
                            k_iface['ips'] = [ip['addr'] for ip in val.get('addrs', [])]
            except libvirt.libvirtError:
                pass

        # Boot devices list
        all_boot_options = [{'value': 'network', 'text': 'Network (PXE)'}]
        for disk in disks:
            fname = disk['file'].split('/')[-1] if disk['file'] != 'N/A' else disk['target']
            all_boot_options.append({
                'value': f"disk|{disk['target']}",
                'text': f"Disk ({disk['target']}) - {fname}"
            })
        option_map = {opt['value']: opt for opt in all_boot_options}
        boot_devices = []
        if current_boot['1'] and current_boot['1'] in option_map:
            boot_devices.append(option_map[current_boot['1']])
        if current_boot['2'] and current_boot['2'] in option_map:
            boot_devices.append(option_map[current_boot['2']])
        booted_values = {dev['value'] for dev in boot_devices}
        for option in all_boot_options:
            if option['value'] not in booted_values:
                boot_devices.append(option)

        # Snapshots
        snapshots = []
        try:
            snapshot_names = dom.snapshotListNames(0)
            snapshots = [{'name': n} for n in snapshot_names]
        except libvirt.libvirtError:
            pass

        vm_details = {
            'uuid': dom.UUIDString(),
            'name': dom.name(),
            'state': get_vm_state_string(info[0]),
            'state_code': info[0],
            'memory_mb': int(info[1] / 1024),
            'vcpus': info[3],
            'os_type': dom.OSType(),
            'interfaces': interfaces,
            'disks': disks,
            'host_devices': hostdevs,
            'available_devices': available_devices,
            'boot_devices': boot_devices,
            'snapshots': snapshots,
        }
        return jsonify(vm_details)

    except libvirt.libvirtError as e:
        return jsonify({'error': str(e)}), 404
    finally:
        conn.close()


@api_bp.route('/vms/<uuid>', methods=['PUT'])
def update_vm(uuid):
    err = require_auth()
    if err:
        return err

    data = request.get_json() or {}
    new_cpu = data.get('cpu')
    new_ram_mb = data.get('ram')

    conn = get_db_connection()
    if not conn:
        return jsonify({'error': 'Could not connect to hypervisor'}), 500

    try:
        dom = conn.lookupByUUIDString(uuid)
        xml_str = dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
        tree = ET.fromstring(xml_str)

        if new_ram_mb is not None:
            new_ram_mb = int(new_ram_mb)
            mem = tree.find('memory')
            curr = tree.find('currentMemory')
            if mem is not None:
                mem.text = str(new_ram_mb * 1024)
                mem.set('unit', 'KiB')
            if curr is not None:
                curr.text = str(new_ram_mb * 1024)
                curr.set('unit', 'KiB')

        if new_cpu is not None:
            vcpu = tree.find('vcpu')
            if vcpu is not None:
                vcpu.text = str(int(new_cpu))

        conn.defineXML(ET.tostring(tree).decode())
        return jsonify({'success': True})

    except libvirt.libvirtError as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@api_bp.route('/vms/<uuid>', methods=['DELETE'])
def delete_vm(uuid):
    err = require_auth()
    if err:
        return err

    conn = get_db_connection()
    if not conn:
        return jsonify({'error': 'Could not connect to hypervisor'}), 500

    try:
        dom = conn.lookupByUUIDString(uuid)
        if dom.isActive():
            dom.destroy()
        dom.undefine()
        return jsonify({'success': True})
    except libvirt.libvirtError as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@api_bp.route('/vms/<uuid>/start', methods=['POST'])
def start_vm(uuid):
    err = require_auth()
    if err:
        return err

    conn = get_db_connection()
    if not conn:
        return jsonify({'error': 'Could not connect to hypervisor'}), 500

    try:
        dom = conn.lookupByUUIDString(uuid)
        dom.create()
        return jsonify({'success': True})
    except libvirt.libvirtError as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@api_bp.route('/vms/<uuid>/stop', methods=['POST'])
def stop_vm(uuid):
    err = require_auth()
    if err:
        return err

    conn = get_db_connection()
    if not conn:
        return jsonify({'error': 'Could not connect to hypervisor'}), 500

    try:
        dom = conn.lookupByUUIDString(uuid)
        if dom.isActive():
            dom.destroy()
        return jsonify({'success': True})
    except libvirt.libvirtError as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@api_bp.route('/vms/stop_all', methods=['POST'])
def stop_all_vms():
    err = require_auth()
    if err:
        return err

    conn = get_db_connection()
    if not conn:
        return jsonify({'error': 'Could not connect to hypervisor'}), 500

    try:
        domains = conn.listAllDomains(0)
        for domain in domains:
            if domain.isActive():
                domain.destroy()
        return jsonify({'success': True})
    except libvirt.libvirtError as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Disk management
# ---------------------------------------------------------------------------

@api_bp.route('/vms/<uuid>/disks', methods=['POST'])
def add_disk(uuid):
    err = require_auth()
    if err:
        return err

    data = request.get_json() or {}
    file_path = data.get('file_path')
    if not file_path:
        return jsonify({'error': 'file_path is required'}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({'error': 'Could not connect to hypervisor'}), 500

    try:
        dom = conn.lookupByUUIDString(uuid)
        xml_str = dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
        tree = ET.fromstring(xml_str)

        existing_devs = set()
        for disk in tree.findall('devices/disk'):
            target = disk.find('target')
            if target is not None:
                existing_devs.add(target.get('dev'))

        is_iso = file_path.lower().endswith('.iso')
        prefix = 'sd' if is_iso else 'vd'
        target_dev = ''
        for letter in 'abcdefghijklmnopqrstuvwxyz':
            dev_name = f"{prefix}{letter}"
            if dev_name not in existing_devs:
                target_dev = dev_name
                break

        if not target_dev:
            return jsonify({'error': 'No available disk device names left'}), 500

        is_block_device = file_path.startswith('/dev/')

        # Build XML safely with ElementTree (prevents XML injection)
        if is_iso:
            disk_el = ET.Element('disk', type='file', device='cdrom')
            ET.SubElement(disk_el, 'driver', name='qemu', type='raw')
            ET.SubElement(disk_el, 'source', file=file_path)
            ET.SubElement(disk_el, 'target', dev=target_dev, bus='sata')
            ET.SubElement(disk_el, 'readonly')
        elif is_block_device:
            disk_el = ET.Element('disk', type='block', device='disk')
            ET.SubElement(disk_el, 'driver', name='qemu', type='raw', cache='none', io='native')
            ET.SubElement(disk_el, 'source', dev=file_path)
            ET.SubElement(disk_el, 'target', dev=target_dev, bus='virtio')
        else:
            disk_el = ET.Element('disk', type='file', device='disk')
            ET.SubElement(disk_el, 'driver', name='qemu', type='qcow2')
            ET.SubElement(disk_el, 'source', file=file_path)
            ET.SubElement(disk_el, 'target', dev=target_dev, bus='virtio')
        xml = ET.tostring(disk_el).decode()

        flags = libvirt.VIR_DOMAIN_AFFECT_CONFIG
        if dom.isActive():
            flags |= libvirt.VIR_DOMAIN_AFFECT_LIVE
        dom.attachDeviceFlags(xml, flags)
        return jsonify({'success': True})

    except libvirt.libvirtError as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@api_bp.route('/vms/<uuid>/disks', methods=['DELETE'])
def delete_disk(uuid):
    err = require_auth()
    if err:
        return err

    data = request.get_json() or {}
    target_dev = data.get('target_dev')
    if not target_dev:
        return jsonify({'error': 'target_dev is required'}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({'error': 'Could not connect to hypervisor'}), 500

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
            if dom.isActive():
                flags |= libvirt.VIR_DOMAIN_AFFECT_LIVE
            dom.detachDeviceFlags(disk_xml, flags)
        return jsonify({'success': True})

    except libvirt.libvirtError as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Cloud image setup — create overlay + cloud-init seed ISO and attach both
# ---------------------------------------------------------------------------

@api_bp.route('/vms/<uuid>/cloud-image', methods=['POST'])
def attach_cloud_image(uuid):
    err = require_auth()
    if err:
        return err

    data = request.get_json() or {}
    base_image = str(data.get('base_image', '')).strip()
    disk_size_gb = int(data.get('disk_size_gb') or 20)
    if not base_image:
        return jsonify({'error': 'base_image is required'}), 400
    if not os.path.exists(base_image):
        return jsonify({'error': f'Image not found: {base_image}'}), 422

    conn = get_db_connection()
    if not conn:
        return jsonify({'error': 'Could not connect to hypervisor'}), 500

    try:
        dom = conn.lookupByUUIDString(uuid)
        vm_name = dom.name()
        safe_name = re.sub(r'[^a-zA-Z0-9._-]', '_', vm_name)

        created = []

        # 1. qcow2 overlay
        overlay = os.path.join(STORAGE_PATH, f'{safe_name}.qcow2')
        if os.path.exists(overlay):
            return jsonify({'error': f'Overlay already exists: {os.path.basename(overlay)}'}), 409
        subprocess.run(
            ['qemu-img', 'create', '-f', 'qcow2', '-b', base_image, '-F', 'qcow2', overlay, f'{disk_size_gb}G'],
            check=True, capture_output=True, text=True
        )
        created.append(overlay)

        # 2. cloud-init seed ISO — stored in /tmp so it is removed automatically on reboot
        import tempfile, shutil
        seed_iso = os.path.join('/tmp', f'{safe_name}-seed.iso')

        # Generate a proper SHA-512 password hash so the password is permanent
        # and never expires regardless of cloud-init version.
        pw_result = subprocess.run(
            ['openssl', 'passwd', '-6', 'ze'],
            capture_output=True, text=True, check=True
        )
        passwd_hash = pw_result.stdout.strip()

        user_data = f"""\
#cloud-config
hostname: {vm_name}
users:
  - name: ze
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    lock_passwd: false
    passwd: {passwd_hash}
chpasswd:
  expire: false
ssh_pwauth: true
runcmd:
  - touch /etc/cloud/cloud-init.disabled
  - systemctl disable cloud-init cloud-init-local cloud-config cloud-final 2>/dev/null || true
"""
        meta_data = f"instance-id: {safe_name}\nlocal-hostname: {vm_name}\n"
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, 'user-data'), 'w') as f: f.write(user_data)
            with open(os.path.join(tmp, 'meta-data'), 'w') as f: f.write(meta_data)
            subprocess.run(
                [CLOUD_LOCALDS_BIN, seed_iso, os.path.join(tmp, 'user-data'), os.path.join(tmp, 'meta-data')],
                check=True, capture_output=True, text=True
            )
            created.append(seed_iso)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        # 3. attach both devices
        flags = libvirt.VIR_DOMAIN_AFFECT_CONFIG
        if dom.isActive():
            flags |= libvirt.VIR_DOMAIN_AFFECT_LIVE

        for path in [overlay, seed_iso]:
            is_iso = path.endswith('.iso')
            bus = 'sata' if is_iso else 'virtio'
            # find next free target device
            xml_str = dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
            tree = ET.fromstring(xml_str)
            used = {d.find('target').get('dev') for d in tree.findall('devices/disk') if d.find('target') is not None}
            if is_iso:
                idx = next(i for i in range(26) if f'sd{chr(ord("a")+i)}' not in used)
                target = f'sd{chr(ord("a")+idx)}'
                disk_xml = f"""<disk type='file' device='cdrom'>
  <driver name='qemu' type='raw'/>
  <source file='{path}'/>
  <target dev='{target}' bus='sata'/>
  <readonly/>
</disk>"""
            else:
                idx = next(i for i in range(26) if f'vd{chr(ord("a")+i)}' not in used)
                target = f'vd{chr(ord("a")+idx)}'
                disk_xml = f"""<disk type='file' device='disk'>
  <driver name='qemu' type='qcow2' cache='none'/>
  <source file='{path}'/>
  <target dev='{target}' bus='virtio'/>
</disk>"""
            dom.attachDeviceFlags(disk_xml, flags)

        current_app.logger.info(f'Cloud image setup for {vm_name}: overlay={overlay}, seed={seed_iso}')
        return jsonify({'success': True, 'overlay': overlay, 'seed_iso': seed_iso}), 200

    except subprocess.CalledProcessError as e:
        for p in created:
            try: os.remove(p)
            except OSError: pass
        return jsonify({'error': f'Disk setup failed: {e.stderr.strip()}'}), 500
    except libvirt.libvirtError as e:
        for p in created:
            try: os.remove(p)
            except OSError: pass
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Boot order
# ---------------------------------------------------------------------------

@api_bp.route('/vms/<uuid>/boot', methods=['PUT'])
def update_boot_order(uuid):
    err = require_auth()
    if err:
        return err

    data = request.get_json() or {}

    # Accept either an ordered list: devices=['disk|vda','disk|vdb',...]
    # or legacy boot1/boot2 strings
    devices = data.get('devices')
    if devices is None:
        boot1 = data.get('boot1')
        boot2 = data.get('boot2')
        devices = [d for d in [boot1, boot2] if d]

    conn = get_db_connection()
    if not conn:
        return jsonify({'error': 'Could not connect to hypervisor'}), 500

    try:
        dom = conn.lookupByUUIDString(uuid)
        xml_str = dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
        tree = ET.fromstring(xml_str)

        os_node = tree.find('os')
        for b in os_node.findall('boot'):
            os_node.remove(b)
        for d in tree.findall('.//disk'):
            b = d.find('boot')
            if b is not None:
                d.remove(b)
        for i in tree.findall('.//interface'):
            b = i.find('boot')
            if b is not None:
                i.remove(b)

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

        for idx, dev in enumerate(devices, start=1):
            apply_order(dev, idx)

        conn.defineXML(ET.tostring(tree).decode())
        return jsonify({'success': True})

    except libvirt.libvirtError as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Network interfaces
# ---------------------------------------------------------------------------

@api_bp.route('/vms/<uuid>/interfaces', methods=['POST'])
def add_interface(uuid):
    err = require_auth()
    if err:
        return err

    data = request.get_json() or {}
    mode = data.get('mode', 'nat')
    source = data.get('source', 'default')

    conn = get_db_connection()
    if not conn:
        return jsonify({'error': 'Could not connect to hypervisor'}), 500

    if not source:
        return jsonify({'error': 'source is required'}), 400

    try:
        dom = conn.lookupByUUIDString(uuid)
        # Build XML safely with ElementTree (prevents XML injection)
        if mode == 'bridge':
            iface_el = ET.Element('interface', type='bridge')
            ET.SubElement(iface_el, 'source', bridge=source)
        else:
            iface_el = ET.Element('interface', type='network')
            ET.SubElement(iface_el, 'source', network=source)
        ET.SubElement(iface_el, 'model', type='virtio')
        xml = ET.tostring(iface_el).decode()
        flags = libvirt.VIR_DOMAIN_AFFECT_CONFIG
        if dom.isActive():
            flags |= libvirt.VIR_DOMAIN_AFFECT_LIVE
        dom.attachDeviceFlags(xml, flags)
        return jsonify({'success': True})

    except libvirt.libvirtError as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@api_bp.route('/vms/<uuid>/interfaces', methods=['DELETE'])
def delete_interface(uuid):
    err = require_auth()
    if err:
        return err

    data = request.get_json() or {}
    mac = data.get('mac', '')
    if not mac or not MAC_RE.match(mac):
        return jsonify({'error': 'Valid MAC address is required'}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({'error': 'Could not connect to hypervisor'}), 500

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
            if dom.isActive():
                flags |= libvirt.VIR_DOMAIN_AFFECT_LIVE
            dom.detachDeviceFlags(iface_xml, flags)
        return jsonify({'success': True})

    except libvirt.libvirtError as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# PCI Passthrough devices
# ---------------------------------------------------------------------------

@api_bp.route('/vms/<uuid>/devices', methods=['POST'])
def attach_device(uuid):
    err = require_auth()
    if err:
        return err

    data = request.get_json() or {}
    pci_id = data.get('pci_id', '')
    if not pci_id or not PCI_ID_RE.match(pci_id):
        return jsonify({'error': 'Invalid PCI ID format'}), 400

    # Validate against known host devices to prevent arbitrary PCI manipulation
    available = {d['pci_id'] for d in get_host_devices()}
    if pci_id not in available:
        return jsonify({'error': 'Device not found on host'}), 400

    bus, slot, function = parse_pci_id(pci_id)
    # Build XML safely with ElementTree
    hostdev_el = ET.Element('hostdev', mode='subsystem', type='pci', managed='yes')
    src = ET.SubElement(hostdev_el, 'source')
    ET.SubElement(src, 'address', domain='0x0000', bus=bus, slot=slot, function=function)
    xml = ET.tostring(hostdev_el).decode()

    conn = get_db_connection()
    if not conn:
        return jsonify({'error': 'Could not connect to hypervisor'}), 500

    try:
        dom = conn.lookupByUUIDString(uuid)
        flags = libvirt.VIR_DOMAIN_AFFECT_CONFIG
        if dom.isActive():
            flags |= libvirt.VIR_DOMAIN_AFFECT_LIVE
        dom.attachDeviceFlags(xml, flags)
        return jsonify({'success': True})

    except libvirt.libvirtError as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@api_bp.route('/vms/<uuid>/devices', methods=['DELETE'])
def detach_device(uuid):
    err = require_auth()
    if err:
        return err

    data = request.get_json() or {}
    pci_id = data.get('pci_id')
    if not pci_id:
        return jsonify({'error': 'pci_id is required'}), 400

    bus, slot, function = parse_pci_id(pci_id)
    xml = f"""<hostdev mode='subsystem' type='pci' managed='yes'>
  <source>
    <address domain='0x0000' bus='{bus}' slot='{slot}' function='{function}'/>
  </source>
</hostdev>"""

    conn = get_db_connection()
    if not conn:
        return jsonify({'error': 'Could not connect to hypervisor'}), 500

    try:
        dom = conn.lookupByUUIDString(uuid)
        flags = libvirt.VIR_DOMAIN_AFFECT_CONFIG
        if dom.isActive():
            flags |= libvirt.VIR_DOMAIN_AFFECT_LIVE
        dom.detachDeviceFlags(xml, flags)
        return jsonify({'success': True})

    except libvirt.libvirtError as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------

@api_bp.route('/vms/<uuid>/snapshots', methods=['POST'])
def create_snapshot(uuid):
    err = require_auth()
    if err:
        return err

    data = request.get_json() or {}
    snapshot_name = data.get('snapshot_name')
    if not snapshot_name:
        return jsonify({'error': 'snapshot_name is required'}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({'error': 'Could not connect to hypervisor'}), 500

    try:
        dom = conn.lookupByUUIDString(uuid)
        # Build XML safely with ElementTree (prevents XML injection)
        snap_el = ET.Element('domainsnapshot')
        ET.SubElement(snap_el, 'name').text = snapshot_name
        xml = ET.tostring(snap_el).decode()
        dom.snapshotCreateXML(xml, 0)
        return jsonify({'success': True})

    except libvirt.libvirtError as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@api_bp.route('/vms/<uuid>/snapshots/<name>/revert', methods=['POST'])
def revert_snapshot(uuid, name):
    err = require_auth()
    if err:
        return err

    conn = get_db_connection()
    if not conn:
        return jsonify({'error': 'Could not connect to hypervisor'}), 500

    try:
        dom = conn.lookupByUUIDString(uuid)
        snapshot = dom.snapshotLookupByName(name, 0)
        dom.revertToSnapshot(snapshot, 0)
        return jsonify({'success': True})

    except libvirt.libvirtError as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@api_bp.route('/vms/<uuid>/snapshots/<name>', methods=['DELETE'])
def delete_snapshot(uuid, name):
    err = require_auth()
    if err:
        return err

    conn = get_db_connection()
    if not conn:
        return jsonify({'error': 'Could not connect to hypervisor'}), 500

    try:
        dom = conn.lookupByUUIDString(uuid)
        snapshot = dom.snapshotLookupByName(name, 0)
        snapshot.delete(0)
        return jsonify({'success': True})

    except libvirt.libvirtError as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# VM Stats
# ---------------------------------------------------------------------------

@api_bp.route('/vms/<uuid>/stats', methods=['GET'])
def vm_stats(uuid):
    err = require_auth()
    if err:
        return err

    conn = get_db_connection()
    if not conn:
        return jsonify({'error': 'Could not connect to hypervisor'}), 500

    try:
        dom = conn.lookupByUUIDString(uuid)
        if not dom.isActive():
            return jsonify({'error': 'VM is not running'}), 400

        xml_str = dom.XMLDesc(0)
        tree = ET.fromstring(xml_str)

        disk_target = None
        for disk in tree.findall('devices/disk'):
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

        t1 = time.time()
        c1 = dom.info()[4]
        time.sleep(1)
        t2 = time.time()
        c2 = dom.info()[4]
        cpu_usage = (c2 - c1) * 100 / ((t2 - t1) * dom.info()[3] * 1e9)

        mem_stats = dom.memoryStats()
        mem_used = mem_stats.get('actual', 0) / 1024

        disk_read_bytes, disk_write_bytes = 0, 0
        if disk_target:
            try:
                disk_stats = dom.blockStats(disk_target)
                disk_read_bytes = disk_stats[1]
                disk_write_bytes = disk_stats[3]
            except libvirt.libvirtError:
                pass

        net_rx_bytes, net_tx_bytes = 0, 0
        if net_target:
            try:
                net_stats = dom.interfaceStats(net_target)
                net_rx_bytes = net_stats[0]
                net_tx_bytes = net_stats[4]
            except libvirt.libvirtError:
                pass

        return jsonify({
            'cpu_usage': round(cpu_usage, 2),
            'mem_used': round(mem_used, 2),
            'disk_read': disk_read_bytes,
            'disk_write': disk_write_bytes,
            'net_rx': net_rx_bytes,
            'net_tx': net_tx_bytes,
        })

    except libvirt.libvirtError as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

@api_bp.route('/storage', methods=['GET'])
def list_storage():
    err = require_auth()
    if err:
        return err

    files = []
    if os.path.exists(STORAGE_PATH):
        try:
            dir_list = sorted(os.listdir(STORAGE_PATH))
            for filename in dir_list:
                full_path = os.path.join(STORAGE_PATH, filename)
                if os.path.isfile(full_path):
                    stats = os.stat(full_path)
                    ext = filename.split('.')[-1].lower() if '.' in filename else 'raw'
                    files.append({
                        'name': filename,
                        'path': full_path,
                        'size': get_human_readable_size(stats.st_size),
                        'type': ext
                    })
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    return jsonify({'files': files, 'storage_path': STORAGE_PATH})


@api_bp.route('/storage/disks', methods=['POST'])
def create_disk():
    err = require_auth()
    if err:
        return err

    data = request.get_json() or {}
    name = str(data.get('name', '')).strip()
    fmt = str(data.get('format', 'qcow2')).strip()

    if not name:
        return jsonify({'error': 'name is required'}), 400
    if fmt not in ALLOWED_DISK_FORMATS:
        return jsonify({'error': f'format must be one of: {", ".join(ALLOWED_DISK_FORMATS)}'}), 400
    try:
        size_gb = int(data.get('size', 0))
        if size_gb < 1 or size_gb > 10000:
            return jsonify({'error': 'size must be between 1 and 10000 GB'}), 400
    except (ValueError, TypeError):
        return jsonify({'error': 'size must be a number'}), 400

    safe_name = secure_filename(os.path.basename(name))
    if not safe_name:
        return jsonify({'error': 'Invalid disk name'}), 400
    if not safe_name.endswith(f'.{fmt}'):
        safe_name += f'.{fmt}'

    full_path = os.path.join(STORAGE_PATH, safe_name)
    if os.path.exists(full_path):
        return jsonify({'error': 'File already exists'}), 409

    try:
        subprocess.run(['qemu-img', 'create', '-f', fmt, full_path, f'{size_gb}G'],
                       check=True, capture_output=True)
        return jsonify({'success': True})
    except subprocess.CalledProcessError as e:
        current_app.logger.error(f"qemu-img failed: {e.stderr}")
        return jsonify({'error': 'Failed to create disk image'}), 500


@api_bp.route('/storage/upload', methods=['POST'])
def upload_file():
    err = require_auth()
    if err:
        return err

    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400

    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        save_path = os.path.join(STORAGE_PATH, filename)
        try:
            file.save(save_path)
            return jsonify({'success': True})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    return jsonify({'error': 'Invalid file type. Only .iso, .img, .qcow2 allowed.'}), 400


@api_bp.route('/storage/files', methods=['DELETE'])
def delete_file():
    err = require_auth()
    if err:
        return err

    data = request.get_json() or {}
    filename = data.get('filename')
    if not filename:
        return jsonify({'error': 'filename is required'}), 400

    safe_name = secure_filename(os.path.basename(filename))
    full_path = os.path.join(STORAGE_PATH, safe_name)

    if os.path.exists(full_path):
        os.remove(full_path)

    return jsonify({'success': True})


@api_bp.route('/cloud-images', methods=['GET'])
def list_cloud_images():
    """Return image files from the cloud-images directory only."""
    err = require_auth()
    if err:
        return err

    files = []
    os.makedirs(CLOUD_IMAGES_PATH, exist_ok=True)
    try:
        for filename in sorted(os.listdir(CLOUD_IMAGES_PATH)):
            full_path = os.path.join(CLOUD_IMAGES_PATH, filename)
            if os.path.isfile(full_path):
                ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else 'raw'
                stats = os.stat(full_path)
                files.append({
                    'name': filename,
                    'path': full_path,
                    'size': get_human_readable_size(stats.st_size),
                    'type': ext,
                })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    return jsonify({'files': files})


@api_bp.route('/storage/images', methods=['GET'])
def list_storage_images():
    """Return just the list of image files — used for disk autosuggest."""
    err = require_auth()
    if err:
        return err

    files = []
    if os.path.exists(STORAGE_PATH):
        try:
            for filename in sorted(os.listdir(STORAGE_PATH)):
                full_path = os.path.join(STORAGE_PATH, filename)
                if os.path.isfile(full_path):
                    ext = filename.split('.')[-1].lower() if '.' in filename else 'raw'
                    stats = os.stat(full_path)
                    files.append({
                        'name': filename,
                        'path': full_path,
                        'size': get_human_readable_size(stats.st_size),
                        'type': ext,
                    })
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    return jsonify({'files': files})


@api_bp.route('/networks', methods=['GET'])
def list_networks():
    """Return libvirt virtual networks and host bridge interfaces."""
    err = require_auth()
    if err:
        return err

    networks = []
    bridges = []

    conn = get_db_connection()
    if conn:
        try:
            for net in conn.listAllNetworks(0):
                networks.append({
                    'name': net.name(),
                    'active': net.isActive() == 1,
                    'autostart': net.autostart() == 1,
                })
        except libvirt.libvirtError:
            pass
        finally:
            conn.close()

    # Detect host bridge interfaces from /sys/class/net
    try:
        for iface in sorted(os.listdir('/sys/class/net')):
            bridge_path = f'/sys/class/net/{iface}/bridge'
            if os.path.isdir(bridge_path):
                bridges.append(iface)
    except Exception:
        pass

    return jsonify({'networks': networks, 'bridges': bridges})


@api_bp.route('/vms/<uuid>/disks/create', methods=['POST'])
def create_and_attach_disk(uuid):
    """Create a new qcow2 image and immediately attach it to the VM."""
    err = require_auth()
    if err:
        return err

    data = request.get_json() or {}
    name = data.get('name', '').strip()
    size_gb = data.get('size')
    fmt = data.get('format', 'qcow2')

    if not name or not size_gb:
        return jsonify({'error': 'name and size are required'}), 400

    safe_name = secure_filename(name)
    if not safe_name.endswith(f'.{fmt}'):
        safe_name += f'.{fmt}'

    full_path = os.path.join(STORAGE_PATH, safe_name)
    if os.path.exists(full_path):
        return jsonify({'error': f'File already exists: {full_path}'}), 409

    # Create the disk image
    try:
        subprocess.run(
            ['qemu-img', 'create', '-f', fmt, full_path, f'{size_gb}G'],
            check=True, capture_output=True
        )
    except subprocess.CalledProcessError as e:
        return jsonify({'error': f'qemu-img failed: {e.stderr.decode()}'}), 500

    # Attach it to the VM
    conn = get_db_connection()
    if not conn:
        return jsonify({'error': 'Could not connect to hypervisor'}), 500

    try:
        dom = conn.lookupByUUIDString(uuid)
        xml_str = dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
        tree = ET.fromstring(xml_str)

        existing_devs = {d.find('target').get('dev') for d in tree.findall('devices/disk') if d.find('target') is not None}

        prefix = 'sd' if fmt == 'raw' else 'vd'
        target_dev = next(
            (f"{prefix}{c}" for c in 'abcdefghijklmnopqrstuvwxyz' if f"{prefix}{c}" not in existing_devs),
            None
        )
        if not target_dev:
            return jsonify({'error': 'No available device names'}), 500

        driver_type = 'raw' if fmt == 'raw' else 'qcow2'
        disk_xml = f"""
        <disk type='file' device='disk'>
          <driver name='qemu' type='{driver_type}'/>
          <source file='{full_path}'/>
          <target dev='{target_dev}' bus='virtio'/>
        </disk>
        """
        flags = libvirt.VIR_DOMAIN_AFFECT_CONFIG
        if dom.isActive():
            flags |= libvirt.VIR_DOMAIN_AFFECT_LIVE
        dom.attachDeviceFlags(disk_xml, flags)
        return jsonify({'success': True, 'path': full_path, 'target': target_dev})

    except libvirt.libvirtError as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@api_bp.route('/dashboard', methods=['GET'])
def dashboard():
    err = require_auth()
    if err:
        return err

    uptime = datetime.datetime.now() - datetime.datetime.fromtimestamp(psutil.boot_time())
    days = uptime.days
    hours, remainder = divmod(uptime.seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    uptime_str = f"{days}d {hours}h {minutes}m"

    cpu_percent = psutil.cpu_percent(interval=0.5)
    cpu_load = psutil.getloadavg()

    mem = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    net = psutil.net_io_counters()

    processes = []
    for proc in sorted(
        psutil.process_iter(['pid', 'name', 'username', 'cpu_percent', 'memory_percent']),
        key=lambda p: p.info['cpu_percent'] or 0,
        reverse=True
    )[:10]:
        processes.append(proc.info)

    return jsonify({
        'uptime_str': uptime_str,
        'cpu_percent': cpu_percent,
        'load_avg': [round(cpu_load[0], 2), round(cpu_load[1], 2), round(cpu_load[2], 2)],
        'mem': {
            'total_gb': round(mem.total / (1024 ** 3), 2),
            'used_gb': round(mem.used / (1024 ** 3), 2),
            'percent': mem.percent,
        },
        'disk': {
            'total_gb': round(disk.total / (1024 ** 3), 2),
            'used_gb': round(disk.used / (1024 ** 3), 2),
            'percent': disk.percent,
        },
        'net': {
            'bytes_sent': net.bytes_sent,
            'bytes_recv': net.bytes_recv,
        },
        'processes': processes,
    })
