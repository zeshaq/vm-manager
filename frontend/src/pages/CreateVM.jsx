import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { PlusCircle, Cpu } from 'lucide-react'
import api from '../api'

export default function CreateVM() {
  const navigate = useNavigate()
  const [form, setForm] = useState({
    name: '',
    ram: '2048',
    cpu: '2',
    host_cpu: false,
    devices: [],
  })
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const set = (k, v) => setForm(p => ({ ...p, [k]: v }))

  const handleSubmit = async e => {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      const res = await api.post('/vms', {
        name: form.name,
        ram: parseInt(form.ram),
        cpu: parseInt(form.cpu),
        host_cpu: form.host_cpu,
        devices: form.devices,
      })
      navigate(`/vms/${res.data.uuid}`)
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to create VM')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="max-w-2xl">
      <div className="bg-navy-700 border border-navy-400 rounded-xl p-6">
        <h2 className="text-sky-400 font-semibold text-lg mb-6 flex items-center gap-2">
          <PlusCircle size={18} /> New Virtual Machine
        </h2>

        {error && (
          <div className="bg-red-900/50 border border-red-700 text-red-300 text-sm rounded-md px-4 py-3 mb-5">
            {error}
          </div>
        )}

        <form onSubmit={handleSubmit} className="space-y-5">

          {/* VM Name */}
          <div>
            <label className="block text-sm font-medium text-slate-300 mb-1.5">
              VM Name <span className="text-red-400">*</span>
            </label>
            <input
              type="text"
              value={form.name}
              onChange={e => set('name', e.target.value)}
              placeholder="e.g. my-ubuntu-vm"
              className="w-full bg-navy-800 border border-navy-400 text-slate-200 focus:border-sky-500 focus:outline-none rounded-md px-3 py-2"
              required
            />
          </div>

          {/* RAM and CPU */}
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-slate-300 mb-1.5">
                RAM (MB) <span className="text-red-400">*</span>
              </label>
              <input
                type="number"
                value={form.ram}
                onChange={e => set('ram', e.target.value)}
                min="256"
                step="256"
                className="w-full bg-navy-800 border border-navy-400 text-slate-200 focus:border-sky-500 focus:outline-none rounded-md px-3 py-2"
                required
              />
              <p className="text-slate-500 text-xs mt-1">
                {Math.round(form.ram / 1024 * 10) / 10} GB
              </p>
            </div>
            <div>
              <label className="block text-sm font-medium text-slate-300 mb-1.5">
                vCPUs <span className="text-red-400">*</span>
              </label>
              <input
                type="number"
                value={form.cpu}
                onChange={e => set('cpu', e.target.value)}
                min="1"
                max="128"
                className="w-full bg-navy-800 border border-navy-400 text-slate-200 focus:border-sky-500 focus:outline-none rounded-md px-3 py-2"
                required
              />
            </div>
          </div>

          {/* Host CPU passthrough */}
          <div className="flex items-center gap-3">
            <input
              type="checkbox"
              id="host_cpu"
              checked={form.host_cpu}
              onChange={e => set('host_cpu', e.target.checked)}
              className="w-4 h-4 rounded"
            />
            <label htmlFor="host_cpu" className="text-sm text-slate-300 cursor-pointer flex items-center gap-2">
              <Cpu size={14} className="text-sky-400" />
              Enable Host CPU Passthrough
              <span className="text-slate-500 text-xs">(host-passthrough mode)</span>
            </label>
          </div>

          {/* PCI Devices */}
          <div>
            <label className="block text-sm font-medium text-slate-300 mb-1.5">
              PCI Devices <span className="text-slate-500 font-normal">(optional)</span>
            </label>
            <textarea
              value={form.devices.join('\n')}
              onChange={e => set('devices', e.target.value.split('\n').map(s => s.trim()).filter(Boolean))}
              placeholder={'One PCI ID per line, e.g.\n0000:8a:00.0\n0000:8a:00.1'}
              rows={3}
              className="w-full bg-navy-800 border border-navy-400 text-slate-200 focus:border-sky-500 focus:outline-none rounded-md px-3 py-2 text-sm font-mono resize-none"
            />
            <p className="text-slate-500 text-xs mt-1">Format: domain:bus:slot.function</p>
          </div>

          {/* Submit */}
          <div className="flex gap-3 pt-2">
            <button
              type="submit"
              disabled={loading}
              className="flex items-center gap-2 bg-sky-500 hover:bg-sky-400 disabled:opacity-50 text-white font-semibold px-5 py-2.5 rounded-md transition-colors"
            >
              <PlusCircle size={16} />
              {loading ? 'Creating…' : 'Create VM'}
            </button>
            <button
              type="button"
              onClick={() => navigate('/vms')}
              className="bg-navy-500 hover:bg-navy-400 border border-navy-300 text-slate-300 px-5 py-2.5 rounded-md text-sm transition-colors"
            >
              Cancel
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
