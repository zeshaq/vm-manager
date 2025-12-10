from flask import Flask, session, redirect, url_for, request, render_template
import simplepam
import os

# Import the blueprints
from views.listing import listing_bp
from views.creation import creation_bp
from views.storage import storage_bp
from views.audit import audit_bp
from views.loadbalancer import lb_bp
from views.setup import setup_bp
from views.terminal import terminal_bp

from sockets import sock

app = Flask(__name__)
sock.init_app(app)

# Register the blueprints
app.register_blueprint(listing_bp)
app.register_blueprint(creation_bp)
app.register_blueprint(storage_bp)
app.register_blueprint(audit_bp)
app.register_blueprint(lb_bp)
app.register_blueprint(setup_bp)
app.register_blueprint(terminal_bp)

@app.before_request
def before_request():
    if 'username' not in session and request.endpoint not in ['login']:
        return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        if simplepam.authenticate(username, password):
            session['username'] = username
            return redirect(url_for('index'))
        else:
            return render_template('login.html', error='Invalid credentials')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('username', None)
    return redirect(url_for('login'))

import libvirt

# Simple route for the root URL
@app.route('/')
def index():
    conn = libvirt.open('qemu:///system')
    host_info = {}
    if conn:
        try:
            # Get Host CPU Info
            node_info = conn.getInfo()
            host_info['cpu_cores'] = node_info[2]
            
            # Get Host Memory Info
            mem_info = conn.getMemoryStats(0)
            host_info['mem_total_gb'] = round(mem_info['total'] / (1024**2), 2)
            host_info['mem_free_gb'] = round(mem_info['free'] / (1024**2), 2)
            host_info['mem_used_gb'] = round(host_info['mem_total_gb'] - host_info['mem_free_gb'], 2)
            if host_info['mem_total_gb'] > 0:
                host_info['mem_percent_used'] = round((host_info['mem_used_gb'] / host_info['mem_total_gb']) * 100, 1)
            else:
                host_info['mem_percent_used'] = 0

            # Get Storage Pool Info
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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)