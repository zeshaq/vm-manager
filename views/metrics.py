"""
Prometheus proxy — all queries go through here so:
  - Prometheus stays on localhost (not exposed publicly)
  - Auth is enforced by Flask session
"""
import time
import urllib.request
import urllib.parse
import json as _json
from flask import Blueprint, jsonify, request, session

metrics_bp = Blueprint('metrics', __name__)

PROMETHEUS = 'http://localhost:9090'


def _auth():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    return None


def _prom(path, params=None):
    """GET a Prometheus API endpoint, return parsed JSON."""
    url = f'{PROMETHEUS}{path}'
    if params:
        url += '?' + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return _json.loads(resp.read())
    except Exception as e:
        return {'status': 'error', 'error': str(e)}


def _scalar(query):
    """Return the single float value for an instant query, or None."""
    result = _prom('/api/v1/query', {'query': query})
    try:
        return float(result['data']['result'][0]['value'][1])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _range_series(query, start, end, step):
    """Return list of [timestamp, value] pairs for a range query."""
    result = _prom('/api/v1/query_range', {
        'query': query, 'start': start, 'end': end, 'step': step,
    })
    try:
        results = result['data']['result']
        if not results:
            return []
        # Sum across all series if multiple (e.g. per-CPU)
        if len(results) == 1:
            return [[int(v[0]), float(v[1])] for v in results[0]['values']]
        # Multiple series — sum them per timestamp
        from collections import defaultdict
        totals = defaultdict(float)
        for series in results:
            for ts, val in series['values']:
                try:
                    totals[int(ts)] += float(val)
                except ValueError:
                    pass
        return [[ts, v] for ts, v in sorted(totals.items())]
    except (KeyError, IndexError, TypeError):
        return []


# ── Single dashboard data endpoint ───────────────────────────────────────────

@metrics_bp.route('/api/metrics/dashboard')
def dashboard():
    err = _auth()
    if err:
        return err

    minutes = int(request.args.get('minutes', 60))
    now = int(time.time())
    start = now - minutes * 60
    # Pick step so we get ~120 data points
    step = max(15, (minutes * 60) // 120)

    # ── Instant values ────────────────────────────────────────────────────────
    cpu_pct   = _scalar('100 - (avg(irate(node_cpu_seconds_total{mode="idle"}[2m])) * 100)')
    mem_total = _scalar('node_memory_MemTotal_bytes')
    mem_avail = _scalar('node_memory_MemAvailable_bytes')
    load1     = _scalar('node_load1')
    load5     = _scalar('node_load5')
    load15    = _scalar('node_load15')

    mem_used  = (mem_total - mem_avail) if mem_total and mem_avail else None
    mem_pct   = (mem_used / mem_total * 100) if mem_total and mem_used else None

    # ── Disk space ────────────────────────────────────────────────────────────
    disk_total = _scalar('node_filesystem_size_bytes{mountpoint="/",fstype!="tmpfs"}')
    disk_avail = _scalar('node_filesystem_avail_bytes{mountpoint="/",fstype!="tmpfs"}')
    disk_used  = (disk_total - disk_avail) if disk_total and disk_avail else None
    disk_pct   = (disk_used / disk_total * 100) if disk_total and disk_used else None

    # ── Historical series ─────────────────────────────────────────────────────
    cpu_hist   = _range_series('100 - (avg(irate(node_cpu_seconds_total{mode="idle"}[2m])) * 100)', start, now, step)
    mem_hist   = _range_series('(node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes) / node_memory_MemTotal_bytes * 100', start, now, step)
    load_hist  = _range_series('node_load1', start, now, step)
    net_rx     = _range_series('sum(irate(node_network_receive_bytes_total{device!~"lo|docker.*|br-.*|virbr.*"}[2m]))', start, now, step)
    net_tx     = _range_series('sum(irate(node_network_transmit_bytes_total{device!~"lo|docker.*|br-.*|virbr.*"}[2m]))', start, now, step)
    disk_read  = _range_series('sum(irate(node_disk_read_bytes_total[2m]))', start, now, step)
    disk_write = _range_series('sum(irate(node_disk_written_bytes_total[2m]))', start, now, step)

    return jsonify({
        'cpu_pct':    round(cpu_pct, 1)  if cpu_pct  is not None else None,
        'mem_pct':    round(mem_pct, 1)  if mem_pct  is not None else None,
        'mem_used':   mem_used,
        'mem_total':  mem_total,
        'disk_pct':   round(disk_pct, 1) if disk_pct is not None else None,
        'disk_used':  disk_used,
        'disk_total': disk_total,
        'load1':      round(load1,  2)   if load1    is not None else None,
        'load5':      round(load5,  2)   if load5    is not None else None,
        'load15':     round(load15, 2)   if load15   is not None else None,
        'history': {
            'cpu':        cpu_hist,
            'memory':     mem_hist,
            'load':       load_hist,
            'net_rx':     net_rx,
            'net_tx':     net_tx,
            'disk_read':  disk_read,
            'disk_write': disk_write,
        },
    })


# ── Docker / container metrics ────────────────────────────────────────────────

@metrics_bp.route('/api/metrics/containers')
def containers():
    err = _auth()
    if err:
        return err

    minutes = int(request.args.get('minutes', 30))
    now = int(time.time())
    start = now - minutes * 60
    step = max(15, (minutes * 60) // 60)

    # Current CPU % per container (cAdvisor)
    cpu_result = _prom('/api/v1/query', {
        'query': 'sum by (name) (irate(container_cpu_usage_seconds_total{name!="",name!~"k8s_.*"}[2m])) * 100'
    })
    mem_result = _prom('/api/v1/query', {
        'query': 'container_memory_usage_bytes{name!="",name!~"k8s_.*"}'
    })
    mem_limit_result = _prom('/api/v1/query', {
        'query': 'container_spec_memory_limit_bytes{name!="",name!~"k8s_.*"}'
    })

    # Build per-container instant stats
    containers_map = {}

    try:
        for r in (cpu_result.get('data', {}).get('result') or []):
            name = r['metric'].get('name', '')
            if name:
                containers_map.setdefault(name, {})['cpu_pct'] = round(float(r['value'][1]), 2)
    except Exception:
        pass

    try:
        for r in (mem_result.get('data', {}).get('result') or []):
            name = r['metric'].get('name', '')
            if name:
                containers_map.setdefault(name, {})['mem_bytes'] = int(float(r['value'][1]))
    except Exception:
        pass

    try:
        for r in (mem_limit_result.get('data', {}).get('result') or []):
            name = r['metric'].get('name', '')
            if name:
                limit = int(float(r['value'][1]))
                containers_map.setdefault(name, {})['mem_limit'] = limit if limit > 0 else None
    except Exception:
        pass

    stats = [{'name': k, **v} for k, v in sorted(containers_map.items())]

    # Top 5 containers by CPU for history chart
    top_names = sorted(containers_map.items(), key=lambda x: x[1].get('cpu_pct', 0), reverse=True)[:5]
    history = {}
    for name, _ in top_names:
        safe = name.replace('"', '')
        series = _range_series(
            f'sum(irate(container_cpu_usage_seconds_total{{name="{safe}"}}[2m])) * 100',
            start, now, step
        )
        if series:
            history[name] = series

    return jsonify({'containers': stats, 'history': history})


# ── Raw proxy for custom PromQL ───────────────────────────────────────────────

@metrics_bp.route('/api/metrics/query')
def query():
    err = _auth()
    if err:
        return err
    q = request.args.get('q', '')
    if not q:
        return jsonify({'error': 'q param required'}), 400
    return jsonify(_prom('/api/v1/query', {'query': q}))
