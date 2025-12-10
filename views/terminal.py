import libvirt
import xml.etree.ElementTree as ET
import socket
from flask import Blueprint, render_template, request
from flask_sock import ConnectionClosed

# Import the sock object from the main app
from sockets import sock

terminal_bp = Blueprint('terminal', __name__)

def get_vnc_port(vm_name):
    conn = libvirt.open('qemu:///system')
    if conn is None:
        return None
    try:
        domain = conn.lookupByName(vm_name)
        if domain is None:
            return None
        xml_desc = domain.XMLDesc(0)
        root = ET.fromstring(xml_desc)
        graphics = root.find('./devices/graphics[@type="vnc"]')
        if graphics is not None:
            port = graphics.get('port')
            if port:
                return int(port)
    finally:
        conn.close()
    return None

@terminal_bp.route('/terminal')
def terminal():
    vm_name = request.args.get('vm_name')
    if not vm_name:
        return "Missing vm_name parameter", 400
    return render_template('novnc.html', vm_name=vm_name)

@sock.route('/vnc')
def vnc(ws):
    vm_name = request.args.get('vm_name')
    if not vm_name:
        ws.close(reason=1008, message="Missing vm_name parameter")
        return

    vnc_port = get_vnc_port(vm_name)
    if not vnc_port:
        ws.close(reason=1011, message=f"Could not get VNC port for VM {vm_name}")
        return
    
    # Connect to the VNC server
    try:
        with socket.create_connection(('localhost', vnc_port)) as target:
            while True:
                client_data = ws.receive(timeout=1)
                if client_data:
                    target.sendall(client_data)
                
                server_data = target.recv(4096)
                if server_data:
                    ws.send(server_data)

    except ConnectionClosed:
        print(f"WebSocket connection closed for {vm_name}")
    except Exception as e:
        print(f"Error proxying VNC for {vm_name}: {e}")
    finally:
        if not ws.closed:
            ws.close()
