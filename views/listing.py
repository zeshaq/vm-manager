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
    # (This ensures it works whether you are on localhost or a remote PC)
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