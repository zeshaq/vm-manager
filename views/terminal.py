import os
import subprocess
from flask import Blueprint, render_template, request
from flask import current_app as app
from flask_socketio import SocketIO

terminal_bp = Blueprint('terminal', __name__)

@terminal_bp.route('/terminal')
def terminal():
    vm_name = request.args.get('vm_name')
    if not vm_name:
        return "Missing vm_name parameter", 400
    return render_template('terminal.html', vm_name=vm_name)

def create_terminal_socket(socketio):
    @socketio.on('connect', namespace='/terminal')
    def connect():
        print("Client connected to terminal")

    @socketio.on('disconnect', namespace='/terminal')
    def disconnect():
        print("Client disconnected from terminal")

    @socketio.on('execute', namespace='/terminal')
    def execute_command(data):
        command = data.get('command')
        if not command:
            return

        try:
            # IMPORTANT: This is a placeholder. In a real application,
            # you would use virsh or another method to execute commands
            # *inside* the specified VM.
            # For now, we execute on the host.
            result = subprocess.check_output(
                f"virsh -c qemu:///system console {data.get('vm_name')}",
                shell=True,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                cwd=os.path.expanduser("~")
            )
            socketio.emit('response', {'data': result}, namespace='/terminal')
        except subprocess.CalledProcessError as e:
            socketio.emit('response', {'data': e.output}, namespace='/terminal')

def setup_terminal(app):
    socketio = SocketIO(app)
    create_terminal_socket(socketio)
    return socketio
