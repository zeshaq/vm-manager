from flask import Flask, request, render_template, send_from_directory
import os
import psutil
import libvirt
from datetime import timedelta

# Import the blueprints
from views.listing import listing_bp
from views.creation import creation_bp
from views.storage import storage_bp
from views.terminal import terminal_bp
from views.host_terminal import host_terminal_bp
from views.dashboard import dashboard_bp
from views.api import api_bp, limiter
from views.docker_mgmt import docker_bp
from views.docker_exec import docker_exec_bp
from views.network_mgmt import network_bp
from views.metrics import metrics_bp
from views.files import files_bp
from views.kubernetes import k8s_bp
from views.images import images_bp
from views.console import console_bp
from views.system_mgmt import system_bp
from views.openshift import ocp_bp

from sockets import sock

app = Flask(__name__, static_folder='frontend/dist/assets', static_url_path='/assets')

# ── Secret key: env var → persisted file → generated once ────────────────
_key_file = os.path.join(os.path.dirname(__file__), '.secret_key')
if os.environ.get('SECRET_KEY'):
    app.secret_key = os.environ['SECRET_KEY'].encode()
elif os.path.exists(_key_file):
    with open(_key_file, 'rb') as _f:
        app.secret_key = _f.read()
else:
    app.secret_key = os.urandom(32)
    with open(_key_file, 'wb') as _f:
        _f.write(app.secret_key)

# ── Session security ──────────────────────────────────────────────────────
app.permanent_session_lifetime = timedelta(days=30)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=False,   # flip to True once HTTPS is set up
    MAX_CONTENT_LENGTH=500 * 1024 * 1024,  # 500 MB upload limit
)

sock.init_app(app)
limiter.init_app(app)

# ── Security response headers ─────────────────────────────────────────────
@app.after_request
def set_security_headers(response):
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Permissions-Policy'] = 'geolocation=(), microphone=(), camera=()'
    return response

# Register the blueprints
app.register_blueprint(listing_bp)
app.register_blueprint(creation_bp)
app.register_blueprint(storage_bp)
app.register_blueprint(terminal_bp)
app.register_blueprint(host_terminal_bp)
app.register_blueprint(dashboard_bp)
app.register_blueprint(api_bp)
app.register_blueprint(docker_bp)
app.register_blueprint(docker_exec_bp)
app.register_blueprint(network_bp)
app.register_blueprint(metrics_bp)
app.register_blueprint(files_bp)
app.register_blueprint(k8s_bp)
app.register_blueprint(images_bp)
app.register_blueprint(console_bp)
app.register_blueprint(system_bp)
app.register_blueprint(ocp_bp)

@app.route('/login')
@app.route('/logout')
def serve_react_auth():
    build_dir = os.path.join(os.path.dirname(__file__), 'frontend', 'dist')
    return send_from_directory(build_dir, 'index.html')

@app.route('/')
def index():
    build_dir = os.path.join(os.path.dirname(__file__), 'frontend', 'dist')
    if os.path.exists(os.path.join(build_dir, 'index.html')):
        return send_from_directory(build_dir, 'index.html')
    # Fallback if React build is missing
    conn = libvirt.open('qemu:///system')
    host_info = {}
    if conn:
        try:
            node_info = conn.getInfo()
            host_info['cpu_cores'] = node_info[2]
            host_info['cpu_percent'] = round(psutil.cpu_percent(interval=0.2), 1)
            la1, la5, la15 = os.getloadavg()
            host_info['load_1'] = round(la1, 2)
            host_info['load_5'] = round(la5, 2)
            host_info['load_15'] = round(la15, 2)
            numa_nodes = max(int(node_info[4]) if len(node_info) > 4 else 1, 1)
            total_kib = free_kib = 0
            for cell in range(numa_nodes):
                stats = conn.getMemoryStats(cell)
                total_kib += stats.get('total', 0)
                free_kib += stats.get('free', 0)
            mem_total_gb = total_kib / (1024 ** 2)
            mem_free_gb = free_kib / (1024 ** 2)
            mem_used_gb = mem_total_gb - mem_free_gb
            host_info.update({
                'mem_total_gb': round(mem_total_gb, 2),
                'mem_free_gb': round(mem_free_gb, 2),
                'mem_used_gb': round(mem_used_gb, 2),
                'mem_percent_used': round((mem_used_gb / mem_total_gb) * 100, 1) if mem_total_gb else 0,
                'storage_pools': [],
            })
            for pool_name in conn.listStoragePools():
                pool = conn.storagePoolLookupByName(pool_name)
                pool.refresh(0)
                info = pool.info()
                host_info['storage_pools'].append({
                    'name': pool_name,
                    'capacity_gb': round(info[1] / (1024 ** 3), 2),
                    'allocation_gb': round(info[2] / (1024 ** 3), 2),
                    'available_gb': round(info[3] / (1024 ** 3), 2),
                })
        except libvirt.libvirtError as e:
            app.logger.error(f"Error getting host info: {e}")
        finally:
            conn.close()
    return render_template('home.html', host=host_info)

@app.route('/<path:path>')
def serve_react(path):
    if path.startswith('api/') or path.startswith('docker-exec') or path in ('login', 'logout', 'host-terminal'):
        return "Not found", 404
    build_dir = os.path.realpath(os.path.join(os.path.dirname(__file__), 'frontend', 'dist'))
    # Guard against path traversal
    target = os.path.realpath(os.path.join(build_dir, path))
    if target.startswith(build_dir) and os.path.isfile(target):
        return send_from_directory(build_dir, path)
    index_path = os.path.join(build_dir, 'index.html')
    if os.path.exists(index_path):
        return send_from_directory(build_dir, 'index.html')
    return "Frontend not built yet", 404

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=os.environ.get('FLASK_DEBUG', 'false').lower() == 'true')
