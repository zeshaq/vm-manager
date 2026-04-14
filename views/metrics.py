"""
Host + VM metrics — pure Python, no external services required.

A daemon thread samples psutil every INTERVAL seconds and stores readings
in fixed-length deques (ring buffer).  The API endpoints serve current
snapshots plus the in-memory history.
"""

import collections
import threading
import time
import xml.etree.ElementTree as ET
from flask import Blueprint, jsonify, request, session

try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False

try:
    import libvirt
    _LIBVIRT = True
except ImportError:
    _LIBVIRT = False

metrics_bp = Blueprint('metrics', __name__)

# ── ring-buffer config ────────────────────────────────────────────────────────
INTERVAL    = 15          # seconds between samples
HISTORY_LEN = 5760        # 5760 × 15 s = 24 hours

_history = {
    'cpu':        collections.deque(maxlen=HISTORY_LEN),
    'memory':     collections.deque(maxlen=HISTORY_LEN),
    'load':       collections.deque(maxlen=HISTORY_LEN),
    'net_rx':     collections.deque(maxlen=HISTORY_LEN),
    'net_tx':     collections.deque(maxlen=HISTORY_LEN),
    'disk_read':  collections.deque(maxlen=HISTORY_LEN),
    'disk_write': collections.deque(maxlen=HISTORY_LEN),
}
_lock      = threading.Lock()
_prev_net  = None   # (ts, bytes_recv, bytes_sent)
_prev_disk = None   # (ts, read_bytes, write_bytes)


def _collect_loop():
    """Background daemon thread — runs forever, never raises."""
    global _prev_net, _prev_disk

    # Prime cpu_percent so first real call is non-blocking
    if _PSUTIL:
        psutil.cpu_percent(interval=None)

    while True:
        try:
            if not _PSUTIL:
                time.sleep(INTERVAL)
                continue

            ts = int(time.time())

            # CPU
            cpu_pct = psutil.cpu_percent(interval=None)

            # Memory
            mem = psutil.virtual_memory()

            # Load average
            try:
                load1 = psutil.getloadavg()[0]
            except AttributeError:
                load1 = 0.0

            # Network rates
            net = psutil.net_io_counters()
            net_rx_rate = net_tx_rate = 0.0
            if _prev_net:
                dt = ts - _prev_net[0]
                if dt > 0:
                    net_rx_rate = max(0.0, (net.bytes_recv - _prev_net[1]) / dt)
                    net_tx_rate = max(0.0, (net.bytes_sent - _prev_net[2]) / dt)
            _prev_net = (ts, net.bytes_recv, net.bytes_sent)

            # Disk I/O rates
            disk_io = psutil.disk_io_counters()
            disk_r_rate = disk_w_rate = 0.0
            if _prev_disk and disk_io:
                dt = ts - _prev_disk[0]
                if dt > 0:
                    disk_r_rate = max(0.0, (disk_io.read_bytes  - _prev_disk[1]) / dt)
                    disk_w_rate = max(0.0, (disk_io.write_bytes - _prev_disk[2]) / dt)
            if disk_io:
                _prev_disk = (ts, disk_io.read_bytes, disk_io.write_bytes)

            with _lock:
                _history['cpu'].append([ts, round(cpu_pct, 1)])
                _history['memory'].append([ts, round(mem.percent, 1)])
                _history['load'].append([ts, round(load1, 2)])
                _history['net_rx'].append([ts, round(net_rx_rate)])
                _history['net_tx'].append([ts, round(net_tx_rate)])
                _history['disk_read'].append([ts, round(disk_r_rate)])
                _history['disk_write'].append([ts, round(disk_w_rate)])

        except Exception:
            pass   # never crash the thread

        time.sleep(INTERVAL)


# Start collector once when this module is first imported
_thread = threading.Thread(target=_collect_loop, daemon=True, name='metrics-collector')
_thread.start()


# ── helpers ───────────────────────────────────────────────────────────────────

def _auth():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    return None


def _since(key, cutoff):
    with _lock:
        return [[ts, v] for ts, v in _history[key] if ts >= cutoff]


# ── /api/metrics/dashboard ───────────────────────────────────────────────────

@metrics_bp.route('/api/metrics/dashboard')
def dashboard():
    err = _auth()
    if err:
        return err

    if not _PSUTIL:
        return jsonify({'error': 'psutil not installed — run: pip install psutil'}), 503

    minutes = int(request.args.get('minutes', 60))
    cutoff  = int(time.time()) - minutes * 60

    cpu_pct = psutil.cpu_percent(interval=0.1)
    mem     = psutil.virtual_memory()

    try:
        disk       = psutil.disk_usage('/')
        disk_pct   = round(disk.percent, 1)
        disk_used  = disk.used
        disk_total = disk.total
    except Exception:
        disk_pct = disk_used = disk_total = None

    try:
        load1, load5, load15 = psutil.getloadavg()
    except AttributeError:
        load1 = load5 = load15 = 0.0

    return jsonify({
        'cpu_pct':    round(cpu_pct, 1),
        'mem_pct':    round(mem.percent, 1),
        'mem_used':   mem.used,
        'mem_total':  mem.total,
        'disk_pct':   disk_pct,
        'disk_used':  disk_used,
        'disk_total': disk_total,
        'load1':      round(load1,  2),
        'load5':      round(load5,  2),
        'load15':     round(load15, 2),
        'history': {
            'cpu':        _since('cpu',        cutoff),
            'memory':     _since('memory',     cutoff),
            'load':       _since('load',       cutoff),
            'net_rx':     _since('net_rx',     cutoff),
            'net_tx':     _since('net_tx',     cutoff),
            'disk_read':  _since('disk_read',  cutoff),
            'disk_write': _since('disk_write', cutoff),
        },
    })


