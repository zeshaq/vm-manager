from flask import Blueprint, render_template
import psutil
import datetime

dashboard_bp = Blueprint('dashboard', __name__)

@dashboard_bp.route('/dashboard')
def dashboard():
    # System Uptime
    uptime = datetime.datetime.now() - datetime.datetime.fromtimestamp(psutil.boot_time())
    
    # CPU Info
    cpu_percent = psutil.cpu_percent(interval=1)
    cpu_load = psutil.getloadavg()
    
    # Memory Info
    mem = psutil.virtual_memory()
    mem_total_gb = round(mem.total / (1024**3), 2)
    mem_used_gb = round(mem.used / (1024**3), 2)
    mem_percent = mem.percent
    
    # Disk Info
    disk = psutil.disk_usage('/')
    disk_total_gb = round(disk.total / (1024**3), 2)
    disk_used_gb = round(disk.used / (1024**3), 2)
    disk_percent = disk.percent
    
    # Network Info
    net = psutil.net_io_counters()
    
    # Top Processes
    processes = []
    for proc in sorted(psutil.process_iter(['pid', 'name', 'username', 'cpu_percent', 'memory_percent']), key=lambda p: p.info['cpu_percent'], reverse=True)[:5]:
        processes.append(proc.info)

    return render_template('dashboard.html', 
                           uptime=uptime,
                           cpu_percent=cpu_percent,
                           cpu_load=cpu_load,
                           mem_total_gb=mem_total_gb,
                           mem_used_gb=mem_used_gb,
                           mem_percent=mem_percent,
                           disk_total_gb=disk_total_gb,
                           disk_used_gb=disk_used_gb,
                           disk_percent=disk_percent,
                           net=net,
                           processes=processes)
