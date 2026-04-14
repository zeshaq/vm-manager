import { useEffect, useState } from 'react'
import { useParams, Link, useNavigate } from 'react-router-dom'
import {
  Play, Square, Pencil, RefreshCw, HardDrive, Network, Cpu, Camera,
  Trash2, PlusCircle, Monitor, Terminal, ChevronUp, ChevronDown, Save
} from 'lucide-react'
import api from '../api'

function StateBadge({ state }) {
  const variants = {
    Running: 'bg-green-900 text-green-300',
    Shutoff: 'bg-navy-500 text-slate-400',
    Paused: 'bg-yellow-900 text-yellow-300',
    Crashed: 'bg-red-900 text-red-300',
  }
  const cls = variants[state] || 'bg-yellow-900 text-yellow-300'
  return <span className={`px-2.5 py-0.5 rounded-full text-xs font-medium ${cls}`}>{state}</span>
}

function Section({ title, icon: Icon, children }) {
  return (
    <div className="bg-navy-700 border border-navy-400 rounded-xl p-5">
      <h3 className="text-sky-400 font-semibold mb-4 flex items-center gap-2">
        {Icon && <Icon size={16} />} {title}
      </h3>
      {children}
    </div>
  )
}

export default function VMDetail() {
  const { uuid } = useParams()
  const navigate = useNavigate()
  const [vm, setVm] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  // Form states
  const [newDisk, setNewDisk] = useState('')
  const [newIface, setNewIface] = useState({ mode: 'nat', source: 'default' })
  const [newPci, setNewPci] = useState('')
  const [newSnap, setNewSnap] = useState('')
  const [boot1, setBoot1] = useState('')
  const [boot2, setBoot2] = useState('')
  const [saving, setSaving] = useState({})

  const fetchVM = () => {
    setLoading(true)
    api.get(`/vms/${uuid}`)
      .then(r => {
        setVm(r.data)
        setBoot1(r.data.boot_devices?.[0]?.value || '')
        setBoot2(r.data.boot_devices?.[1]?.value || '')
      })
      .catch(e => setError(e.response?.data?.error || 'Failed to load VM'))
      .finally(() => setLoading(false))
  }

  useEffect(() => { fetchVM() }, [uuid])

  const act = async (key, fn) => {
    setSaving(p => ({ ...p, [key]: true }))
    try { await fn(); fetchVM() }
    catch (e) { alert(e.response?.data?.error || 'Error') }
    setSaving(p => ({ ...p, [key]: false }))
  }

  if (loading) return <div className="text-sky-400 text-center py-20">Loading VM details...</div>
  if (error) return <div className="text-red-400 text-center py-20">{error}</div>
  if (!vm) return null

  const bootOptions = vm.boot_devices || []

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="bg-navy-700 border border-navy-400 rounded-xl p-5">
        <div className="flex flex-wrap items-start gap-4 justify-between">
          <div>
            <div className="flex items-center gap-3 mb-1">
              <h2 className="text-xl font-bold text-slate-100">{vm.name}</h2>
              <StateBadge state={vm.state} />
            </div>
            <div className="text-slate-400 text-sm font-mono">{vm.uuid}</div>
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            {vm.state_code !== 1 ? (
              <button onClick={() => act('start', () => api.post(`/vms/${uuid}/start`))}
                className="flex items-center gap-2 bg-green-800 hover:bg-green-700 text-green-300 px-3 py-2 rounded-md text-sm">
                <Play size={14} /> Start
              </button>
            ) : (
              <button onClick={() => { if(confirm('Force stop?')) act('stop', () => api.post(`/vms/${uuid}/stop`)) }}
                className="flex items-center gap-2 bg-yellow-800 hover:bg-yellow-700 text-yellow-300 px-3 py-2 rounded-md text-sm">
                <Square size={14} /> Stop
              </button>
            )}
            <Link to={`/vms/${uuid}/edit`}
              className="flex items-center gap-2 bg-navy-500 hover:bg-navy-400 border border-navy-300 text-slate-300 px-3 py-2 rounded-md text-sm">
              <Pencil size={14} /> Edit
            </Link>
            <Link to={`/vms/${uuid}/monitor`}
              className="flex items-center gap-2 bg-navy-500 hover:bg-navy-400 border border-navy-300 text-slate-300 px-3 py-2 rounded-md text-sm">
              <Monitor size={14} /> Monitor
            </Link>
            <a href={`/terminal?vm_name=${vm.name}`} target="_blank" rel="noopener noreferrer"
              className="flex items-center gap-2 bg-navy-500 hover:bg-navy-400 border border-navy-300 text-slate-300 px-3 py-2 rounded-md text-sm">
              <Terminal size={14} /> Console
            </a>
            <button onClick={fetchVM}
              className="flex items-center gap-2 bg-navy-500 hover:bg-navy-400 border border-navy-300 text-slate-300 px-3 py-2 rounded-md text-sm">
              <RefreshCw size={14} />
            </button>
          </div>
        </div>

        {/* Resource summary */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mt-5">
          {[
            { label: 'Memory', value: `${vm.memory_mb} MB` },
            { label: 'vCPUs', value: vm.vcpus },
            { label: 'OS Type', value: vm.os_type || 'N/A' },
            { label: 'Project', value: vm.project || 'N/A' },
          ].map(item => (
            <div key={item.label} className="bg-navy-800 rounded-lg px-4 py-3">
              <div className="text-slate-400 text-xs mb-1">{item.label}</div>
              <div className="text-slate-200 font-medium">{item.value}</div>
            </div>
          ))}
        </div>
      </div>

      {/* Disks */}
      <Section title="Disks" icon={HardDrive}>
        {vm.disks?.length > 0 ? (
          <table className="w-full text-sm mb-4">
            <thead>
              <tr className="bg-navy-800 text-sky-400">
                <th className="px-3 py-2 text-left">Target</th>
                <th className="px-3 py-2 text-left">File</th>
                <th className="px-3 py-2 text-left">Type</th>
                <th className="px-3 py-2 text-left">Device</th>
                <th className="px-3 py-2"></th>
              </tr>
            </thead>
            <tbody>
              {vm.disks.map(d => (
                <tr key={d.target} className="border-b border-navy-500 hover:bg-navy-600">
                  <td className="px-3 py-2 font-mono text-slate-300">{d.target}</td>
                  <td className="px-3 py-2 text-slate-400 text-xs truncate max-w-xs">{d.file}</td>
                  <td className="px-3 py-2 text-slate-400">{d.type}</td>
                  <td className="px-3 py-2 text-slate-400">{d.device}</td>
                  <td className="px-3 py-2 text-right">
                    <button onClick={() => act(`disk-${d.target}`, () => api.delete(`/vms/${uuid}/disks`, { data: { target_dev: d.target } }))}
                      disabled={saving[`disk-${d.target}`]}
                      className="p-1.5 rounded bg-red-900/60 hover:bg-red-800 text-red-400 transition-colors disabled:opacity-50">
                      <Trash2 size={13} />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="text-slate-400 text-sm mb-4">No disks attached.</p>
        )}
        <div className="flex gap-2">
          <input
            value={newDisk}
            onChange={e => setNewDisk(e.target.value)}
            placeholder="File path (e.g. /var/lib/libvirt/images/disk.qcow2)"
            className="flex-1 bg-navy-800 border border-navy-400 text-slate-200 focus:border-sky-500 focus:outline-none rounded-md px-3 py-2 text-sm"
          />
          <button
            onClick={() => { if (!newDisk) return; act('add-disk', () => api.post(`/vms/${uuid}/disks`, { file_path: newDisk })); setNewDisk('') }}
            disabled={saving['add-disk']}
            className="flex items-center gap-1.5 bg-sky-500 hover:bg-sky-400 text-white px-3 py-2 rounded-md text-sm disabled:opacity-50"
          >
            <PlusCircle size={14} /> Add Disk
          </button>
        </div>
      </Section>

      {/* Network Interfaces */}
      <Section title="Network Interfaces" icon={Network}>
        {vm.interfaces?.length > 0 ? (
          <table className="w-full text-sm mb-4">
            <thead>
              <tr className="bg-navy-800 text-sky-400">
                <th className="px-3 py-2 text-left">MAC</th>
                <th className="px-3 py-2 text-left">Model</th>
                <th className="px-3 py-2 text-left">Network</th>
                <th className="px-3 py-2 text-left">IPs</th>
                <th className="px-3 py-2"></th>
              </tr>
            </thead>
            <tbody>
              {vm.interfaces.map(iface => (
                <tr key={iface.mac} className="border-b border-navy-500 hover:bg-navy-600">
                  <td className="px-3 py-2 font-mono text-slate-300 text-xs">{iface.mac}</td>
                  <td className="px-3 py-2 text-slate-400">{iface.model}</td>
                  <td className="px-3 py-2 text-slate-400">{iface.network}</td>
                  <td className="px-3 py-2 text-slate-400 text-xs">{iface.ips?.join(', ') || '—'}</td>
                  <td className="px-3 py-2 text-right">
                    <button onClick={() => act(`iface-${iface.mac}`, () => api.delete(`/vms/${uuid}/interfaces`, { data: { mac: iface.mac } }))}
                      disabled={saving[`iface-${iface.mac}`]}
                      className="p-1.5 rounded bg-red-900/60 hover:bg-red-800 text-red-400 transition-colors disabled:opacity-50">
                      <Trash2 size={13} />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="text-slate-400 text-sm mb-4">No network interfaces.</p>
        )}
        <div className="flex gap-2 flex-wrap">
          <select
            value={newIface.mode}
            onChange={e => setNewIface(p => ({ ...p, mode: e.target.value }))}
            className="bg-navy-800 border border-navy-400 text-slate-200 focus:border-sky-500 focus:outline-none rounded-md px-3 py-2 text-sm"
          >
            <option value="nat">NAT</option>
            <option value="bridge">Bridge</option>
          </select>
          <input
            value={newIface.source}
            onChange={e => setNewIface(p => ({ ...p, source: e.target.value }))}
            placeholder={newIface.mode === 'bridge' ? 'Bridge name (e.g. br0)' : 'Network name (e.g. default)'}
            className="flex-1 bg-navy-800 border border-navy-400 text-slate-200 focus:border-sky-500 focus:outline-none rounded-md px-3 py-2 text-sm"
          />
          <button
            onClick={() => act('add-iface', () => api.post(`/vms/${uuid}/interfaces`, newIface))}
            disabled={saving['add-iface']}
            className="flex items-center gap-1.5 bg-sky-500 hover:bg-sky-400 text-white px-3 py-2 rounded-md text-sm disabled:opacity-50"
          >
            <PlusCircle size={14} /> Add
          </button>
        </div>
      </Section>

      {/* PCI Devices */}
      <Section title="PCI Passthrough Devices" icon={Cpu}>
        {vm.host_devices?.length > 0 ? (
          <div className="space-y-2 mb-4">
            {vm.host_devices.map(dev => (
              <div key={dev.pci_id} className="flex items-center justify-between bg-navy-800 rounded-lg px-4 py-2.5">
                <div>
                  <div className="text-slate-200 text-sm">{dev.name}</div>
                  <div className="text-slate-500 text-xs font-mono">{dev.pci_id}</div>
                </div>
                <button onClick={() => act(`dev-${dev.pci_id}`, () => api.delete(`/vms/${uuid}/devices`, { data: { pci_id: dev.pci_id } }))}
                  disabled={saving[`dev-${dev.pci_id}`]}
                  className="p-1.5 rounded bg-red-900/60 hover:bg-red-800 text-red-400 transition-colors disabled:opacity-50">
                  <Trash2 size={13} />
                </button>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-slate-400 text-sm mb-4">No PCI devices attached.</p>
        )}
        {vm.available_devices?.length > 0 && (
          <div className="flex gap-2">
            <select
              value={newPci}
              onChange={e => setNewPci(e.target.value)}
              className="flex-1 bg-navy-800 border border-navy-400 text-slate-200 focus:border-sky-500 focus:outline-none rounded-md px-3 py-2 text-sm"
            >
              <option value="">Select device to attach...</option>
              {vm.available_devices.map(d => (
                <option key={d.pci_id} value={d.pci_id}>{d.name} ({d.pci_id})</option>
              ))}
            </select>
            <button
              onClick={() => { if (!newPci) return; act('add-dev', () => api.post(`/vms/${uuid}/devices`, { pci_id: newPci })); setNewPci('') }}
              disabled={saving['add-dev'] || !newPci}
              className="flex items-center gap-1.5 bg-sky-500 hover:bg-sky-400 text-white px-3 py-2 rounded-md text-sm disabled:opacity-50"
            >
              <PlusCircle size={14} /> Attach
            </button>
          </div>
        )}
      </Section>

      {/* Boot Order */}
      <Section title="Boot Order" icon={ChevronUp}>
        <div className="flex flex-wrap gap-4 items-end">
          <div>
            <label className="block text-xs text-slate-400 mb-1">Boot 1</label>
            <select
              value={boot1}
              onChange={e => setBoot1(e.target.value)}
              className="bg-navy-800 border border-navy-400 text-slate-200 focus:border-sky-500 focus:outline-none rounded-md px-3 py-2 text-sm"
            >
              <option value="">None</option>
              {bootOptions.map(o => <option key={o.value} value={o.value}>{o.text}</option>)}
            </select>
          </div>
          <div>
            <label className="block text-xs text-slate-400 mb-1">Boot 2</label>
            <select
              value={boot2}
              onChange={e => setBoot2(e.target.value)}
              className="bg-navy-800 border border-navy-400 text-slate-200 focus:border-sky-500 focus:outline-none rounded-md px-3 py-2 text-sm"
            >
              <option value="">None</option>
              {bootOptions.map(o => <option key={o.value} value={o.value}>{o.text}</option>)}
            </select>
          </div>
          <button
            onClick={() => act('boot', () => api.put(`/vms/${uuid}/boot`, { boot1, boot2 }))}
            disabled={saving['boot']}
            className="flex items-center gap-1.5 bg-sky-500 hover:bg-sky-400 text-white px-4 py-2 rounded-md text-sm disabled:opacity-50"
          >
            <Save size={14} /> Save Boot Order
          </button>
        </div>
      </Section>

      {/* Snapshots */}
      <Section title="Snapshots" icon={Camera}>
        {vm.snapshots?.length > 0 ? (
          <div className="space-y-2 mb-4">
            {vm.snapshots.map(snap => (
              <div key={snap.name} className="flex items-center justify-between bg-navy-800 rounded-lg px-4 py-2.5">
                <span className="text-slate-200 text-sm font-medium">{snap.name}</span>
                <div className="flex gap-2">
                  <button onClick={() => act(`revert-${snap.name}`, () => api.post(`/vms/${uuid}/snapshots/${snap.name}/revert`))}
                    disabled={saving[`revert-${snap.name}`]}
                    className="flex items-center gap-1 bg-navy-500 hover:bg-navy-400 border border-navy-300 text-slate-300 px-2.5 py-1.5 rounded text-xs disabled:opacity-50">
                    Revert
                  </button>
                  <button onClick={() => act(`del-snap-${snap.name}`, () => api.delete(`/vms/${uuid}/snapshots/${snap.name}`))}
                    disabled={saving[`del-snap-${snap.name}`]}
                    className="p-1.5 rounded bg-red-900/60 hover:bg-red-800 text-red-400 transition-colors disabled:opacity-50">
                    <Trash2 size={13} />
                  </button>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-slate-400 text-sm mb-4">No snapshots.</p>
        )}
        <div className="flex gap-2">
          <input
            value={newSnap}
            onChange={e => setNewSnap(e.target.value)}
            placeholder="Snapshot name"
            className="flex-1 bg-navy-800 border border-navy-400 text-slate-200 focus:border-sky-500 focus:outline-none rounded-md px-3 py-2 text-sm"
          />
          <button
            onClick={() => { if (!newSnap) return; act('add-snap', () => api.post(`/vms/${uuid}/snapshots`, { snapshot_name: newSnap })); setNewSnap('') }}
            disabled={saving['add-snap']}
            className="flex items-center gap-1.5 bg-sky-500 hover:bg-sky-400 text-white px-3 py-2 rounded-md text-sm disabled:opacity-50"
          >
            <Camera size={14} /> Create
          </button>
        </div>
      </Section>
    </div>
  )
}
