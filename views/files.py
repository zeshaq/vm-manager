"""
Host filesystem manager — browse, read, write, upload, download.
All paths are resolved with os.path.realpath to prevent traversal.

Sudo mode: user authenticates once with their password; the OS sudo
ticket is cached for SUDO_DURATION seconds. Elevated ops use
`sudo -n` (non-interactive, relies on cached ticket). The password
is never stored — only a session timestamp marking the expiry.
"""
import os
import stat
import shutil
import mimetypes
import subprocess
import time as _time
from pathlib import Path
from flask import Blueprint, jsonify, request, session, send_file
from werkzeug.utils import secure_filename

files_bp = Blueprint('files', __name__)

MAX_READ_BYTES = 3 * 1024 * 1024   # 3 MB cap for inline text display
SUDO_DURATION  = 600                # 10 minutes (less than typical sudoers 15-min timeout)

TEXT_EXTENSIONS = {
    '.py', '.js', '.jsx', '.ts', '.tsx', '.json', '.yaml', '.yml',
    '.toml', '.ini', '.cfg', '.conf', '.sh', '.bash', '.zsh', '.fish',
    '.env', '.md', '.txt', '.log', '.xml', '.html', '.htm', '.css',
    '.csv', '.sql', '.rs', '.go', '.c', '.cpp', '.h', '.java', '.rb',
    '.php', '.pl', '.r', '.tf', '.hcl', '.nix', '.dockerfile',
    '.gitignore', '.gitattributes', '.editorconfig', '.htaccess',
    '.service', '.timer', '.socket', '.target', '.mount',
}


def _auth():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    return None


def _resolve(raw):
    if not raw:
        return '/'
    return os.path.realpath(os.path.normpath(raw))


# ── Sudo helpers ──────────────────────────────────────────────────────────────

def _sudo_active():
    """True if the session has a live sudo grant."""
    return _time.time() < session.get('sudo_until', 0)


def _sudo(args, stdin=None):
    """Run ['sudo', '-n', ...args] and return (stdout_bytes, stderr_str, ok)."""
    try:
        r = subprocess.run(
            ['sudo', '-n'] + args,
            input=stdin,
            capture_output=True,
            timeout=30,
        )
        return r.stdout, r.stderr.decode(errors='replace').strip(), r.returncode == 0
    except subprocess.TimeoutExpired:
        return b'', 'Timed out', False
    except Exception as e:
        return b'', str(e), False


# ── Sudo session endpoints ────────────────────────────────────────────────────

@files_bp.route('/api/files/sudo/status')
def sudo_status():
    err = _auth()
    if err:
        return err
    until   = session.get('sudo_until', 0)
    active  = _time.time() < until
    return jsonify({
        'active':    active,
        'remaining': max(0, int(until - _time.time())) if active else 0,
    })


@files_bp.route('/api/files/sudo/enable', methods=['POST'])
def sudo_enable():
    err = _auth()
    if err:
        return err
    password = (request.get_json() or {}).get('password', '')
    if not password:
        return jsonify({'error': 'Password required'}), 400
    try:
        r = subprocess.run(
            ['sudo', '-S', '-v'],
            input=(password + '\n').encode(),
            capture_output=True,
            timeout=10,
        )
        if r.returncode != 0:
            return jsonify({'error': 'Incorrect password or sudo not permitted'}), 403
        until = int(_time.time()) + SUDO_DURATION
        session['sudo_until'] = until
        session.modified = True
        return jsonify({'ok': True, 'remaining': SUDO_DURATION})
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Authentication timed out'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@files_bp.route('/api/files/sudo/disable', methods=['POST'])
def sudo_disable():
    err = _auth()
    if err:
        return err
    session.pop('sudo_until', None)
    session.modified = True
    subprocess.run(['sudo', '-k'], capture_output=True)   # drop OS ticket too
    return jsonify({'ok': True})


def _is_text(path):
    ext = os.path.splitext(path.lower())[1]
    if ext in TEXT_EXTENSIONS:
        return True
    if os.path.basename(path).lower() in {
        'dockerfile', 'makefile', 'rakefile', 'vagrantfile',
        'readme', 'license', 'authors', 'changelog',
    }:
        return True
    mime, _ = mimetypes.guess_type(path)
    return bool(mime and mime.startswith('text/'))


def _entry(path):
    try:
        st = os.lstat(path)
    except OSError:
        return None
    mode    = st.st_mode
    is_link = stat.S_ISLNK(mode)
    is_dir  = stat.S_ISDIR(mode)
    link_to = None
    if is_link:
        try:
            link_to = os.readlink(path)
        except OSError:
            pass
    return {
        'name':     os.path.basename(path),
        'path':     path,
        'type':     'dir' if is_dir else ('link' if is_link else 'file'),
        'size':     None if is_dir else st.st_size,
        'modified': int(st.st_mtime),
        'mode':     oct(stat.S_IMODE(mode)),
        'link_to':  link_to,
    }


