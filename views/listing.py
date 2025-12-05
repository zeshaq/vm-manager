import libvirt
from flask import Blueprint, render_template

listing_bp = Blueprint('listing', __name__)

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
    conn = None
    try:
        conn = libvirt.openReadOnly('qemu:///system')
        if conn:
            domains = conn.listAllDomains(0)
            for domain in domains:
                info = domain.info()
                vm_data = {
                    'id': domain.ID(),
                    'name': domain.name(),
                    'state': get_vm_state_string(info[0]),
                    'state_code': info[0],
                    'memory_mb': int(info[1] / 1024),
                    'vcpus': info[3]
                }
                vms_list.append(vm_data)
    except libvirt.libvirtError as e:
        print(f"Libvirt Error: {e}")
    finally:
        if conn:
            conn.close()
            
    return render_template('list.html', vms=vms_list)