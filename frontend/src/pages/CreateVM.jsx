import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { PlusCircle, Cpu, HardDrive, Trash2, Plus } from 'lucide-react'
import api from '../api'

const EMPTY_DISK = { path: '', size_gb: '20' }

export default function CreateVM() {
  const navigate = useNavigate()
  const [form, setForm] = useState({
    name: '',
    ram: '2048',
    cpu: '2',
    host_cpu: false,
    devices: [],
  })
  const [disks, setDisks] = useState([{ ...EMPTY_DISK }])
  const [loading, setLoading] = useState(false)
  const [error, setError]   = useState('')

  const set = (k, v) => setForm(p => ({ ...p, [k]: v }))

  // ── disk helpers ────────────────────────────────────────────────────────────
  const setDisk = (idx, field, value) =>
    setDisks(prev => prev.map((d, i) => i === idx ? { ...d, [field]: value } : d))

  const addDisk = () => setDisks(prev => [...prev, { ...EMPTY_DISK }])

  const removeDisk = idx =>
    setDisks(prev => prev.filter((_, i) => i !== idx))

  const isIso = path => path.trim().toLowerCase().endsWith('.iso')

  // ── submit ──────────────────────────────────────────────────────────────────
  const handleSubmit = async e => {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      const diskPayload = disks
        .filter(d => d.path.trim())
        .map(d => ({
          path:    d.path.trim(),
          size_gb: parseInt(d.size_gb) || 20,
        }))

      const res = await api.post('/vms', {
        name:     form.name,
        ram:      parseInt(form.ram),
        cpu:      parseInt(form.cpu),
        host_cpu: form.host_cpu,
        devices:  form.devices,
        disks:    diskPayload,
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

          {/* ── Disks ───────────────────────────────────────────────────────── */}
          <div>
            <div className="flex items-center justify-between mb-2">
              <label className="text-sm font-medium text-slate-300 flex items-center gap-2">
                <HardDrive size={14} className="text-sky-400" />
                Disks
                <span className="text-slate-500 font-normal">(optional)</span>
              </label>
              <button
                type="button"
                onClick={addDisk}
                className="flex items-center gap-1 text-xs text-sky-400 hover:text-sky-300 transition-colors"
              >
                <Plus size={13} /> Add Disk
              </button>
            </div>

            <div className="space-y-2">
              {disks.map((disk, idx) => {
                const iso = isIso(disk.path)
                return (
                  <div key={idx} className="flex gap-2 items-start">
                    {/* Path */}
                    <div className="flex-1">
                      <input
                        type="text"
                        value={disk.path}
                        onChange={e => setDisk(idx, 'path', e.target.value)}
                        placeholder={idx === 0
                          ? '/var/lib/libvirt/images/base.qcow2 or ubuntu.iso'
                          : '/var/lib/libvirt/images/data.qcow2'}
                        className="w-full bg-navy-800 border border-navy-400 text-slate-200 focus:border-sky-500 focus:outline-none rounded-md px-3 py-2 text-sm font-mono"
                      />
                      {iso && (
                        <p className="text-amber-400 text-xs mt-0.5">
                          ISO detected — attached as read-only CD-ROM
                        </p>
                      )}
                    </div>

                    {/* Size — hidden for ISOs */}
                    {!iso && (
                      <div className="w-28">
                        <div className="flex items-center">
                          <input
                            type="number"
                            value={disk.size_gb}
                            onChange={e => setDisk(idx, 'size_gb', e.target.value)}
                            min="1"
                            max="65536"
                            className="w-full bg-navy-800 border border-navy-400 text-slate-200 focus:border-sky-500 focus:outline-none rounded-md px-3 py-2 text-sm"
                          />
                          <span className="ml-1.5 text-slate-500 text-xs whitespace-nowrap">GB</span>
                        </div>
                      </div>
                    )}

                    {/* Remove button (only if more than one disk) */}
                    {disks.length > 1 && (
                      <button
                        type="button"
                        onClick={() => removeDisk(idx)}
                        className="mt-2 text-slate-500 hover:text-red-400 transition-colors"
                        title="Remove disk"
                      >
                        <Trash2 size={15} />
                      </button>
                    )}
                  </div>
                )
              })}
            </div>

            <p className="text-slate-500 text-xs mt-1.5">
              qcow2/img base images create a per-VM overlay. ISOs attach as CD-ROM.
              Multiple disks attach as <code className="text-slate-400">vda</code>, <code className="text-slate-400">vdb</code>, …
            </p>
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
