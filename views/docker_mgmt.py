import re
import docker
import docker.errors
from flask import Blueprint, jsonify, request, session

docker_bp = Blueprint('docker', __name__)

# Docker container/image names: alphanumeric, hyphens, underscores, dots, colons, slashes
_ID_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.\-:/]{0,255}$')


def _auth():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    return None


def _client():
    return docker.from_env()


def _safe_id(value):
    return bool(value and _ID_RE.match(value))


# ── Info ──────────────────────────────────────────────────────────────────────

@docker_bp.route('/api/docker/info')
def docker_info():
    err = _auth()
    if err:
        return err
    try:
        info = _client().info()
        return jsonify({
            'containers':         info.get('Containers', 0),
            'containers_running': info.get('ContainersRunning', 0),
            'containers_paused':  info.get('ContainersPaused', 0),
            'containers_stopped': info.get('ContainersStopped', 0),
            'images':             info.get('Images', 0),
            'server_version':     info.get('ServerVersion', ''),
            'storage_driver':     info.get('Driver', ''),
            'memory_total':       info.get('MemTotal', 0),
        })
    except docker.errors.DockerException as e:
        return jsonify({'error': str(e)}), 503


# ── Containers ────────────────────────────────────────────────────────────────

@docker_bp.route('/api/docker/containers')
def list_containers():
    err = _auth()
    if err:
        return err
    all_ = request.args.get('all', 'true').lower() != 'false'
    try:
        containers = _client().containers.list(all=all_)
        result = []
        for c in containers:
            tags = c.image.tags
            result.append({
                'id':      c.id[:12],
                'full_id': c.id,
                'name':    c.name,
                'image':   tags[0] if tags else c.image.short_id,
                'status':  c.status,
                'created': c.attrs.get('Created', ''),
                'ports':   c.ports,
                'state':   c.attrs.get('State', {}),
                'command': c.attrs.get('Config', {}).get('Cmd') or '',
            })
        return jsonify(result)
    except docker.errors.DockerException as e:
        return jsonify({'error': str(e)}), 503


@docker_bp.route('/api/docker/containers/<container_id>')
def get_container(container_id):
    err = _auth()
    if err:
        return err
    if not _safe_id(container_id):
        return jsonify({'error': 'Invalid container id'}), 400
    try:
        c = _client().containers.get(container_id)
        tags = c.image.tags
        return jsonify({
            'id':               c.id[:12],
            'full_id':          c.id,
            'name':             c.name,
            'image':            tags[0] if tags else c.image.short_id,
            'status':           c.status,
            'created':          c.attrs.get('Created', ''),
            'ports':            c.ports,
            'state':            c.attrs.get('State', {}),
            'config':           c.attrs.get('Config', {}),
            'network_settings': c.attrs.get('NetworkSettings', {}),
            'mounts':           c.attrs.get('Mounts', []),
            'host_config':      c.attrs.get('HostConfig', {}),
        })
    except docker.errors.NotFound:
        return jsonify({'error': 'Container not found'}), 404
    except docker.errors.DockerException as e:
        return jsonify({'error': str(e)}), 503


@docker_bp.route('/api/docker/containers/<container_id>/start', methods=['POST'])
def start_container(container_id):
    err = _auth()
    if err:
        return err
    if not _safe_id(container_id):
        return jsonify({'error': 'Invalid container id'}), 400
    try:
        _client().containers.get(container_id).start()
        return jsonify({'success': True})
    except docker.errors.NotFound:
        return jsonify({'error': 'Container not found'}), 404
    except docker.errors.DockerException as e:
        return jsonify({'error': str(e)}), 500


@docker_bp.route('/api/docker/containers/<container_id>/stop', methods=['POST'])
def stop_container(container_id):
    err = _auth()
    if err:
        return err
    if not _safe_id(container_id):
        return jsonify({'error': 'Invalid container id'}), 400
    try:
        _client().containers.get(container_id).stop(timeout=10)
        return jsonify({'success': True})
    except docker.errors.NotFound:
        return jsonify({'error': 'Container not found'}), 404
    except docker.errors.DockerException as e:
        return jsonify({'error': str(e)}), 500


@docker_bp.route('/api/docker/containers/<container_id>/restart', methods=['POST'])
def restart_container(container_id):
    err = _auth()
    if err:
        return err
    if not _safe_id(container_id):
        return jsonify({'error': 'Invalid container id'}), 400
    try:
        _client().containers.get(container_id).restart(timeout=10)
        return jsonify({'success': True})
    except docker.errors.NotFound:
        return jsonify({'error': 'Container not found'}), 404
    except docker.errors.DockerException as e:
        return jsonify({'error': str(e)}), 500


@docker_bp.route('/api/docker/containers/<container_id>', methods=['DELETE'])
def remove_container(container_id):
    err = _auth()
    if err:
        return err
    if not _safe_id(container_id):
        return jsonify({'error': 'Invalid container id'}), 400
    force = request.args.get('force', 'false').lower() == 'true'
    try:
        _client().containers.get(container_id).remove(force=force)
        return jsonify({'success': True})
    except docker.errors.NotFound:
        return jsonify({'error': 'Container not found'}), 404
    except docker.errors.DockerException as e:
        return jsonify({'error': str(e)}), 500


