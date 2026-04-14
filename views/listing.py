import libvirt
import xml.etree.ElementTree as ET
from flask import Blueprint, Response, request

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


def parse_pci_id(pci_id):
    """Parse a PCI id string like '0000:8a:00.0' into hex bus/slot/function components."""
    parts = pci_id.split(':')
    bus = f"0x{parts[1]}"
    slot_func = parts[2].split('.')
    slot = f"0x{slot_func[0]}"
    function = f"0x{slot_func[1]}"
    return bus, slot, function


def get_host_devices():
    """Scans host for all PCI devices using Libvirt NodeDevice API"""
    devices = []
    conn = get_db_connection()
    if not conn:
        return []

    try:
        device_names = conn.listNodeDevices('pci', 0)

        for name in device_names:
            try:
                dev = conn.nodeDeviceLookupByName(name)
                xml_str = dev.XMLDesc()
                tree = ET.fromstring(xml_str)

                pci_cap = tree.find(".//capability[@type='pci']")
                if pci_cap is None:
                    continue

                iommu_group = None
                iommu_tag = pci_cap.find("iommuGroup/number")
                if iommu_tag is not None:
                    iommu_group = iommu_tag.text

                product_tag = tree.find(".//product")
                vendor_tag = tree.find(".//vendor")
                product_name = product_tag.text if product_tag is not None and product_tag.text else f"PCI Device ({name})"
                vendor_name = vendor_tag.text if vendor_tag is not None and vendor_tag.text else "Unknown Vendor"

                address_tag = pci_cap.find("address")
                if address_tag is None:
                    continue

                domain = f"{int(address_tag.get('domain')):04x}"
                bus = f"{int(address_tag.get('bus')):02x}"
                slot = f"{int(address_tag.get('slot')):02x}"
                function = f"{int(address_tag.get('function')):x}"
                pci_str = f"{domain}:{bus}:{slot}.{function}"

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

            except Exception as e:
                print(f"[Device Scan] Parse Error for {name}: {e}")

    except Exception as e:
        print(f"[Device Scan] Error: {e}")
    finally:
        if conn:
            conn.close()

    devices.sort(key=lambda x: (x['iommu_group'] or 'zzz', x['pci_id']))
    return devices


# Legacy virt-viewer .vv file download (kept for desktop virt-viewer users)
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
        except libvirt.libvirtError:
            pass
        finally:
            conn.close()
    if not port or port == '-1':
        return "<h1>VM Not Running</h1><p>Start the VM first.</p>", 409
    host_ip = request.host.split(':')[0]
    vv_content = (
        f"[virt-viewer]\ntype=vnc\nhost={host_ip}\nport={port}\n"
        f"delete-this-file=1\ntitle=Console-{uuid}\n"
    )
    return Response(
        vv_content,
        mimetype="application/x-virt-viewer",
        headers={"Content-Disposition": f"attachment; filename=console-{uuid}.vv"},
    )
