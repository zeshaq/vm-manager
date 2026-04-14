import { useEffect, useState } from 'react'
import { Clock, Cpu, MemoryStick, HardDrive, Network, RefreshCw, Activity } from 'lucide-react'
import api from '../api'

function Card({ icon: Icon, title, value, sub, progress, color = 'sky' }) {
  return (
    <div className="bg-navy-700 border border-navy-400 rounded-xl p-5">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <Icon size={16} className={`text-${color}-400`} />
          <span className="text-slate-400 text-sm font-medium">{title}</span>
        </div>
      </div>
      <div className="text-2xl font-bold text-slate-100 mb-1">{value}</div>
      {sub && <div className="text-slate-400 text-xs">{sub}</div>}
      {progress !== undefined && (
        <div className="mt-3">
          <div className="bg-navy-900 rounded-full h-2 overflow-hidden">
            <div
              className={`bg-${color}-500 h-2 rounded-full transition-all`}
              style={{ width: `${Math.min(progress, 100)}%` }}
            />
          </div>
          <div className="text-xs text-slate-500 mt-1 text-right">{progress}%</div>
        </div>
      )}
    </div>
  )
}

function bytesHuman(b) {
  if (b === null || b === undefined) return '—'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let val = b
  let i = 0
  while (val >= 1024 && i < units.length - 1) { val /= 1024; i++ }
  return `${val.toFixed(1)} ${units[i]}`
}

export default function Dashboard() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const fetchData = () => {
    setLoading(true)
    api.get('/dashboard')
      .then(r => setData(r.data))
      .catch(e => setError(e.response?.data?.error || 'Failed to load dashboard'))
      .finally(() => setLoading(false))
  }

  useEffect(() => { fetchData() }, [])

  if (loading) return <div className="text-sky-400 text-center py-20">Loading dashboard...</div>
  if (error) return <div className="text-red-400 text-center py-20">{error}</div>

  const { uptime_str, cpu_percent, load_avg, mem, disk, net, processes } = data

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h2 className="text-xl font-bold text-slate-100">System Dashboard</h2>
        <button
          onClick={fetchData}
          className="flex items-center gap-2 bg-navy-500 hover:bg-navy-400 border border-navy-300 text-slate-300 px-3 py-2 rounded-md text-sm transition-colors"
        >
          <RefreshCw size={13} /> Refresh
        </button>
      </div>

      {/* Cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <Card icon={Clock} title="Uptime" value={uptime_str} color="sky" />
        <Card
          icon={Cpu}
          title="CPU Usage"
          value={`${cpu_percent}%`}
          sub={`Load: ${load_avg?.[0]} / ${load_avg?.[1]} / ${load_avg?.[2]}`}
          progress={cpu_percent}
          color="sky"
        />
        <Card
          icon={MemoryStick}
          title="Memory"
          value={`${mem?.used_gb} GB`}
          sub={`of ${mem?.total_gb} GB`}
          progress={mem?.percent}
          color="purple"
        />
        <Card
          icon={HardDrive}
          title="Disk (/)"
          value={`${disk?.used_gb} GB`}
          sub={`of ${disk?.total_gb} GB`}
          progress={disk?.percent}
          color="green"
        />
      </div>

      {/* Network */}
      <div className="bg-navy-700 border border-navy-400 rounded-xl p-5">
        <h3 className="text-sky-400 font-semibold mb-4 flex items-center gap-2">
          <Network size={16} /> Network I/O (cumulative)
        </h3>
        <div className="grid grid-cols-2 gap-4">
          <div className="bg-navy-800 rounded-lg px-4 py-3">
            <div className="text-slate-400 text-xs mb-1">Bytes Received</div>
            <div className="text-slate-100 font-semibold text-lg">{bytesHuman(net?.bytes_recv)}</div>
          </div>
          <div className="bg-navy-800 rounded-lg px-4 py-3">
            <div className="text-slate-400 text-xs mb-1">Bytes Sent</div>
            <div className="text-slate-100 font-semibold text-lg">{bytesHuman(net?.bytes_sent)}</div>
          </div>
        </div>
      </div>

      {/* Top processes */}
      <div className="bg-navy-700 border border-navy-400 rounded-xl overflow-hidden">
        <div className="px-5 py-4 border-b border-navy-500">
          <h3 className="text-sky-400 font-semibold flex items-center gap-2">
            <Activity size={16} /> Top Processes
          </h3>
        </div>
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-navy-800 text-sky-400">
              <th className="px-4 py-2.5 text-left">PID</th>
              <th className="px-4 py-2.5 text-left">Name</th>
              <th className="px-4 py-2.5 text-left hidden md:table-cell">User</th>
              <th className="px-4 py-2.5 text-right">CPU %</th>
              <th className="px-4 py-2.5 text-right">MEM %</th>
            </tr>
          </thead>
          <tbody>
            {(processes || []).map((proc, i) => (
              <tr key={`${proc.pid}-${i}`} className="border-b border-navy-500 hover:bg-navy-600 transition-colors">
                <td className="px-4 py-2.5 text-slate-400 font-mono text-xs">{proc.pid}</td>
                <td className="px-4 py-2.5 text-slate-200">{proc.name}</td>
                <td className="px-4 py-2.5 text-slate-400 hidden md:table-cell">{proc.username}</td>
                <td className="px-4 py-2.5 text-right">
                  <span className={`font-medium ${proc.cpu_percent > 10 ? 'text-yellow-400' : 'text-slate-300'}`}>
                    {(proc.cpu_percent || 0).toFixed(1)}%
                  </span>
                </td>
                <td className="px-4 py-2.5 text-right text-slate-300">
                  {(proc.memory_percent || 0).toFixed(1)}%
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
