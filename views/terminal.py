"""Legacy VNC terminal — proxies by VM name (used by /terminal?vm_name=...)."""

import libvirt
import xml.etree.ElementTree as ET
import socket
import gevent
from flask import Blueprint, render_template, request, session

try:
    from geventwebsocket.exceptions import WebSocketError
except ImportError:
    WebSocketError = Exception

terminal_bp = Blueprint('terminal', __name__)


def get_vnc_port(vm_name):
    conn = libvirt.open('qemu:///system')
    if conn is None:
        return None
    try:
        domain = conn.lookupByName(vm_name)
        if domain is None:
            return None
        root = ET.fromstring(domain.XMLDesc(0))
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


@terminal_bp.route('/vnc')
def vnc(ws=None):
    ws = request.environ.get('wsgi.websocket')
    if not ws:
        return 'WebSocket required', 400

    if 'username' not in session:
        ws.close()
        return ''

    vm_name = request.args.get('vm_name')
    if not vm_name:
        ws.close()
        return ''

    vnc_port = get_vnc_port(vm_name)
    if not vnc_port:
        ws.close()
        return ''

    try:
        target = socket.create_connection(('localhost', vnc_port))
    except OSError:
        ws.close()
        return ''

    def client_to_server():
        try:
            while True:
                data = ws.receive()
                if data is None:
                    break
                if isinstance(data, str):
                    data = data.encode('latin-1')
                target.sendall(data)
        except (WebSocketError, OSError):
            pass
        finally:
            try: target.close()
            except Exception: pass

    def server_to_client():
        try:
            while True:
                data = target.recv(4096)
                if not data:
                    break
                ws.send(data)
        except (WebSocketError, OSError):
            pass

    g1 = gevent.spawn(client_to_server)
    g2 = gevent.spawn(server_to_client)
    gevent.joinall([g1, g2])

    return ''
