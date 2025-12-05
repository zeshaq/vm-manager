import os
import subprocess
from flask import Blueprint, render_template, request, redirect, url_for, flash
from werkzeug.utils import secure_filename

storage_bp = Blueprint('storage', __name__)

# CONFIGURATION
STORAGE_PATH = '/var/lib/libvirt/images'
ALLOWED_EXTENSIONS = {'iso', 'img', 'qcow2'}

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_human_readable_size(size_in_bytes):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_in_bytes < 1024.0:
            return f"{size_in_bytes:.2f} {unit}"
        size_in_bytes /= 1024.0
    return f"{size_in_bytes:.2f} TB"

@storage_bp.route('/storage')
def list_storage():
    files = []
    if not os.path.exists(STORAGE_PATH):
        return f"Error: {STORAGE_PATH} not found."

    try:
        # List all files and sort them alphabetically
        dir_list = os.listdir(STORAGE_PATH)
        dir_list.sort()  # <--- Added sorting here

        for filename in dir_list:
            full_path = os.path.join(STORAGE_PATH, filename)
            if os.path.isfile(full_path):
                stats = os.stat(full_path)
                ext = filename.split('.')[-1].lower() if '.' in filename else 'raw'
                files.append({
                    'name': filename,
                    'path': full_path,
                    'size': get_human_readable_size(stats.st_size),
                    'type': ext
                })
    except Exception as e:
        print(f"Error: {e}")

    return render_template('storage.html', files=files, storage_path=STORAGE_PATH)

@storage_bp.route('/storage/create', methods=['POST'])
def create_disk():
    name = request.form.get('name')
    size_gb = request.form.get('size')
    fmt = request.form.get('format', 'qcow2')
    
    if not name or not size_gb: return "Missing Data"

    safe_name = secure_filename(name)
    if not safe_name.endswith(f'.{fmt}'):
        safe_name += f'.{fmt}'
    
    full_path = os.path.join(STORAGE_PATH, safe_name)
    if os.path.exists(full_path): return "File Exists"

    try:
        subprocess.run(['qemu-img', 'create', '-f', fmt, full_path, f'{size_gb}G'], check=True)
    except Exception as e: return f"Error: {e}"

    return redirect(url_for('storage.list_storage'))

@storage_bp.route('/storage/upload', methods=['POST'])
def upload_iso():
    if 'file' not in request.files:
        return "No file part"
        
    file = request.files['file']
    if file.filename == '':
        return "No selected file"
        
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        save_path = os.path.join(STORAGE_PATH, filename)
        
        try:
            file.save(save_path)
        except Exception as e:
            return f"Error saving file: {e}"
            
        return redirect(url_for('storage.list_storage'))
    
    return "Invalid file type. Only .iso, .img, .qcow2 allowed."

@storage_bp.route('/storage/delete', methods=['POST'])
def delete_disk():
    filename = request.form.get('filename')
    if not filename: return "No filename"
    
    safe_name = secure_filename(os.path.basename(filename))
    full_path = os.path.join(STORAGE_PATH, safe_name)
    
    if os.path.exists(full_path):
        os.remove(full_path)
    
    return redirect(url_for('storage.list_storage'))