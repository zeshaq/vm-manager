"""
noVNC WebSocket proxy.

Flow:
  Browser (noVNC JS)  ←WebSocket→  /ws/vnc/<uuid>  ←TCP→  VM VNC server

libvirt assigns each VM a VNC port (5900+display).  We read it from
the running domain's XML and proxy raw bytes in both directions using
gevent greenlets so the single worker thread is never blocked.
"""

import socket as _socket
import xml.etree.ElementTree as ET
from flask import Blueprint, jsonify, request, session

try:
    import libvirt as _libvirt
    _LIBVIRT = True
except ImportError:
    _LIBVIRT = False

from sockets import sock

console_bp = Blueprint('console', __name__)


def _auth():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    return None


def _get_vnc_port(vm_uuid: str) -> int | None:
    """Return the live VNC TCP port for a running VM, or None."""
    if not _LIBVIRT:
        return None
    conn = _libvirt.open('qemu:///system')
    try:
        dom  = conn.lookupByUUIDString(vm_uuid)
        xml  = dom.XMLDesc()          # live XML — includes actual port
        root = ET.fromstring(xml)
        g    = root.find('.//graphics[@type="vnc"]')
        if g is not None:
            port = int(g.get('port', -1))
            return port if port > 0 else None
    except _libvirt.libvirtError:
        return None
    finally:
        conn.close()


# ── REST: VNC info ────────────────────────────────────────────────────────────

@console_bp.route('/api/console/<vm_uuid>/info')
def vnc_info(vm_uuid):
    err = _auth()
    if err:
        return err
    port = _get_vnc_port(vm_uuid)
    if port is None:
        return jsonify({'error': 'VNC not available — VM may be stopped or VNC not configured'}), 404
    return jsonify({'vnc_port': port, 'ws_path': f'/ws/vnc/{vm_uuid}'})


# ── WebSocket proxy ───────────────────────────────────────────────────────────

@sock.route('/ws/vnc/<vm_uuid>')
def vnc_proxy(ws, vm_uuid):
    """Bidirectional WebSocket ↔ TCP proxy to the VM's VNC port."""
    # Session auth — flask session is available inside flask-sock handlers
    if 'username' not in session:
        ws.close(1008, 'Unauthorized')
        return

    port = _get_vnc_port(vm_uuid)
    if port is None:
        ws.close(1011, 'VNC unavailable')
        return

    try:
        vnc = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        vnc.connect(('127.0.0.1', port))
    except OSError as e:
        ws.close(1011, str(e))
        return

    import gevent

    def _ws_to_vnc():
        try:
            while True:
                data = ws.receive()
                if data is None:
                    break
                if isinstance(data, str):
                    data = data.encode('latin-1')
                vnc.sendall(data)
        except Exception:
            pass
        finally:
            vnc.close()

    def _vnc_to_ws():
        try:
            while True:
                chunk = vnc.recv(65536)
                if not chunk:
                    break
                ws.send(chunk)
        except Exception:
            pass

    g1 = gevent.spawn(_ws_to_vnc)
    g2 = gevent.spawn(_vnc_to_ws)
    gevent.joinall([g1, g2])
