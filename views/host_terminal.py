import fcntl
import json
import os
import pty
import select
import struct
import termios
from threading import Thread
from flask import Blueprint, render_template, session, request, current_app
from flask_sock import ConnectionClosed
from sockets import sock

host_terminal_bp = Blueprint('host_terminal', __name__)


def _set_pty_size(fd, cols, rows):
    """Resize the PTY to match the client terminal dimensions."""
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ,
                    struct.pack('HHHH', rows, cols, 0, 0))
    except OSError:
        pass


@host_terminal_bp.route('/host-terminal')
def terminal():
    hostname = os.uname().nodename
    return render_template('host_terminal.html', hostname=hostname)


@sock.route('/host-ws')
def host_ws(ws):
    # Auth check — host terminal gives full shell access
    if 'username' not in session:
        ws.close(reason=1008, message="Unauthorized")
        return
    current_app.logger.info(f"Host terminal opened by {session['username']}")

    pid, fd = pty.fork()

    if pid == 0:
        # Child: replace this process with bash
        os.execvp('/bin/bash', ['/bin/bash', '-l'])
        os._exit(1)   # only reached if exec fails

    # Default window size
    _set_pty_size(fd, 220, 50)

    def pty_to_ws():
        """Read PTY output → send to browser."""
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
        except (ConnectionClosed, OSError):
            pass
        finally:
            try:
                ws.close()
            except Exception:
                pass

    def ws_to_pty():
        """Receive from browser → write to PTY (or handle resize)."""
        try:
            while True:
                data = ws.receive()
                if data is None:
                    break
                # Check for resize control message
                if isinstance(data, str):
                    try:
                        msg = json.loads(data)
                        if msg.get('type') == 'resize':
                            cols = int(msg.get('cols', 80))
                            rows = int(msg.get('rows', 24))
                            _set_pty_size(fd, cols, rows)
                            continue
                    except (ValueError, KeyError):
                        pass
                    os.write(fd, data.encode())
                else:
                    os.write(fd, data)
        except (ConnectionClosed, OSError):
            pass

    t1 = Thread(target=pty_to_ws, daemon=True)
    t2 = Thread(target=ws_to_pty, daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Clean up child process
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.waitpid(pid, 0)
    except OSError:
        pass
