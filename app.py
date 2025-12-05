import libvirt
from flask import Flask, render_template

app = Flask(__name__)

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

def get_vms():
    vms_list = []
    conn = None
    try:
        # Connect to local system hypervisor (requires root or libvirt group)
        conn = libvirt.openReadOnly('qemu:///system')
        if conn is None:
            return []

        domains = conn.listAllDomains(0)
        
        for domain in domains:
            info = domain.info()
            # info structure: [state, maxmem, mem, vcpus, cputime]
            
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
            
    return vms_list

@app.route('/')
def list_vms():
    vms = get_vms()
    # Flask automatically looks for 'index.html' inside the 'templates' folder
    return render_template('index.html', vms=vms)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)