# ── List directory ────────────────────────────────────────────────────────────

@files_bp.route('/api/files/list')
def list_dir():
    err = _auth()
    if err:
        return err
    path = _resolve(request.args.get('path', '/'))
    if not os.path.exists(path):
        # Try sudo stat to confirm the path really exists
        if _sudo_active():
            _, _, ok = _sudo(['test', '-e', path])
            if not ok:
                return jsonify({'error': 'Path not found'}), 404
        else:
            return jsonify({'error': 'Path not found'}), 404
    if not os.path.isdir(path):
        return jsonify({'error': 'Not a directory'}), 400
    try:
        entries = []
        with os.scandir(path) as it:
            for e in it:
                info = _entry(e.path)
                if info:
                    entries.append(info)
        entries.sort(key=lambda x: (0 if x['type'] == 'dir' else 1, x['name'].lower()))
        parent = str(Path(path).parent) if path != '/' else None
        return jsonify({'path': path, 'parent': parent, 'entries': entries, 'sudo': False})
    except PermissionError:
        if not _sudo_active():
            return jsonify({'error': 'Permission denied — enable Sudo mode to access this directory'}), 403
        # Use sudo find to enumerate entries
        stdout, stderr, ok = _sudo([
            'find', path, '-maxdepth', '1', '-mindepth', '1',
            '-exec', 'stat', '--format=%n\t%F\t%s\t%Y\t%a', '{}', ';'
        ])
        if not ok:
            return jsonify({'error': stderr or 'Permission denied'}), 403
        entries = []
        for line in stdout.decode(errors='replace').splitlines():
            parts = line.split('\t')
            if len(parts) < 5:
                continue
            fpath, ftype, fsize, mtime, mode = parts[:5]
            is_dir = ftype in ('directory', 'symbolic link to directory')
            entries.append({
                'name':     os.path.basename(fpath),
                'path':     fpath,
                'type':     'dir' if is_dir else 'file',
                'size':     None if is_dir else int(fsize),
                'modified': int(mtime),
                'mode':     '0o' + mode,
                'link_to':  None,
            })
        entries.sort(key=lambda x: (0 if x['type'] == 'dir' else 1, x['name'].lower()))
        parent = str(Path(path).parent) if path != '/' else None
        return jsonify({'path': path, 'parent': parent, 'entries': entries, 'sudo': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Read text file ────────────────────────────────────────────────────────────

@files_bp.route('/api/files/read')
def read_file():
    err = _auth()
    if err:
        return err
    path = _resolve(request.args.get('path', ''))
    if not path:
        return jsonify({'error': 'path required'}), 400
    try:
        if not os.path.isfile(path):
            return jsonify({'error': 'Not a file'}), 400
        size = os.path.getsize(path)
        if not _is_text(path):
            return jsonify({'error': 'Binary file', 'binary': True, 'size': size}), 422
        if size > MAX_READ_BYTES:
            return jsonify({'error': f'File too large ({size} bytes, max 3 MB)', 'size': size}), 413
        with open(path, 'r', errors='replace') as f:
            content = f.read()
        return jsonify({'path': path, 'content': content, 'size': size, 'sudo': False})
    except PermissionError:
        if not _sudo_active():
            return jsonify({'error': 'Permission denied — enable Sudo mode to read this file'}), 403
        stdout, stderr, ok = _sudo(['cat', path])
        if not ok:
            return jsonify({'error': stderr or 'Permission denied'}), 403
        content = stdout.decode(errors='replace')
        if not _is_text(path) and b'\x00' in stdout:
            return jsonify({'error': 'Binary file', 'binary': True}), 422
        return jsonify({'path': path, 'content': content, 'size': len(stdout), 'sudo': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Write text file ───────────────────────────────────────────────────────────

@files_bp.route('/api/files/write', methods=['POST'])
def write_file():
    err = _auth()
    if err:
        return err
    data    = request.get_json() or {}
    path    = _resolve(data.get('path', ''))
    content = data.get('content', '')
    if not path or path == '/':
        return jsonify({'error': 'path required'}), 400
    try:
        with open(path, 'w') as f:
            f.write(content)
        return jsonify({'ok': True, 'path': path, 'sudo': False})
    except PermissionError:
        if not _sudo_active():
            return jsonify({'error': 'Permission denied — enable Sudo mode to write this file'}), 403
        _, stderr, ok = _sudo(['tee', path], stdin=content.encode())
        if not ok:
            return jsonify({'error': stderr or 'Permission denied'}), 403
        return jsonify({'ok': True, 'path': path, 'sudo': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Create directory ──────────────────────────────────────────────────────────

@files_bp.route('/api/files/mkdir', methods=['POST'])
def mkdir():
    err = _auth()
    if err:
        return err
    data = request.get_json() or {}
    path = _resolve(data.get('path', ''))
    if not path or path == '/':
        return jsonify({'error': 'path required'}), 400
    try:
        os.makedirs(path, exist_ok=False)
        return jsonify({'ok': True, 'path': path})
    except FileExistsError:
        return jsonify({'error': 'Already exists'}), 409
    except PermissionError:
        if not _sudo_active():
            return jsonify({'error': 'Permission denied — enable Sudo mode'}), 403
        _, stderr, ok = _sudo(['mkdir', '-p', path])
        if not ok:
            return jsonify({'error': stderr or 'Permission denied'}), 403
        return jsonify({'ok': True, 'path': path, 'sudo': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Rename / move ─────────────────────────────────────────────────────────────

@files_bp.route('/api/files/rename', methods=['POST'])
def rename():
    err = _auth()
    if err:
        return err
    data = request.get_json() or {}
    src  = _resolve(data.get('src', ''))
    dst  = _resolve(data.get('dst', ''))
    if not src or not dst:
        return jsonify({'error': 'src and dst required'}), 400
    if not os.path.exists(src):
        return jsonify({'error': 'Source not found'}), 404
    try:
        os.rename(src, dst)
        return jsonify({'ok': True, 'dst': dst})
    except PermissionError:
        if not _sudo_active():
            return jsonify({'error': 'Permission denied — enable Sudo mode'}), 403
        _, stderr, ok = _sudo(['mv', src, dst])
        if not ok:
            return jsonify({'error': stderr or 'Permission denied'}), 403
        return jsonify({'ok': True, 'dst': dst, 'sudo': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Delete ────────────────────────────────────────────────────────────────────

@files_bp.route('/api/files/delete', methods=['POST'])
def delete():
    err = _auth()
    if err:
        return err
    data = request.get_json() or {}
    path = _resolve(data.get('path', ''))
    if not path or path == '/':
        return jsonify({'error': 'Invalid path'}), 400
    if not os.path.lexists(path):
        return jsonify({'error': 'Not found'}), 404
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
        return jsonify({'ok': True})
    except PermissionError:
        if not _sudo_active():
            return jsonify({'error': 'Permission denied — enable Sudo mode'}), 403
        cmd = ['rm', '-rf', path] if os.path.isdir(path) else ['rm', path]
        _, stderr, ok = _sudo(cmd)
        if not ok:
            return jsonify({'error': stderr or 'Permission denied'}), 403
        return jsonify({'ok': True, 'sudo': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Download ──────────────────────────────────────────────────────────────────

@files_bp.route('/api/files/download')
def download():
    err = _auth()
    if err:
        return err
    path = _resolve(request.args.get('path', ''))
    if not path or not os.path.isfile(path):
        return jsonify({'error': 'Not a file'}), 400
    try:
        return send_file(path, as_attachment=True,
                         download_name=os.path.basename(path))
    except PermissionError:
        if not _sudo_active():
            return jsonify({'error': 'Permission denied — enable Sudo mode'}), 403
        stdout, stderr, ok = _sudo(['cat', path])
        if not ok:
            return jsonify({'error': stderr or 'Permission denied'}), 403
        from flask import Response
        return Response(
            stdout,
            headers={'Content-Disposition': f'attachment; filename="{os.path.basename(path)}"'},
            mimetype='application/octet-stream',
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Upload ────────────────────────────────────────────────────────────────────

@files_bp.route('/api/files/upload', methods=['POST'])
def upload():
    err = _auth()
    if err:
        return err
    dest_dir = _resolve(request.form.get('path', '/'))
    if not os.path.isdir(dest_dir):
        return jsonify({'error': 'Destination not a directory'}), 400
    files = request.files.getlist('files')
    if not files:
        return jsonify({'error': 'No files provided'}), 400
    saved = []
    import tempfile
    try:
        for f in files:
            name = secure_filename(f.filename)
            if not name:
                continue
            dest = os.path.join(dest_dir, name)
            try:
                f.save(dest)
                saved.append(name)
            except PermissionError:
                if not _sudo_active():
                    raise
                # Save to temp, then sudo mv into place
                with tempfile.NamedTemporaryFile(delete=False) as tmp:
                    f.save(tmp.name)
                    _, stderr, ok = _sudo(['cp', tmp.name, dest])
                    os.unlink(tmp.name)
                    if not ok:
                        return jsonify({'error': stderr or 'Permission denied'}), 403
                saved.append(name)
        return jsonify({'ok': True, 'saved': saved})
    except PermissionError:
        return jsonify({'error': 'Permission denied — enable Sudo mode'}), 403
    except Exception as e:
        return jsonify({'error': str(e)}), 500
