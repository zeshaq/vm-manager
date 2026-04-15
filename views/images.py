"""
Image registry — Glance-like store for cloud / VM base images.

Images live in /var/lib/libvirt/images/.
Metadata is persisted in APP_DIR/image_registry.json.

Features
────────
• Catalog of popular cloud images with one-click download
• Custom URL download with live SSE progress streaming
• Direct file upload
• Auto-scan: any .img / .qcow2 already on disk is auto-registered
• qemu-img info for virtual-size / format detection
• Images are selectable in VM creation and Kubernetes deployment
"""

import os, json, re, time, uuid as _uuid, threading, traceback, subprocess
import urllib.request
from pathlib import Path
from flask import Blueprint, jsonify, request, session, Response, stream_with_context, send_file
from werkzeug.utils import secure_filename

images_bp = Blueprint('images', __name__)

APP_DIR       = Path(__file__).parent.parent
REGISTRY_FILE = APP_DIR / 'image_registry.json'
IMAGES_DIR    = Path('/var/lib/libvirt/images/cloud-images')

# In-memory download jobs: { job_id: {downloaded, total, status, error, image_id} }
_DOWNLOADS: dict = {}
_DL_LOCK = threading.Lock()

SUPPORTED_EXTS = {'.img', '.qcow2', '.raw', '.vmdk', '.vhd', '.vhdx'}

# ── Auth ──────────────────────────────────────────────────────────────────────

def _auth():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    return None


# ── Registry I/O ──────────────────────────────────────────────────────────────

def _load_registry():
    if not REGISTRY_FILE.exists():
        return []
    try:
        with open(REGISTRY_FILE) as f:
            return json.load(f).get('images', [])
    except Exception:
        return []


def _save_registry(images):
    with open(REGISTRY_FILE, 'w') as f:
        json.dump({'images': images}, f, indent=2)


