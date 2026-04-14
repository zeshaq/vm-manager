import { useEffect, useState, useRef } from 'react'
import { HardDrive, Upload, Plus, Trash2, RefreshCw, File } from 'lucide-react'
import api from '../api'

export default function Storage() {
  const [data, setData] = useState({ files: [], storage_path: '' })
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [diskForm, setDiskForm] = useState({ name: '', size: '20', format: 'qcow2' })
  const [creating, setCreating] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [deleting, setDeleting] = useState({})
  const fileRef = useRef(null)

  const fetchStorage = () => {
    setLoading(true)
    api.get('/storage')
      .then(r => setData(r.data))
      .catch(e => setError(e.response?.data?.error || 'Failed to load storage'))
      .finally(() => setLoading(false))
  }

  useEffect(() => { fetchStorage() }, [])

  const handleCreateDisk = async e => {
    e.preventDefault()
    setCreating(true)
    try {
      await api.post('/storage/disks', diskForm)
      setDiskForm({ name: '', size: '20', format: 'qcow2' })
      fetchStorage()
    } catch (err) {
      alert(err.response?.data?.error || 'Failed to create disk')
    } finally {
      setCreating(false)
    }
  }

  const handleUpload = async e => {
    const file = e.target.files[0]
    if (!file) return
    setUploading(true)
    try {
      const formData = new FormData()
      formData.append('file', file)
      await api.post('/storage/upload', formData, {
        headers: { 'Content-Type': 'multipart/form-data' }
      })
      fetchStorage()
    } catch (err) {
      alert(err.response?.data?.error || 'Upload failed')
    } finally {
      setUploading(false)
      e.target.value = ''
    }
  }

  const handleDelete = async filename => {
    if (!confirm(`Delete ${filename}?`)) return
    setDeleting(p => ({ ...p, [filename]: true }))
    try {
      await api.delete('/storage/files', { data: { filename } })
      fetchStorage()
    } catch (err) {
      alert(err.response?.data?.error || 'Delete failed')
    } finally {
      setDeleting(p => ({ ...p, [filename]: false }))
    }
  }

  const typeColor = type => {
    if (type === 'iso') return 'bg-blue-900 text-blue-300'
    if (type === 'qcow2') return 'bg-green-900 text-green-300'
    if (type === 'img') return 'bg-yellow-900 text-yellow-300'
    return 'bg-navy-500 text-slate-400'
  }

  if (loading) return <div className="text-sky-400 text-center py-20">Loading storage...</div>
  if (error) return <div className="text-red-400 text-center py-20">{error}</div>

  return (
    <div className="space-y-5">
      {/* Top forms */}
      <div className="grid md:grid-cols-2 gap-5">
        {/* Create Disk */}
        <div className="bg-navy-700 border border-navy-400 rounded-xl p-5">
          <h3 className="text-sky-400 font-semibold mb-4 flex items-center gap-2">
            <Plus size={16} /> Create Disk Image
          </h3>
          <form onSubmit={handleCreateDisk} className="space-y-3">
            <div>
              <label className="block text-xs text-slate-400 mb-1">Name</label>
              <input
                type="text"
                value={diskForm.name}
                onChange={e => setDiskForm(p => ({ ...p, name: e.target.value }))}
                placeholder="disk-name (extension added automatically)"
                className="w-full bg-navy-800 border border-navy-400 text-slate-200 focus:border-sky-500 focus:outline-none rounded-md px-3 py-2 text-sm"
                required
              />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block text-xs text-slate-400 mb-1">Size (GB)</label>
                <input
                  type="number"
                  value={diskForm.size}
                  onChange={e => setDiskForm(p => ({ ...p, size: e.target.value }))}
                  min="1"
                  className="w-full bg-navy-800 border border-navy-400 text-slate-200 focus:border-sky-500 focus:outline-none rounded-md px-3 py-2 text-sm"
                  required
                />
              </div>
              <div>
                <label className="block text-xs text-slate-400 mb-1">Format</label>
                <select
                  value={diskForm.format}
                  onChange={e => setDiskForm(p => ({ ...p, format: e.target.value }))}
                  className="w-full bg-navy-800 border border-navy-400 text-slate-200 focus:border-sky-500 focus:outline-none rounded-md px-3 py-2 text-sm"
                >
                  <option value="qcow2">qcow2</option>
                  <option value="raw">raw</option>
                </select>
              </div>
            </div>
            <button
              type="submit"
              disabled={creating}
              className="w-full flex items-center justify-center gap-2 bg-sky-500 hover:bg-sky-400 disabled:opacity-50 text-white px-4 py-2.5 rounded-md text-sm font-medium transition-colors"
            >
              <HardDrive size={14} /> {creating ? 'Creating...' : 'Create Disk'}
            </button>
          </form>
        </div>

        {/* Upload ISO */}
        <div className="bg-navy-700 border border-navy-400 rounded-xl p-5">
          <h3 className="text-sky-400 font-semibold mb-4 flex items-center gap-2">
            <Upload size={16} /> Upload File
          </h3>
          <div className="space-y-3">
            <div
              onClick={() => fileRef.current?.click()}
              className="border-2 border-dashed border-navy-300 hover:border-sky-500 rounded-xl p-8 text-center cursor-pointer transition-colors"
            >
              <Upload size={24} className="text-slate-400 mx-auto mb-2" />
              <p className="text-slate-300 text-sm font-medium">Click to select file</p>
              <p className="text-slate-500 text-xs mt-1">.iso, .img, .qcow2</p>
            </div>
            <input
              ref={fileRef}
              type="file"
              accept=".iso,.img,.qcow2"
              onChange={handleUpload}
              className="hidden"
            />
            {uploading && (
              <div className="bg-navy-800 rounded-lg px-4 py-3 text-center text-sky-400 text-sm animate-pulse">
                Uploading...
              </div>
            )}
          </div>
        </div>
      </div>

      {/* File list */}
      <div className="bg-navy-700 border border-navy-400 rounded-xl overflow-hidden">
        <div className="flex items-center justify-between px-5 py-4 border-b border-navy-500">
          <div>
            <h3 className="text-sky-400 font-semibold flex items-center gap-2">
              <File size={16} /> Storage Files
            </h3>
            <p className="text-slate-500 text-xs mt-0.5">{data.storage_path}</p>
          </div>
          <button onClick={fetchStorage} className="text-slate-400 hover:text-sky-400 transition-colors">
            <RefreshCw size={14} />
          </button>
        </div>

        {data.files.length === 0 ? (
          <div className="text-center py-12 text-slate-400 text-sm">No files found in storage directory.</div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-navy-800 text-sky-400">
                <th className="px-4 py-3 text-left">Name</th>
                <th className="px-4 py-3 text-left">Type</th>
                <th className="px-4 py-3 text-right">Size</th>
                <th className="px-4 py-3 text-left hidden md:table-cell">Path</th>
                <th className="px-4 py-3"></th>
              </tr>
            </thead>
            <tbody>
              {data.files.map(file => (
                <tr key={file.name} className="border-b border-navy-500 hover:bg-navy-600 transition-colors">
                  <td className="px-4 py-3 text-slate-200 font-medium">{file.name}</td>
                  <td className="px-4 py-3">
                    <span className={`px-2 py-0.5 rounded text-xs font-medium ${typeColor(file.type)}`}>
                      {file.type}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-right text-slate-400">{file.size}</td>
                  <td className="px-4 py-3 text-slate-500 text-xs truncate max-w-xs hidden md:table-cell">{file.path}</td>
                  <td className="px-4 py-3 text-right">
                    <button
                      onClick={() => handleDelete(file.name)}
                      disabled={deleting[file.name]}
                      className="p-1.5 rounded bg-red-900/60 hover:bg-red-800 text-red-400 transition-colors disabled:opacity-50"
                    >
                      <Trash2 size={13} />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="text-slate-400 text-sm">{data.files.length} file{data.files.length !== 1 ? 's' : ''}</div>
    </div>
  )
}
