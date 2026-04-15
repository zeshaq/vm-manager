import fcntl
import json
import os
import pty
import select
import struct
import termios
import gevent
from flask import Blueprint, render_template, session, request, current_app

try:
    from geventwebsocket.exceptions import WebSocketError
except ImportError:
    WebSocketError = Exception

host_terminal_bp = Blueprint('host_terminal', __name__)


def _set_pty_size(fd, cols, rows):
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ,
                    struct.pack('HHHH', rows, cols, 0, 0))
    except OSError:
        pass


@host_terminal_bp.route('/host-terminal')
def terminal():
    hostname = os.uname().nodename
    return render_template('host_terminal.html', hostname=hostname)


@host_terminal_bp.route('/host-ws')
def host_ws():
    ws = request.environ.get('wsgi.websocket')
    if not ws:
        return 'WebSocket required', 400

    if 'username' not in session:
        ws.close()
        return ''

    current_app.logger.info(f"Host terminal opened by {session['username']}")

    pid, fd = pty.fork()
    if pid == 0:
        os.execvp('/bin/bash', ['/bin/bash', '-l'])
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
                        if msg.get('type') == 'resize':
                            _set_pty_size(fd, int(msg.get('cols', 80)), int(msg.get('rows', 24)))
                            continue
                    except (ValueError, KeyError):
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
