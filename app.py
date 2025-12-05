import libvirt
from flask import Flask, render_template_string

app = Flask(__name__)

# Simple HTML template for displaying the list
HTML_TEMPLATE = """
<!doctype html>
<html>
<head>
    <title>KVM VM List</title>
    <style>
        body { font-family: sans-serif; padding: 20px; }
        table { border-collapse: collapse; width: 100%; max-width: 800px; margin-top: 20px; }
        th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
        th { background-color: #f2f2f2; }
        tr:nth-child(even) { background-color: #f9f9f9; }
        .status-1 { color: green; font-weight: bold; } /* Running */
        .status-5 { color: red; } /* Shutoff */
    </style>
</head>
<body>
    <h1>Local Hypervisor VM List</h1>
    <table>
        <thead>
            <tr>
                <th>ID</th>
                <th>Name</th>
                <th>State</th>
                <th>Memory (Max)</th>
                <th>vCPUs</th>
            </tr>
        </thead>
        <tbody>
            {% for vm in vms %}
            <tr>
                <td>{{ vm.id if vm.id != -1 else 'N/A' }}</td>
                <td>{{ vm.name }}</td>
                <td class="status-{{ vm.state_code }}">{{ vm.state }}</td>
                <td>{{ vm.memory_mb }} MB</td>
                <td>{{ vm.vcpus }}</td>
            </tr>
            {% endfor %}
        </tbody>
    </table>
</body>
</html>
"""

def get_vm_state_string(state_int):
    # Mapping libvirt state integers to readable strings
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
        # Connect to the local system hypervisor
        # qemu:///system gives access to system-wide VMs (usually requires root or libvirt group)
        conn = libvirt.openReadOnly('qemu:///system')
        
        if conn is None:
            print('Failed to open connection to qemu:///system')
            return []

        # listAllDomains(0) returns all domains (active and inactive)
        domains = conn.listAllDomains(0)
        
        for domain in domains:
            # getInfo() returns: [state, maxmem, mem, vcpus, cputime]
            info = domain.info()
            state_code = info[0]
            
            vm_data = {
                'id': domain.ID(),
                'name': domain.name(),
                'state': get_vm_state_string(state_code),
                'state_code': state_code,
                'memory_mb': int(info[1] / 1024), # convert KB to MB
                'vcpus': info[3]
            }
            vms_list.append(vm_data)
            
    except libvirt.libvirtError as e:
        print(f"Libvirt error: {e}")
        return []
    finally:
        if conn:
            conn.close()
            
    return vms_list

@app.route('/')
def list_vms():
    vms = get_vms()
    return render_template_string(HTML_TEMPLATE, vms=vms)

if __name__ == '__main__':
    # Run the app
    app.run(host='0.0.0.0', port=5000, debug=True)