def _get_image(image_id):
    return next((i for i in _load_registry() if i['id'] == image_id), None)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _qemu_info(path):
    """Return {virtual_size, format} via qemu-img info, or safe defaults."""
    import subprocess, shutil
    if not shutil.which('qemu-img'):
        return {'virtual_size': 0, 'format': 'unknown'}
    try:
        r = subprocess.run(
            ['qemu-img', 'info', '--output=json', path],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0:
            d = json.loads(r.stdout)
            return {
                'virtual_size': d.get('virtual-size', 0),
                'format':       d.get('format', 'unknown'),
            }
    except Exception:
        pass
    return {'virtual_size': 0, 'format': 'unknown'}


def _guess_os(filename):
    fn = filename.lower()
    if 'ubuntu'   in fn: return 'ubuntu'
    if 'debian'   in fn: return 'debian'
    if 'centos'   in fn: return 'centos'
    if 'rocky'    in fn: return 'rocky'
    if 'alma'     in fn: return 'almalinux'
    if 'fedora'   in fn: return 'fedora'
    if 'arch'     in fn: return 'arch'
    if 'opensuse' in fn: return 'opensuse'
    if 'freebsd'  in fn: return 'freebsd'
    return 'linux'


def _guess_version(filename):
    m = re.search(r'(\d+\.\d+|\d{2,})', filename)
    return m.group(1) if m else ''


def _scan_unregistered():
    """Find image files on disk not yet in registry; auto-register them."""
    if not IMAGES_DIR.exists():
        return
    images  = _load_registry()
    known   = {img['path'] for img in images}
    changed = False
    for p in IMAGES_DIR.iterdir():
        if p.suffix.lower() not in SUPPORTED_EXTS or p.name.startswith('.'):
            continue
        if str(p) in known:
            continue
        info = _qemu_info(str(p))
        images.append({
            'id':           _uuid.uuid4().hex[:12],
            'name':         p.stem,
            'filename':     p.name,
            'path':         str(p),
            'os':           _guess_os(p.name),
            'version':      _guess_version(p.name),
            'description':  f'Auto-detected: {p.name}',
            'format':       info['format'],
            'size':         p.stat().st_size,
            'virtual_size': info['virtual_size'],
            'status':       'available',
            'source_url':   None,
            'added_at':     int(p.stat().st_mtime),
        })
        changed = True
    if changed:
        _save_registry(images)


# ── Catalog of popular cloud images ──────────────────────────────────────────

CATALOG = [
    {
        'name': 'Ubuntu 24.04 LTS (Noble)',
        'os': 'ubuntu', 'version': '24.04',
        'filename': 'ubuntu-24.04-cloudimg.img',
        'url': 'https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img',
        'description': 'Ubuntu 24.04 LTS Noble Numbat — latest LTS',
        'k8s_compatible': True,
    },
    {
        'name': 'Ubuntu 22.04 LTS (Jammy)',
        'os': 'ubuntu', 'version': '22.04',
        'filename': 'ubuntu-22.04-cloudimg.img',
        'url': 'https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img',
        'description': 'Ubuntu 22.04 LTS Jammy Jellyfish — recommended for Kubernetes',
        'k8s_compatible': True,
    },
    {
        'name': 'Ubuntu 20.04 LTS (Focal)',
        'os': 'ubuntu', 'version': '20.04',
        'filename': 'ubuntu-20.04-cloudimg.img',
        'url': 'https://cloud-images.ubuntu.com/focal/current/focal-server-cloudimg-amd64.img',
        'description': 'Ubuntu 20.04 LTS Focal Fossa',
        'k8s_compatible': True,
    },
    {
        'name': 'Debian 12 (Bookworm)',
        'os': 'debian', 'version': '12',
        'filename': 'debian-12-cloudimg.qcow2',
        'url': 'https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2',
        'description': 'Debian 12 Bookworm genericcloud',
        'k8s_compatible': True,
    },
    {
        'name': 'Debian 11 (Bullseye)',
        'os': 'debian', 'version': '11',
        'filename': 'debian-11-cloudimg.qcow2',
        'url': 'https://cloud.debian.org/images/cloud/bullseye/latest/debian-11-genericcloud-amd64.qcow2',
        'description': 'Debian 11 Bullseye genericcloud',
        'k8s_compatible': True,
    },
    {
        'name': 'Rocky Linux 9',
        'os': 'rocky', 'version': '9',
        'filename': 'rocky-9-cloudimg.qcow2',
        'url': 'https://dl.rockylinux.org/pub/rocky/9/images/x86_64/Rocky-9-GenericCloud.latest.x86_64.qcow2',
        'description': 'Rocky Linux 9 — RHEL 9-compatible',
        'k8s_compatible': True,
    },
    {
        'name': 'Rocky Linux 8',
        'os': 'rocky', 'version': '8',
        'filename': 'rocky-8-cloudimg.qcow2',
        'url': 'https://dl.rockylinux.org/pub/rocky/8/images/x86_64/Rocky-8-GenericCloud.latest.x86_64.qcow2',
        'description': 'Rocky Linux 8 — RHEL 8-compatible',
        'k8s_compatible': False,
    },
    {
        'name': 'AlmaLinux 9',
        'os': 'almalinux', 'version': '9',
        'filename': 'almalinux-9-cloudimg.qcow2',
        'url': 'https://repo.almalinux.org/almalinux/9/cloud/x86_64/images/AlmaLinux-9-GenericCloud-latest.x86_64.qcow2',
        'description': 'AlmaLinux 9 — RHEL 9-compatible',
        'k8s_compatible': True,
    },
    {
        'name': 'Fedora 40',
        'os': 'fedora', 'version': '40',
        'filename': 'fedora-40-cloudimg.qcow2',
        'url': 'https://dl.fedoraproject.org/pub/fedora/linux/releases/40/Cloud/x86_64/images/Fedora-Cloud-Base-Generic.x86_64-40-1.14.qcow2',
        'description': 'Fedora 40 Cloud Base',
        'k8s_compatible': False,
    },
    {
        'name': 'CentOS Stream 9',
        'os': 'centos', 'version': '9',
        'filename': 'centos-stream-9-cloudimg.qcow2',
        'url': 'https://cloud.centos.org/centos/9-stream/x86_64/images/CentOS-Stream-GenericCloud-9-latest.x86_64.qcow2',
        'description': 'CentOS Stream 9 — upstream RHEL development',
        'k8s_compatible': False,
    },
]


# ── Background downloader ─────────────────────────────────────────────────────

def _do_download(job_id, image_id, url, dest_path):
    job = _DOWNLOADS[job_id]
    tmp = dest_path + '.part'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Hypercloud/1.0'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            total = int(resp.headers.get('Content-Length', 0))
            with _DL_LOCK:
                job['total'] = total
            chunk_size = 512 * 1024   # 512 KB
            downloaded = 0
            with open(tmp, 'wb') as f:
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    with _DL_LOCK:
                        job['downloaded'] = downloaded

        os.rename(tmp, dest_path)
        info        = _qemu_info(dest_path)
        actual_size = os.path.getsize(dest_path)

        images = _load_registry()
        for img in images:
            if img['id'] == image_id:
                img['size']         = actual_size
                img['virtual_size'] = info['virtual_size']
                img['format']       = info['format']
                img['status']       = 'available'
                break
        _save_registry(images)

        with _DL_LOCK:
            job['status']     = 'done'
            job['downloaded'] = actual_size
            job['total']      = actual_size

    except Exception as exc:
        for f in [tmp, dest_path]:
            try: os.remove(f)
            except FileNotFoundError: pass
        images = _load_registry()
        for img in images:
            if img['id'] == image_id:
                img['status'] = 'failed'
                img['error']  = str(exc)
                break
        _save_registry(images)
        with _DL_LOCK:
            job['status'] = 'error'
            job['error']  = str(exc)


# ── API endpoints ─────────────────────────────────────────────────────────────

@images_bp.route('/api/images')
def list_images():
    err = _auth()
    if err: return err
    _scan_unregistered()
    images = _load_registry()
    # Attach live download progress
    for img in images:
        if img.get('status') == 'downloading':
            jid = img.get('job_id')
            if jid and jid in _DOWNLOADS:
                with _DL_LOCK:
                    j = _DOWNLOADS[jid]
                img['downloaded'] = j.get('downloaded', 0)
                img['total']      = j.get('total', 0)
                if j['status'] in ('done', 'error'):
                    img['status'] = j['status']
    return jsonify({'images': images})


@images_bp.route('/api/images/catalog')
def catalog():
    err = _auth()
    if err: return err
    # Mark which catalog items are already downloaded
    images   = _load_registry()
    on_disk  = {img['filename'] for img in images if img.get('status') == 'available'}
    result   = []
    for item in CATALOG:
        result.append({**item, 'downloaded': item['filename'] in on_disk})
    return jsonify({'catalog': result})


@images_bp.route('/api/images', methods=['POST'])
def add_image():
    """Start a download from URL or register an existing path."""
    err = _auth()
    if err: return err

    data     = request.get_json() or {}
    name     = data.get('name', '').strip()
    url      = data.get('url', '').strip()
    path     = data.get('path', '').strip()   # register existing file
    filename = data.get('filename', '').strip()
    os_hint  = data.get('os', 'linux')
    ver_hint = data.get('version', '')
    desc     = data.get('description', '')

    if not name:
        return jsonify({'error': 'name required'}), 400

    IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    # ── Register existing file ────────────────────────────────────────────────
    if path:
        real = os.path.realpath(path)
        if not os.path.isfile(real):
            return jsonify({'error': f'File not found: {path}'}), 404
        info     = _qemu_info(real)
        image_id = _uuid.uuid4().hex[:12]
        images   = _load_registry()
        if any(i['path'] == real for i in images):
            return jsonify({'error': 'Image already registered'}), 409
        images.append({
            'id': image_id, 'name': name, 'filename': os.path.basename(real),
            'path': real, 'os': os_hint, 'version': ver_hint,
            'description': desc, 'format': info['format'],
            'size': os.path.getsize(real), 'virtual_size': info['virtual_size'],
            'status': 'available', 'source_url': None,
            'added_at': int(time.time()),
        })
        _save_registry(images)
        return jsonify({'image_id': image_id, 'status': 'available'})

    # ── Download from URL ─────────────────────────────────────────────────────
    if not url:
        return jsonify({'error': 'url or path required'}), 400

    if not filename:
        filename = secure_filename(url.split('/')[-1].split('?')[0]) or 'image.img'
    dest_path = str(IMAGES_DIR / filename)

    if os.path.exists(dest_path):
        # Already on disk — just register it
        info     = _qemu_info(dest_path)
        image_id = _uuid.uuid4().hex[:12]
        images   = _load_registry()
        if any(i['path'] == dest_path for i in images):
            return jsonify({'error': 'Image already registered'}), 409
        images.append({
            'id': image_id, 'name': name, 'filename': filename,
            'path': dest_path, 'os': os_hint, 'version': ver_hint,
            'description': desc, 'format': info['format'],
            'size': os.path.getsize(dest_path),
            'virtual_size': info['virtual_size'],
            'status': 'available', 'source_url': url,
            'added_at': int(time.time()),
        })
        _save_registry(images)
        return jsonify({'image_id': image_id, 'status': 'available'})

    # Start background download
    image_id = _uuid.uuid4().hex[:12]
    job_id   = _uuid.uuid4().hex[:12]

    images = _load_registry()
    images.append({
        'id': image_id, 'name': name, 'filename': filename,
        'path': dest_path, 'os': os_hint, 'version': ver_hint,
        'description': desc, 'format': 'unknown',
        'size': 0, 'virtual_size': 0,
        'status': 'downloading', 'job_id': job_id,
        'source_url': url, 'added_at': int(time.time()),
    })
    _save_registry(images)

    with _DL_LOCK:
        _DOWNLOADS[job_id] = {
            'image_id': image_id, 'downloaded': 0,
            'total': 0, 'status': 'running', 'error': None,
        }

    t = threading.Thread(
        target=_do_download, args=(job_id, image_id, url, dest_path), daemon=True
    )
    t.start()

    return jsonify({'image_id': image_id, 'job_id': job_id, 'status': 'downloading'}), 202


@images_bp.route('/api/images/upload', methods=['POST'])
def upload_image():
    err = _auth()
    if err: return err

    f    = request.files.get('file')
    name = request.form.get('name', '').strip()
    if not f:
        return jsonify({'error': 'No file provided'}), 400
    if not name:
        name = Path(f.filename).stem

    filename  = secure_filename(f.filename)
    dest_path = str(IMAGES_DIR / filename)

    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    f.save(dest_path)

    info     = _qemu_info(dest_path)
    image_id = _uuid.uuid4().hex[:12]
    images   = _load_registry()
    images.append({
        'id': image_id, 'name': name, 'filename': filename,
        'path': dest_path,
        'os': _guess_os(filename), 'version': _guess_version(filename),
        'description': f'Uploaded: {filename}',
        'format': info['format'],
        'size': os.path.getsize(dest_path),
        'virtual_size': info['virtual_size'],
        'status': 'available', 'source_url': None,
        'added_at': int(time.time()),
    })
    _save_registry(images)
    return jsonify({'image_id': image_id, 'status': 'available'})


@images_bp.route('/api/images/<image_id>', methods=['DELETE'])
def delete_image(image_id):
    err = _auth()
    if err: return err

    images = _load_registry()
    img    = next((i for i in images if i['id'] == image_id), None)
    if not img:
        return jsonify({'error': 'Not found'}), 404
    if img.get('status') == 'downloading':
        return jsonify({'error': 'Cannot delete while downloading'}), 409

    remove_file = request.args.get('delete_file', 'false').lower() == 'true'
    if remove_file:
        try:
            os.remove(img['path'])
        except FileNotFoundError:
            pass

    images = [i for i in images if i['id'] != image_id]
    _save_registry(images)
    return jsonify({'ok': True, 'file_deleted': remove_file})


@images_bp.route('/api/images/jobs/<job_id>/progress')
def download_progress(job_id):
    """SSE stream: download progress updates."""
    err = _auth()
    if err: return err

    def generate():
        while True:
            job = _DOWNLOADS.get(job_id)
            if not job:
                yield f'data: {json.dumps({"error": "Job not found"})}\n\n'
                return
            with _DL_LOCK:
                snap = dict(job)
            yield f'data: {json.dumps(snap)}\n\n'
            if snap['status'] in ('done', 'error'):
                return
            time.sleep(0.6)

    return Response(
        stream_with_context(generate()),
        content_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@images_bp.route('/api/images/<image_id>/run-script', methods=['POST'])
def run_script(image_id):
    """Run an arbitrary shell script against an image, stream output via SSE."""
    err = _auth()
    if err: return err

    data   = request.get_json() or {}
    script = data.get('script', '').strip()
    if not script:
        return jsonify({'error': 'script required'}), 400

    img = _get_image(image_id)
    if not img:
        return jsonify({'error': 'Image not found'}), 404

    def generate():
        try:
            proc = subprocess.Popen(
                ['bash', '-c', script],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            for line in proc.stdout:
                yield f'data: {json.dumps({"line": line.rstrip()})}\n\n'
            proc.wait()
            status = 'done' if proc.returncode == 0 else 'error'
            yield f'data: {json.dumps({"status": status, "returncode": proc.returncode})}\n\n'
        except Exception as exc:
            yield f'data: {json.dumps({"status": "error", "line": str(exc)})}\n\n'

    return Response(
        stream_with_context(generate()),
        content_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )
