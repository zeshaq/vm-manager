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
from views.projects import projects_bp
from views.api import api_bp

from sockets import sock

app = Flask(__name__, static_folder='frontend/dist/assets', static_url_path='/assets')
app.secret_key = os.environ.get('SECRET_KEY', 'vm-manager-default-secret-change-me')
app.permanent_session_lifetime = timedelta(days=30)
sock.init_app(app)

# Register the blueprints
app.register_blueprint(listing_bp)
app.register_blueprint(creation_bp)
app.register_blueprint(storage_bp)
app.register_blueprint(terminal_bp)
app.register_blueprint(host_terminal_bp)
app.register_blueprint(dashboard_bp)
app.register_blueprint(projects_bp)
app.register_blueprint(api_bp)

@app.route('/login')
@app.route('/logout')
def serve_react_auth():
    build_dir = os.path.join(os.path.dirname(__file__), 'frontend', 'dist')
    return send_from_directory(build_dir, 'index.html')

# Simple route for the root URL
@app.route('/')
def index():
    build_dir = os.path.join(os.path.dirname(__file__), 'frontend', 'dist')
    index_path = os.path.join(build_dir, 'index.html')
    if os.path.exists(index_path):
        return send_from_directory(build_dir, 'index.html')
    # Fallback to old Jinja template if frontend not built
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
            numa_nodes = int(node_info[4]) if len(node_info) > 4 else 1
            if numa_nodes <= 0:
                numa_nodes = 1
            total_kib = 0
            free_kib = 0
            for cell in range(numa_nodes):
                stats = conn.getMemoryStats(cell)
                total_kib += stats.get('total', 0)
                free_kib += stats.get('free', 0)
            mem_total_gb = total_kib / (1024**2)
            mem_free_gb = free_kib / (1024**2)
            mem_used_gb = mem_total_gb - mem_free_gb
            host_info['mem_total_gb'] = round(mem_total_gb, 2)
            host_info['mem_free_gb'] = round(mem_free_gb, 2)
            host_info['mem_used_gb'] = round(mem_used_gb, 2)
            if mem_total_gb > 0:
                host_info['mem_percent_used'] = round((mem_used_gb / mem_total_gb) * 100, 1)
            else:
                host_info['mem_percent_used'] = 0
            storage_pools = []
            for pool_name in conn.listStoragePools():
                pool = conn.storagePoolLookupByName(pool_name)
                pool.refresh(0)
                info = pool.info()
                storage_pools.append({
                    'name': pool_name,
                    'capacity_gb': round(info[1] / (1024**3), 2),
                    'allocation_gb': round(info[2] / (1024**3), 2),
                    'available_gb': round(info[3] / (1024**3), 2)
                })
            host_info['storage_pools'] = storage_pools
        except libvirt.libvirtError as e:
            print(f"Error getting host info: {e}")
        finally:
            conn.close()

    return render_template('home.html', host=host_info)

# Catch-all route to serve the React build
@app.route('/<path:path>')
def serve_react(path):
    if path.startswith('api/') or path in ['login', 'logout']:
        return "Not found", 404
    build_dir = os.path.join(os.path.dirname(__file__), 'frontend', 'dist')
    # Try to serve static files from dist directly
    full_path = os.path.join(build_dir, path)
    if os.path.exists(full_path) and os.path.isfile(full_path):
        return send_from_directory(build_dir, path)
    index_path = os.path.join(build_dir, 'index.html')
    if os.path.exists(index_path):
        return send_from_directory(build_dir, 'index.html')
    return "Frontend not built yet", 404

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
