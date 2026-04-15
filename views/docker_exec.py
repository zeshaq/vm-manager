import os
import re
import pty
import select
import gevent
from flask import Blueprint, render_template, session, request, current_app

try:
    from geventwebsocket.exceptions import WebSocketError
except ImportError:
    WebSocketError = Exception

docker_exec_bp = Blueprint('docker_exec', __name__)

_CTR_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.\-]{0,127}$')


@docker_exec_bp.route('/docker-exec')
def docker_exec_page():
    container_id = request.args.get('container_id', '')
    container_name = request.args.get('name', container_id)
    if not container_id or not _CTR_RE.match(container_id):
        return "Invalid container id", 400
    return render_template('docker_exec.html',
                           container_id=container_id,
                           container_name=container_name)


@docker_exec_bp.route('/docker-exec-ws')
def docker_exec_ws():
    ws = request.environ.get('wsgi.websocket')
    if not ws:
        return 'WebSocket required', 400

    if 'username' not in session:
        ws.close()
        return ''

    container_id = request.args.get('container_id', '')
    if not container_id or not _CTR_RE.match(container_id):
        ws.close()
        return ''

    current_app.logger.info(f"Docker exec opened for {container_id} by {session['username']}")

    pid, fd = pty.fork()
    if pid == 0:
        os.execvp('docker', ['docker', 'exec', '-it', container_id, '/bin/bash'])
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