@docker_bp.route('/api/docker/containers/<container_id>/logs')
def container_logs(container_id):
    err = _auth()
    if err:
        return err
    if not _safe_id(container_id):
        return jsonify({'error': 'Invalid container id'}), 400
    tail = min(int(request.args.get('tail', 200)), 2000)
    try:
        c = _client().containers.get(container_id)
        raw = c.logs(tail=tail, timestamps=True, stream=False)
        return jsonify({'logs': raw.decode('utf-8', errors='replace')})
    except docker.errors.NotFound:
        return jsonify({'error': 'Container not found'}), 404
    except docker.errors.DockerException as e:
        return jsonify({'error': str(e)}), 500


# ── Images ────────────────────────────────────────────────────────────────────

@docker_bp.route('/api/docker/images')
def list_images():
    err = _auth()
    if err:
        return err
    try:
        images = _client().images.list(all=False)
        result = []
        for img in images:
            result.append({
                'id':       img.id,
                'short_id': img.short_id,
                'tags':     img.tags,
                'created':  img.attrs.get('Created', ''),
                'size':     img.attrs.get('Size', 0),
            })
        return jsonify(result)
    except docker.errors.DockerException as e:
        return jsonify({'error': str(e)}), 503


@docker_bp.route('/api/docker/images/pull', methods=['POST'])
def pull_image():
    err = _auth()
    if err:
        return err
    data = request.get_json() or {}
    name = str(data.get('image', '')).strip()[:256]
    if not name:
        return jsonify({'error': 'Image name required'}), 400
    if not _safe_id(name):
        return jsonify({'error': 'Invalid image name'}), 400
    try:
        _client().images.pull(name)
        return jsonify({'success': True})
    except docker.errors.ImageNotFound:
        return jsonify({'error': f'Image "{name}" not found'}), 404
    except docker.errors.DockerException as e:
        return jsonify({'error': str(e)}), 500


@docker_bp.route('/api/docker/images/remove', methods=['POST'])
def remove_image():
    err = _auth()
    if err:
        return err
    data = request.get_json() or {}
    image_id = str(data.get('id', '')).strip()
    force = bool(data.get('force', False))
    if not image_id:
        return jsonify({'error': 'Image id required'}), 400
    try:
        _client().images.remove(image_id, force=force)
        return jsonify({'success': True})
    except docker.errors.ImageNotFound:
        return jsonify({'error': 'Image not found'}), 404
    except docker.errors.DockerException as e:
        return jsonify({'error': str(e)}), 500


# ── Networks ──────────────────────────────────────────────────────────────────

@docker_bp.route('/api/docker/networks')
def list_networks():
    err = _auth()
    if err:
        return err
    try:
        networks = _client().networks.list()
        result = []
        for net in networks:
            ipam = net.attrs.get('IPAM', {})
            configs = (ipam.get('Config') or [])
            subnet = configs[0].get('Subnet', '') if configs else ''
            gateway = configs[0].get('Gateway', '') if configs else ''
            result.append({
                'id':         net.id[:12],
                'full_id':    net.id,
                'name':       net.name,
                'driver':     net.attrs.get('Driver', ''),
                'scope':      net.attrs.get('Scope', ''),
                'internal':   net.attrs.get('Internal', False),
                'subnet':     subnet,
                'gateway':    gateway,
                'containers': len(net.attrs.get('Containers', {})),
            })
        return jsonify(result)
    except docker.errors.DockerException as e:
        return jsonify({'error': str(e)}), 503


@docker_bp.route('/api/docker/networks', methods=['POST'])
def create_network():
    err = _auth()
    if err:
        return err
    data = request.get_json() or {}
    name = str(data.get('name', '')).strip()[:64]
    driver = str(data.get('driver', 'bridge')).strip()
    if not name:
        return jsonify({'error': 'Network name required'}), 400
    if driver not in ('bridge', 'overlay', 'host', 'none', 'macvlan'):
        return jsonify({'error': 'Invalid driver'}), 400
    try:
        net = _client().networks.create(name, driver=driver)
        return jsonify({'success': True, 'id': net.id[:12]})
    except docker.errors.DockerException as e:
        return jsonify({'error': str(e)}), 500


@docker_bp.route('/api/docker/networks/<network_id>', methods=['DELETE'])
def remove_network(network_id):
    err = _auth()
    if err:
        return err
    if not _safe_id(network_id):
        return jsonify({'error': 'Invalid network id'}), 400
    try:
        _client().networks.get(network_id).remove()
        return jsonify({'success': True})
    except docker.errors.NotFound:
        return jsonify({'error': 'Network not found'}), 404
    except docker.errors.DockerException as e:
        return jsonify({'error': str(e)}), 500


# ── Volumes ───────────────────────────────────────────────────────────────────

@docker_bp.route('/api/docker/volumes')
def list_volumes():
    err = _auth()
    if err:
        return err
    try:
        volumes = _client().volumes.list()
        result = []
        for vol in volumes:
            result.append({
                'name':       vol.name,
                'driver':     vol.attrs.get('Driver', ''),
                'mountpoint': vol.attrs.get('Mountpoint', ''),
                'created':    vol.attrs.get('CreatedAt', ''),
                'labels':     vol.attrs.get('Labels') or {},
            })
        return jsonify(result)
    except docker.errors.DockerException as e:
        return jsonify({'error': str(e)}), 503


@docker_bp.route('/api/docker/volumes/<volume_name>', methods=['DELETE'])
def remove_volume(volume_name):
    err = _auth()
    if err:
        return err
    if not _safe_id(volume_name):
        return jsonify({'error': 'Invalid volume name'}), 400
    try:
        _client().volumes.get(volume_name).remove()
        return jsonify({'success': True})
    except docker.errors.NotFound:
        return jsonify({'error': 'Volume not found'}), 404
    except docker.errors.DockerException as e:
        return jsonify({'error': str(e)}), 500
