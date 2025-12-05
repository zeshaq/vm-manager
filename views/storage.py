import os
import subprocess
from flask import Blueprint, render_template, request, redirect, url_for, flash
from werkzeug.utils import secure_filename

storage_bp = Blueprint('storage', __name__)

# CONFIGURATION: The default path where KVM stores images
# Ensure the user running this script has WRITE permissions to this folder.
STORAGE_PATH = '/var/lib/libvirt/images'

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
        return f"Error: Storage path {STORAGE_PATH} does not exist. Please check configuration."

    try:
        # List all files in the directory
        for filename in os.listdir(STORAGE_PATH):
            full_path = os.path.join(STORAGE_PATH, filename)
            
            if os.path.isfile(full_path):
                # Get file stats
                stats = os.stat(full_path)
                size_str = get_human_readable_size(stats.st_size)
                
                # Determine type based on extension
                ext = filename.split('.')[-1].lower() if '.' in filename else 'raw'
                
                files.append({
                    'name': filename,
                    'path': full_path,
                    'size': size_str,
                    'type': ext
                })
    except Exception as e:
        print(f"Error access storage: {e}")

    return render_template('storage.html', files=files, storage_path=STORAGE_PATH)

@storage_bp.route('/storage/create', methods=['POST'])
def create_disk():
    name = request.form.get('name')
    size_gb = request.form.get('size')
    fmt = request.form.get('format', 'qcow2')
    
    if not name or not size_gb:
        return "Missing Name or Size"

    # Sanitize filename to prevent directory traversal
    safe_name = secure_filename(name)
    if not safe_name.endswith(f'.{fmt}'):
        safe_name += f'.{fmt}'
        
    full_path = os.path.join(STORAGE_PATH, safe_name)

    if os.path.exists(full_path):
        return "File already exists!"

    # Use qemu-img to create the disk
    # Command: qemu-img create -f qcow2 /path/to/file.qcow2 20G
    try:
        cmd = ['qemu-img', 'create', '-f', fmt, full_path, f'{size_gb}G']
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        return f"Error creating disk: {e}"
    except FileNotFoundError:
        return "Error: 'qemu-img' not installed on server."

    return redirect(url_for('storage.list_storage'))

@storage_bp.route('/storage/delete', methods=['POST'])
def delete_disk():
    filename = request.form.get('filename')
    
    if not filename:
        return "No filename provided"
        
    # Security: Ensure we only delete files inside our STORAGE_PATH
    safe_name = secure_filename(os.path.basename(filename))
    full_path = os.path.join(STORAGE_PATH, safe_name)
    
    if os.path.exists(full_path):
        try:
            os.remove(full_path)
        except Exception as e:
            return f"Error deleting file: {e}"
    
    return redirect(url_for('storage.list_storage'))
```json