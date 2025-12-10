import os
import pty
import select
import subprocess
from threading import Thread
from flask import Blueprint, render_template
from flask_sock import ConnectionClosed
from sockets import sock

host_terminal_bp = Blueprint('host_terminal', __name__)

@host_terminal_bp.route('/host-terminal')
def terminal():
    return render_template('host_terminal.html')

@sock.route('/host-ws')
def host_ws(ws):
    # Create a new process with a pseudo-terminal
    pid, fd = pty.fork()

    if pid == 0:
        # Child process: exec a shell
        subprocess.run('/bin/bash')
        os._exit(0)

    def pty_to_ws():
        """Reads from the pty and writes to the websocket."""
        try:
            while True:
                r, _, _ = select.select([fd], [], [], 1)
                if r:
                    data = os.read(fd, 1024)
                    if not data:
                        break
                    ws.send(data.decode(errors='ignore'))
        except (ConnectionClosed, OSError):
            pass
        finally:
            ws.close()

    def ws_to_pty():
        """Reads from the websocket and writes to the pty."""
        try:
            while True:
                data = ws.receive()
                if data is None:
                    break
                os.write(fd, data.encode())
        except (ConnectionClosed, OSError):
            pass

    # Start the two threads to proxy data
    pty_thread = Thread(target=pty_to_ws)
    ws_thread = Thread(target=ws_to_pty)

    pty_thread.start()
    ws_thread.start()

    pty_thread.join()
    ws_thread.join()

    # Clean up the child process
    try:
        os.close(fd)
        os.waitpid(pid, 0)
    except OSError:
        pass
