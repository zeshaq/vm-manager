import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { FolderOpen, Plus, Trash2, RefreshCw, Server, X } from 'lucide-react'
import api from '../api'

export default function Projects() {
  const [projects, setProjects] = useState({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [newProject, setNewProject] = useState('')
  const [creating, setCreating] = useState(false)
  const [deleting, setDeleting] = useState({})

  const fetchProjects = () => {
    setLoading(true)
    api.get('/projects')
      .then(r => setProjects(r.data.projects || {}))
      .catch(e => setError(e.response?.data?.error || 'Failed to load projects'))
      .finally(() => setLoading(false))
  }

  useEffect(() => { fetchProjects() }, [])

  const handleCreate = async e => {
    e.preventDefault()
    if (!newProject.trim()) return
    setCreating(true)
    try {
      await api.post('/projects', { project_name: newProject.trim() })
      setNewProject('')
      fetchProjects()
    } catch (err) {
      alert(err.response?.data?.error || 'Failed to create project')
    } finally {
      setCreating(false)
    }
  }

  const handleDeleteProject = async name => {
    if (!confirm(`Delete project "${name}"?`)) return
    setDeleting(p => ({ ...p, [name]: true }))
    try {
      await api.delete(`/projects/${name}`)
      fetchProjects()
    } catch (err) {
      alert(err.response?.data?.error || 'Failed to delete project')
    } finally {
      setDeleting(p => ({ ...p, [name]: false }))
    }
  }

  const handleRemoveVM = async (project, uuid) => {
    setDeleting(p => ({ ...p, [`${project}-${uuid}`]: true }))
    try {
      await api.delete(`/projects/${project}/vms/${uuid}`)
      fetchProjects()
    } catch (err) {
      alert(err.response?.data?.error || 'Failed to remove VM')
    } finally {
      setDeleting(p => ({ ...p, [`${project}-${uuid}`]: false }))
    }
  }

  if (loading) return <div className="text-sky-400 text-center py-20">Loading projects...</div>
  if (error) return <div className="text-red-400 text-center py-20">{error}</div>

  const projectNames = Object.keys(projects).sort()

  return (
    <div className="space-y-5">
      {/* Create project form */}
      <div className="bg-navy-700 border border-navy-400 rounded-xl p-5">
        <h3 className="text-sky-400 font-semibold mb-4 flex items-center gap-2">
          <Plus size={16} /> New Project
        </h3>
        <form onSubmit={handleCreate} className="flex gap-3">
          <input
            type="text"
            value={newProject}
            onChange={e => setNewProject(e.target.value)}
            placeholder="Project name"
            className="flex-1 bg-navy-800 border border-navy-400 text-slate-200 focus:border-sky-500 focus:outline-none rounded-md px-3 py-2 text-sm"
            required
          />
          <button
            type="submit"
            disabled={creating}
            className="flex items-center gap-2 bg-sky-500 hover:bg-sky-400 disabled:opacity-50 text-white px-4 py-2 rounded-md text-sm font-medium"
          >
            <Plus size={14} /> {creating ? 'Creating...' : 'Create'}
          </button>
        </form>
      </div>

      {/* Header */}
      <div className="flex items-center justify-between">
        <h3 className="text-slate-200 font-semibold">{projectNames.length} Project{projectNames.length !== 1 ? 's' : ''}</h3>
        <button onClick={fetchProjects} className="flex items-center gap-1.5 text-slate-400 hover:text-sky-400 text-sm transition-colors">
          <RefreshCw size={13} /> Refresh
        </button>
      </div>

      {/* Projects list */}
      {projectNames.length === 0 ? (
        <div className="bg-navy-700 border border-navy-400 rounded-xl py-16 text-center text-slate-400">
          No projects yet. Create one above.
        </div>
      ) : (
        <div className="space-y-4">
          {projectNames.map(name => {
            const vms = projects[name] || []
            return (
              <div key={name} className="bg-navy-700 border border-navy-400 rounded-xl overflow-hidden">
                {/* Project header */}
                <div className="flex items-center justify-between px-5 py-4 bg-navy-800 border-b border-navy-500">
                  <div className="flex items-center gap-2">
                    <FolderOpen size={16} className="text-sky-400" />
                    <h4 className="text-slate-200 font-semibold">{name}</h4>
                    <span className="bg-navy-500 text-slate-400 text-xs px-2 py-0.5 rounded-full">
                      {vms.length} VM{vms.length !== 1 ? 's' : ''}
                    </span>
                  </div>
                  <button
                    onClick={() => handleDeleteProject(name)}
                    disabled={deleting[name]}
                    className="flex items-center gap-1.5 bg-red-900/60 hover:bg-red-800 text-red-400 px-3 py-1.5 rounded text-xs transition-colors disabled:opacity-50"
                  >
                    <Trash2 size={12} /> Delete Project
                  </button>
                </div>

                {/* VMs in project */}
                {vms.length === 0 ? (
                  <div className="px-5 py-4 text-slate-500 text-sm">No VMs in this project.</div>
                ) : (
                  <div className="divide-y divide-navy-500">
                    {vms.map(vm => (
                      <div key={vm.uuid} className="flex items-center justify-between px-5 py-3 hover:bg-navy-600 transition-colors">
                        <div className="flex items-center gap-3">
                          <Server size={14} className="text-slate-400" />
                          <Link to={`/vms/${vm.uuid}`} className="text-slate-200 hover:text-sky-400 text-sm font-medium transition-colors">
                            {vm.name}
                          </Link>
                          <span className="text-slate-500 text-xs font-mono hidden md:inline">{vm.uuid}</span>
                        </div>
                        <button
                          onClick={() => handleRemoveVM(name, vm.uuid)}
                          disabled={deleting[`${name}-${vm.uuid}`]}
                          title="Remove from project"
                          className="p-1.5 rounded text-slate-400 hover:text-red-400 hover:bg-red-900/30 transition-colors disabled:opacity-50"
                        >
                          <X size={14} />
                        </button>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