# ── /api/metrics/vms ─────────────────────────────────────────────────────────

_vm_samples: dict = {}


def _parse_vm_stats(dom):
    global _vm_samples

    try:
        state, _ = dom.state(0)
    except Exception:
        return None
    if state != 1:
        return None

    uuid = dom.UUIDString()
    now  = time.time()

    mem_used = mem_total = cpu_ns = None
    try:
        info      = dom.info()
        mem_total = info[1] * 1024
        mem_used  = info[2] * 1024
        cpu_ns    = info[4]
    except Exception:
        pass

    disk_r_bytes = disk_w_bytes = 0
    root = None
    try:
        xml  = dom.XMLDesc(0)
        root = ET.fromstring(xml)
        for disk in root.findall('.//devices/disk[@device="disk"]'):
            tgt = disk.find('target')
            if tgt is None:
                continue
            try:
                st = dom.blockStats(tgt.get('dev', ''))
                disk_r_bytes += st[1]
                disk_w_bytes += st[3]
            except Exception:
                pass
    except Exception:
        pass

    net_rx_bytes = net_tx_bytes = 0
    try:
        if root is None:
            root = ET.fromstring(dom.XMLDesc(0))
        for iface in root.findall('.//devices/interface'):
            tgt = iface.find('target')
            if tgt is None:
                continue
            dev = tgt.get('dev', '')
            if not dev:
                continue
            try:
                st = dom.interfaceStats(dev)
                net_rx_bytes += st[0]
                net_tx_bytes += st[4]
            except Exception:
                pass
    except Exception:
        pass

    prev = _vm_samples.get(uuid)
    cpu_pct = disk_r_rate = disk_w_rate = net_rx_rate = net_tx_rate = None

    if prev and cpu_ns is not None:
        dt = now - prev['ts']
        if dt > 0:
            delta_cpu   = cpu_ns - prev.get('cpu_ns', cpu_ns)
            try:
                ncpus = dom.info()[3]
            except Exception:
                ncpus = 1
            cpu_pct     = max(0.0, min(100.0 * ncpus, (delta_cpu / 1e9) / dt * 100))
            disk_r_rate = max(0.0, (disk_r_bytes - prev.get('disk_r', disk_r_bytes)) / dt)
            disk_w_rate = max(0.0, (disk_w_bytes - prev.get('disk_w', disk_w_bytes)) / dt)
            net_rx_rate = max(0.0, (net_rx_bytes  - prev.get('net_rx', net_rx_bytes))  / dt)
            net_tx_rate = max(0.0, (net_tx_bytes  - prev.get('net_tx', net_tx_bytes))  / dt)

    _vm_samples[uuid] = {
        'ts':     now,
        'cpu_ns': cpu_ns or 0,
        'disk_r': disk_r_bytes,
        'disk_w': disk_w_bytes,
        'net_rx': net_rx_bytes,
        'net_tx': net_tx_bytes,
    }

    return {
        'uuid':        uuid,
        'name':        dom.name(),
        'cpu_pct':     round(cpu_pct, 2)     if cpu_pct     is not None else None,
        'mem_used':    mem_used,
        'mem_total':   mem_total,
        'mem_pct':     round(mem_used / mem_total * 100, 1) if mem_used and mem_total else None,
        'disk_r_rate': round(disk_r_rate, 1) if disk_r_rate is not None else None,
        'disk_w_rate': round(disk_w_rate, 1) if disk_w_rate is not None else None,
        'net_rx_rate': round(net_rx_rate, 1) if net_rx_rate is not None else None,
        'net_tx_rate': round(net_tx_rate, 1) if net_tx_rate is not None else None,
    }


@metrics_bp.route('/api/metrics/vms')
def vms_stats():
    err = _auth()
    if err:
        return err

    if not _LIBVIRT:
        return jsonify({'error': 'libvirt not available'}), 503

    try:
        conn = libvirt.open('qemu:///system')
    except Exception as e:
        return jsonify({'error': f'Cannot connect to libvirt: {e}'}), 503

    result = []
    try:
        for dom in conn.listAllDomains(0):
            stats = _parse_vm_stats(dom)
            if stats:
                result.append(stats)
    finally:
        conn.close()

    return jsonify({'vms': result, 'ts': time.time()})
