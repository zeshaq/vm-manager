import xml.etree.ElementTree as ET
from xml.dom import minidom
import os

PROJECTS_FILE = 'projects.xml'

def load_projects():
    if not os.path.exists(PROJECTS_FILE):
        return {}
    tree = ET.parse(PROJECTS_FILE)
    root = tree.getroot()
    projects = {}
    for project in root.findall('project'):
        name = project.get('name')
        projects[name] = [vm.text for vm in project.findall('vm')]
    return projects

def save_projects(projects):
    root = ET.Element('projects')
    for name, vms in projects.items():
        project = ET.SubElement(root, 'project', name=name)
        for vm_uuid in vms:
            vm_element = ET.SubElement(project, 'vm')
            vm_element.text = vm_uuid
    
    # Prettify the XML output
    xml_str = ET.tostring(root, 'utf-8')
    pretty_xml_str = minidom.parseString(xml_str).toprettyxml(indent="   ")
    
    with open(PROJECTS_FILE, 'w') as f:
        f.write(pretty_xml_str)

def add_project(name):
    projects = load_projects()
    if name not in projects:
        projects[name] = []
        save_projects(projects)

def remove_project(name):
    projects = load_projects()
    if name in projects:
        del projects[name]
        save_projects(projects)

def add_vm_to_project(project_name, vm_uuid):
    projects = load_projects()
    if project_name in projects and vm_uuid not in projects[project_name]:
        projects[project_name].append(vm_uuid)
        save_projects(projects)

def remove_vm_from_project(project_name, vm_uuid):
    projects = load_projects()
    if project_name in projects and vm_uuid in projects[project_name]:
        projects[project_name].remove(vm_uuid)
        save_projects(projects)
