import os
import re
import pty
import select
from threading import Thread
from flask import Blueprint, render_template, session, request, current_app
from flask_sock import ConnectionClosed
from sockets import sock

docker_exec_bp = Blueprint('docker_exec', __name__)

# Only allow safe container IDs / names
_CTR_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.\-]{0,127}$')


@docker_exec_bp.route('/docker-exec')
def docker_exec_page():
    """Serve the xterm.js-based exec terminal page."""
    container_id = request.args.get('container_id', '')
    container_name = request.args.get('name', container_id)
    if not container_id or not _CTR_RE.match(container_id):
        return "Invalid container id", 400
    return render_template('docker_exec.html',
                           container_id=container_id,
                           container_name=container_name)


@sock.route('/docker-exec-ws')
def docker_exec_ws(ws):
    """WebSocket proxy: browser ↔ docker exec shell."""
    if 'username' not in session:
        ws.close(reason=1008, message="Unauthorized")
        return

    container_id = request.args.get('container_id', '')
    if not container_id or not _CTR_RE.match(container_id):
        ws.close(reason=1008, message="Invalid container_id")
        return

    current_app.logger.info(
        f"Docker exec opened for {container_id} by {session['username']}"
    )

    # Try bash, fall back to sh
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp('docker', ['docker', 'exec', '-it', container_id, '/bin/bash'])
        # If bash not found, try sh
        os.execvp('docker', ['docker', 'exec', '-it', container_id, '/bin/sh'])
        os._exit(1)

    def pty_to_ws():
        try:
            while True:
                r, _, _ = select.select([fd], [], [], 1)
                if r:
                    data = os.read(fd, 2048)
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
        try:
            while True:
                data = ws.receive()
                if data is None:
                    break
                if isinstance(data, str):
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

    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.waitpid(pid, 0)
    except OSError:
        pass
