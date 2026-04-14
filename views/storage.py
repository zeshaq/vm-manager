import os
import subprocess
from flask import Blueprint
from werkzeug.utils import secure_filename

# Storage listing/creation/upload/delete is handled by /api/storage/* (views/api.py).
# This blueprint is kept so the import in app.py remains valid.
storage_bp = Blueprint('storage', __name__)

STORAGE_PATH = '/var/lib/libvirt/images'
ALLOWED_EXTENSIONS = {'iso', 'img', 'qcow2'}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def get_human_readable_size(size_in_bytes):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_in_bytes < 1024.0:
            return f"{size_in_bytes:.2f} {unit}"
        size_in_bytes /= 1024.0
    return f"{size_in_bytes:.2f} TB"
