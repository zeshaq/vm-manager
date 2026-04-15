"""
noVNC WebSocket proxy — geventwebsocket edition.

With GeventWebSocketWorker, WebSocket upgrades are handled by the worker
before the WSGI app runs. The WebSocket object is in environ['wsgi.websocket'].
"""

import socket as _socket
import xml.etree.ElementTree as ET
from flask import Blueprint, jsonify, request, session

try:
    import libvirt as _libvirt
    _LIBVIRT = True
except ImportError:
    _LIBVIRT = False

try:
    from geventwebsocket.exceptions import WebSocketError
    import gevent
except ImportError:
    WebSocketError = Exception
    gevent = None

console_bp = Blueprint('console', __name__)


def _auth():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    return None


def _get_vnc_port(vm_uuid: str):
    """Return the live VNC TCP port for a running VM, or None."""
    if not _LIBVIRT:
        return None
    conn = _libvirt.open('qemu:///system')
    try:
        dom  = conn.lookupByUUIDString(vm_uuid)
        xml  = dom.XMLDesc()
        root = ET.fromstring(xml)
        g    = root.find('.//graphics[@type="vnc"]')
        if g is not None:
            port = int(g.get('port', -1))
            return port if port > 0 else None
    except _libvirt.libvirtError:
        return None
    finally:
        conn.close()


@console_bp.route('/vnc-view/<vm_uuid>')
def vnc_view(vm_uuid):
    """Render the noVNC page for a VM identified by UUID."""
    from flask import render_template
    if 'username' not in session:
        return 'Unauthorized', 401
    if not _LIBVIRT:
        return 'libvirt not available', 500
    conn = _libvirt.open('qemu:///system')
    try:
        dom = conn.lookupByUUIDString(vm_uuid)
        vm_name = dom.name()
    except _libvirt.libvirtError:
        return 'VM not found', 404
    finally:
        conn.close()
    return render_template('novnc.html', vm_name=vm_name)


@console_bp.route('/api/console/<vm_uuid>/info')
def vnc_info(vm_uuid):
    err = _auth()
    if err:
        return err
    port = _get_vnc_port(vm_uuid)
    if port is None:
        return jsonify({'error': 'VNC not available'}), 404
    return jsonify({'vnc_port': port, 'ws_path': f'/ws/vnc/{vm_uuid}'})


@console_bp.route('/ws/vnc/<vm_uuid>')
def vnc_proxy(vm_uuid):
    """Bidirectional WebSocket ↔ TCP proxy to the VM's VNC port."""
    ws = request.environ.get('wsgi.websocket')
    if not ws:
        return 'WebSocket required', 400

    if 'username' not in session:
        ws.close()
        return ''

    port = _get_vnc_port(vm_uuid)
    if port is None:
        ws.close()
        return ''

    try:
        vnc = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        vnc.settimeout(10)
        vnc.connect(('127.0.0.1', port))
        vnc.settimeout(None)
    except OSError:
        ws.close()
        return ''

    def _ws_to_vnc():
        try:
            while True:
                data = ws.receive()
                if data is None:
                    break
                if isinstance(data, str):
                    data = data.encode('latin-1')
                vnc.sendall(data)
        except (WebSocketError, OSError):
            pass
        finally:
            try: vnc.close()
            except Exception: pass

    def _vnc_to_ws():
        try:
            while True:
                chunk = vnc.recv(65536)
                if not chunk:
                    break
                ws.send(chunk)
        except (WebSocketError, OSError):
            pass

    if gevent:
        g1 = gevent.spawn(_ws_to_vnc)
        g2 = gevent.spawn(_vnc_to_ws)
        gevent.joinall([g1, g2])
    else:
        from threading import Thread
        t1 = Thread(target=_ws_to_vnc, daemon=True)
        t2 = Thread(target=_vnc_to_ws, daemon=True)
        t1.start(); t2.start()
        t1.join(); t2.join()

    return ''
