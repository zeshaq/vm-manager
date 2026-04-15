"""VM console — serial console via virsh + xterm.js, VNC proxy for graphical."""

import fcntl
import libvirt
import os
import pty
import select
import socket
import struct
import termios
import xml.etree.ElementTree as ET
import json
import gevent
from flask import Blueprint, render_template, request, session, current_app

try:
    from geventwebsocket.exceptions import WebSocketError
except ImportError:
    WebSocketError = Exception

terminal_bp = Blueprint('terminal', __name__)


def _set_pty_size(fd, cols, rows):
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ,
                    struct.pack('HHHH', rows, cols, 0, 0))
    except OSError:
        pass


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
    return render_template('vm_terminal.html', vm_name=vm_name)


@terminal_bp.route('/vm-console-ws')
def vm_console_ws():
    ws = request.environ.get('wsgi.websocket')
    if not ws:
        return 'WebSocket required', 400

    if 'username' not in session:
        ws.close()
        return ''

    vm_name = request.args.get('vm_name', '').strip()
    if not vm_name:
        ws.close()
        return ''

    current_app.logger.info(f"VM console opened for {vm_name} by {session['username']}")

    # Check if VM has a serial/console device; add one if missing
    try:
        conn = libvirt.open('qemu:///system')
        dom = conn.lookupByName(vm_name)
        tree = ET.fromstring(dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE))
        has_console = (tree.find('devices/console') is not None or
                       tree.find('devices/serial') is not None)
        if not has_console:
            devices = tree.find('devices')
            serial_el = ET.SubElement(devices, 'serial', {'type': 'pty'})
            ET.SubElement(serial_el, 'target', {'port': '0'})
            console_el = ET.SubElement(devices, 'console', {'type': 'pty'})
            ET.SubElement(console_el, 'target', {'type': 'serial', 'port': '0'})
            conn.defineXML(ET.tostring(tree).decode())
            ws.send('\r\n\x1b[33mSerial console device added to this VM.\x1b[0m\r\n')
            ws.send('\x1b[33mRestart the VM once for the console to become active.\x1b[0m\r\n\r\n')
        conn.close()
    except libvirt.libvirtError as e:
        ws.send(f'\r\n\x1b[31mError: {e}\x1b[0m\r\n')
        ws.close()
        return ''

    pid, fd = pty.fork()
    if pid == 0:
        os.execvp('virsh', ['virsh', '-c', 'qemu:///system', 'console', vm_name])
        os._exit(1)

    _set_pty_size(fd, 220, 50)

    def pty_to_ws():
        try:
            while True:
                r, _, _ = select.select([fd], [], [], 1)
                if r:
                    try:
                        data = os.read(fd, 4096)
                    except OSError:
                        break
                    if not data:
                        break
                    ws.send(data.decode(errors='replace'))
        except (WebSocketError, OSError):
            pass
        finally:
            try: ws.close()
            except Exception: pass

    def ws_to_pty():
        try:
            while True:
                data = ws.receive()
                if data is None:
                    break
                if isinstance(data, str):
                    try:
                        msg = json.loads(data)
                        if isinstance(msg, dict) and msg.get('type') == 'resize':
                            _set_pty_size(fd, int(msg.get('cols', 80)), int(msg.get('rows', 24)))
                            continue
                    except (ValueError, KeyError, TypeError):
                        pass
                    os.write(fd, data.encode())
                else:
                    os.write(fd, data)
        except (WebSocketError, OSError):
            pass

    g1 = gevent.spawn(pty_to_ws)
    g2 = gevent.spawn(ws_to_pty)
    gevent.joinall([g1, g2])

    try: os.close(fd)
    except OSError: pass
    try: os.waitpid(pid, 0)
    except OSError: pass

    return ''


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
