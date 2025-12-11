from flask import Blueprint, render_template, request, redirect, url_for
from . import project_utils
import libvirt

projects_bp = Blueprint('projects', __name__)

@projects_bp.route('/projects')
def projects():
    projects_data = project_utils.load_projects()
    
    conn = libvirt.open('qemu:///system')
    vms_by_project = {}
    if conn:
        for project, vm_uuids in projects_data.items():
            vms_by_project[project] = []
            for uuid in vm_uuids:
                try:
                    domain = conn.lookupByUUIDString(uuid)
                    vms_by_project[project].append({'name': domain.name(), 'uuid': uuid})
                except libvirt.libvirtError:
                    # VM not found, it might have been deleted
                    pass
        conn.close()
    
    return render_template('projects.html', projects=vms_by_project)

@projects_bp.route('/create_project', methods=['POST'])
def create_project():
    project_name = request.form.get('project_name')
    if project_name:
        project_utils.add_project(project_name)
    return redirect(url_for('projects.projects'))

@projects_bp.route('/remove_project/<project_name>')
def remove_project(project_name):
    project_utils.remove_project(project_name)
    return redirect(url_for('projects.projects'))

@projects_bp.route('/add_to_project', methods=['POST'])
def add_to_project():
    project_name = request.form.get('project_name')
    vm_uuid = request.form.get('vm_uuid')
    if project_name and vm_uuid:
        project_utils.add_vm_to_project(project_name, vm_uuid)
    return redirect(url_for('listing.list_vms'))

@projects_bp.route('/remove_from_project/<project_name>/<vm_uuid>')
def remove_from_project(project_name, vm_uuid):
    project_utils.remove_vm_from_project(project_name, vm_uuid)
    return redirect(url_for('projects.projects'))
