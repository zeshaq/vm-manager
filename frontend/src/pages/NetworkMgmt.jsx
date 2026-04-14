import { useState, useEffect, useCallback, useRef } from 'react'
import api from '../api'
import {
  Network, RefreshCw, AlertCircle, CheckCircle, Loader2, X,
  ArrowUpDown, FileText, Plus, Save, Play, ShieldAlert,
  Wifi, WifiOff, ChevronRight, Globe, Route, Dna
} from 'lucide-react'

// ── helpers ───────────────────────────────────────────────────────────────────

function bytes(n) {
  if (!n) return '0 B'
  const u = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++ }
  return `${n.toFixed(1)} ${u[i]}`
}

// ── reusable ──────────────────────────────────────────────────────────────────

function Btn({ children, onClick, variant = 'ghost', size = 'sm', disabled, loading, className = '' }) {
  const base = 'inline-flex items-center gap-1.5 rounded font-medium transition-all disabled:opacity-40 disabled:cursor-not-allowed'
  const sizes = { sm: 'px-2.5 py-1.5 text-xs', md: 'px-4 py-2 text-sm' }
  const variants = {
    ghost:   'text-slate-400 hover:text-sky-300 hover:bg-navy-600',
    primary: 'bg-sky-600 hover:bg-sky-500 text-white',
    success: 'bg-green-700 hover:bg-green-600 text-white',
    danger:  'text-red-400 hover:text-red-300 hover:bg-red-400/10',
    warning: 'bg-yellow-700 hover:bg-yellow-600 text-white',
  }
  return (
    <button onClick={onClick} disabled={disabled || loading}
      className={`${base} ${sizes[size]} ${variants[variant]} ${className}`}>
      {loading ? <Loader2 size={12} className="animate-spin" /> : children}
    </button>
  )
}

function Toast({ msg, type, onClose }) {
  useEffect(() => { if (msg) { const t = setTimeout(onClose, 5000); return () => clearTimeout(t) } }, [msg])
  if (!msg) return null
  const color = type === 'error' ? 'bg-red-900/90 border-red-700 text-red-200'
    : type === 'warn' ? 'bg-yellow-900/90 border-yellow-700 text-yellow-200'
    : 'bg-green-900/90 border-green-700 text-green-200'
  const Icon = type === 'error' ? AlertCircle : type === 'warn' ? ShieldAlert : CheckCircle
  return (
    <div className={`fixed bottom-5 right-5 flex items-center gap-2 px-4 py-3 rounded-lg border text-sm z-50 shadow-xl max-w-md ${color}`}>
      <Icon size={16} className="flex-shrink-0" />
      <span>{msg}</span>
      <button onClick={onClose} className="ml-2 opacity-70 hover:opacity-100"><X size={14} /></button>
    </div>
  )
}

// ── Interfaces tab ────────────────────────────────────────────────────────────

function InterfaceCard({ iface }) {
  const up = iface.operstate === 'UP'
  const isLoopback = iface.link_type === 'loopback'
  const ipv4 = iface.addresses.filter(a => a.family === 'inet')
  const ipv6 = iface.addresses.filter(a => a.family === 'inet6')

  const typeBadge = isLoopback ? 'Loopback'
    : iface.name.startsWith('virbr') ? 'Bridge (libvirt)'
    : iface.name.startsWith('docker') || iface.name.startsWith('br-') ? 'Bridge (docker)'
    : iface.name.startsWith('vnet') ? 'VM tap'
    : 'Ethernet'

  return (
    <div className={`bg-navy-800 border rounded-lg p-4 ${up ? 'border-navy-600' : 'border-navy-700 opacity-70'}`}>
      <div className="flex items-start justify-between mb-3">
        <div className="flex items-center gap-2">
          {up
            ? <Wifi size={18} className="text-green-400 flex-shrink-0" />
            : <WifiOff size={18} className="text-slate-500 flex-shrink-0" />}
          <div>
            <div className="text-slate-100 font-semibold font-mono">{iface.name}</div>
            <div className="text-xs text-slate-500">{typeBadge}</div>
          </div>
        </div>
        <span className={`text-xs px-2 py-0.5 rounded-full font-semibold ring-1 ${
          up
            ? 'text-green-400 bg-green-400/10 ring-green-400/30'
            : 'text-slate-400 bg-slate-400/10 ring-slate-400/30'
        }`}>{iface.operstate}</span>
      </div>

      {/* IPs */}
      <div className="space-y-1 mb-3">
        {ipv4.map(a => (
          <div key={a.cidr} className="flex items-center gap-2">
            <span className="text-xs bg-sky-900/50 text-sky-300 px-1.5 rounded font-mono">IPv4</span>
            <span className="text-slate-200 font-mono text-sm">{a.cidr}</span>
          </div>
        ))}
        {ipv6.filter(a => a.scope !== 'host').map(a => (
          <div key={a.cidr} className="flex items-center gap-2">
            <span className="text-xs bg-purple-900/50 text-purple-300 px-1.5 rounded font-mono">IPv6</span>
            <span className="text-slate-400 font-mono text-xs truncate">{a.cidr}</span>
          </div>
        ))}
        {iface.addresses.length === 0 && <span className="text-xs text-slate-500">No addresses</span>}
      </div>

      {/* Details row */}
      <div className="flex flex-wrap gap-3 text-xs text-slate-500 border-t border-navy-700 pt-2">
        {iface.mac && iface.mac !== '00:00:00:00:00:00' && (
          <span title="MAC">{iface.mac}</span>
        )}
        <span>MTU {iface.mtu}</span>
      </div>

      {/* TX/RX */}
      {(iface.tx_bytes > 0 || iface.rx_bytes > 0) && (
        <div className="flex gap-4 mt-2 text-xs text-slate-500">
          <span className="flex items-center gap-1">
            <ArrowUpDown size={11} className="text-sky-500" />
            ↑ {bytes(iface.tx_bytes)} / ↓ {bytes(iface.rx_bytes)}
          </span>
          {(iface.tx_errors > 0 || iface.rx_errors > 0) && (
            <span className="text-red-400">
              Err: TX {iface.tx_errors} / RX {iface.rx_errors}
            </span>
          )}
        </div>
      )}
    </div>
  )
}

function InterfacesTab({ notify }) {
  const [ifaces, setIfaces] = useState([])
  const [routes, setRoutes] = useState([])
  const [dns, setDns] = useState(null)
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [ifR, rtR, dnsR] = await Promise.all([
        api.get('/network/interfaces'),
        api.get('/network/routes'),
        api.get('/network/dns'),
      ])
      setIfaces(ifR.data)
      setRoutes(rtR.data.filter(r => r.family === 'inet'))
      setDns(dnsR.data)
    } catch (e) {
      notify(e.response?.data?.error || 'Failed to load network data', 'error')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  if (loading) return (
    <div className="flex items-center gap-2 text-slate-400 py-8">
      <Loader2 size={18} className="animate-spin" /> Loading interfaces…
    </div>
  )

  const physicalFirst = [...ifaces].sort((a, b) => {
    const score = i => i.link_type === 'loopback' ? 99
      : i.name.startsWith('enp') || i.name.startsWith('eth') ? 0
      : i.name.startsWith('virbr') ? 2
      : i.name.startsWith('docker') || i.name.startsWith('br-') ? 3
      : 1
    return score(a) - score(b)
  })

  return (
    <div className="space-y-6">
      {/* Interface cards */}
      <div>
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-slate-300 font-semibold flex items-center gap-2">
            <Wifi size={16} className="text-sky-400" /> Interfaces
          </h3>
          <Btn onClick={load} disabled={loading}><RefreshCw size={13} /> Refresh</Btn>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
          {physicalFirst.map(iface => <InterfaceCard key={iface.name} iface={iface} />)}
        </div>
      </div>

      {/* Routing table */}
      <div>
        <h3 className="text-slate-300 font-semibold flex items-center gap-2 mb-3">
          <Route size={16} className="text-sky-400" /> IPv4 Routing Table
        </h3>
        <div className="overflow-x-auto rounded-lg border border-navy-600">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-navy-600 bg-navy-700/50">
                <th className="text-left px-4 py-2.5 text-slate-400 font-medium text-xs">Destination</th>
                <th className="text-left px-4 py-2.5 text-slate-400 font-medium text-xs">Gateway</th>
                <th className="text-left px-4 py-2.5 text-slate-400 font-medium text-xs">Interface</th>
                <th className="text-left px-4 py-2.5 text-slate-400 font-medium text-xs">Protocol</th>
                <th className="text-left px-4 py-2.5 text-slate-400 font-medium text-xs">Src</th>
              </tr>
            </thead>
            <tbody>
              {routes.map((r, i) => (
                <tr key={i} className="border-b border-navy-700 hover:bg-navy-700/30 text-xs">
                  <td className="px-4 py-2.5 font-mono text-slate-200">
                    {r.dst === 'default' ? (
                      <span className="flex items-center gap-1">
                        <Globe size={11} className="text-sky-400" /> default
                      </span>
                    ) : r.dst}
                  </td>
                  <td className="px-4 py-2.5 font-mono text-slate-300">{r.gateway || '—'}</td>
                  <td className="px-4 py-2.5 text-slate-300">{r.dev}</td>
                  <td className="px-4 py-2.5 text-slate-500">{r.protocol || '—'}</td>
                  <td className="px-4 py-2.5 font-mono text-slate-400">{r.prefsrc || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* DNS */}
      {dns && (
        <div>
          <h3 className="text-slate-300 font-semibold flex items-center gap-2 mb-3">
            <Dna size={16} className="text-sky-400" /> DNS
          </h3>
          <div className="bg-navy-800 border border-navy-600 rounded-lg p-4 flex flex-wrap gap-8">
            <div>
              <p className="text-xs text-slate-500 mb-1.5">Nameservers</p>
              {dns.servers.length ? dns.servers.map(s => (
                <div key={s} className="text-slate-200 font-mono text-sm">{s}</div>
              )) : <span className="text-slate-500 text-sm">None configured</span>}
            </div>
            {dns.search.length > 0 && (
              <div>
                <p className="text-xs text-slate-500 mb-1.5">Search Domains</p>
                {dns.search.map(s => (
                  <div key={s} className="text-slate-200 font-mono text-sm">{s}</div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

// ── Netplan editor tab ────────────────────────────────────────────────────────

const NEW_FILE_TEMPLATE = `network:
  version: 2
  ethernets:
    # Example: static IP
    # eth0:
    #   addresses:
    #     - 192.168.1.100/24
    #   routes:
    #     - to: default
    #       via: 192.168.1.1
    #   nameservers:
    #     addresses: [8.8.8.8, 1.1.1.1]
`

function NetplanTab({ notify }) {
  const [configs, setConfigs] = useState([])
  const [selected, setSelected] = useState(null)   // filename
  const [content, setContent] = useState('')
  const [original, setOriginal] = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [applying, setApplying] = useState(false)
  const [validating, setValidating] = useState(false)
  const [validResult, setValidResult] = useState(null)  // {valid, error}
  const [newFileName, setNewFileName] = useState('')
  const [showNewForm, setShowNewForm] = useState(false)
  const [applyOutput, setApplyOutput] = useState('')
  const textareaRef = useRef(null)

  const loadConfigs = useCallback(async () => {
    setLoading(true)
    try {
      const r = await api.get('/network/netplan/configs')
      setConfigs(r.data)
      if (r.data.length > 0 && !selected) {
        setSelected(r.data[0].filename)
        setContent(r.data[0].content)
        setOriginal(r.data[0].content)
      }
    } catch (e) {
      notify(e.response?.data?.error || 'Failed to load configs', 'error')
    } finally {
      setLoading(false)
    }
  }, [selected])

  useEffect(() => { loadConfigs() }, [])

  const selectFile = (fname) => {
    const cfg = configs.find(c => c.filename === fname)
    if (!cfg) return
    setSelected(fname)
    setContent(cfg.content)
    setOriginal(cfg.content)
    setValidResult(null)
    setApplyOutput('')
  }

  const isDirty = content !== original

  const save = async () => {
    if (!selected) return
    setSaving(true)
    try {
      await api.put(`/network/netplan/configs/${selected}`, { content })
      setOriginal(content)
      // Update in-memory list
      setConfigs(cs => cs.map(c => c.filename === selected ? { ...c, content } : c))
      notify('File saved', 'ok')
      setValidResult(null)
    } catch (e) {
      notify(e.response?.data?.error || 'Save failed', 'error')
    } finally {
      setSaving(false)
    }
  }

  const validate = async () => {
    setValidating(true)
    setValidResult(null)
    try {
      const r = await api.post('/network/netplan/validate', { content })
      setValidResult(r.data)
      if (r.data.valid) notify('Configuration is valid', 'ok')
      else notify(`Validation failed: ${r.data.error}`, 'error')
    } catch (e) {
      setValidResult({ valid: false, error: e.response?.data?.error || 'Validation error' })
    } finally {
      setValidating(false)
    }
  }

  const apply = async () => {
    if (!confirm('Apply netplan configuration?\n\nWarning: if the config is invalid or changes the IP, you may lose remote access.')) return
    // Save first if dirty
    if (isDirty) {
      await save()
    }
    setApplying(true)
    setApplyOutput('')
    try {
      const r = await api.post('/network/netplan/apply')
      setApplyOutput(r.data.output || 'Applied successfully.')
      notify('netplan apply succeeded', 'ok')
    } catch (e) {
      const msg = e.response?.data?.error || 'Apply failed'
      setApplyOutput(msg)
      notify(msg, 'error')
    } finally {
      setApplying(false)
    }
  }

  const createFile = async () => {
    const fname = newFileName.trim()
    if (!fname) return
    const fn = fname.endsWith('.yaml') ? fname : fname + '.yaml'
    try {
      await api.post('/network/netplan/configs', { filename: fn, content: NEW_FILE_TEMPLATE })
      notify(`Created ${fn}`, 'ok')
      setNewFileName('')
      setShowNewForm(false)
      // Reload and select new file
      const r = await api.get('/network/netplan/configs')
      setConfigs(r.data)
      const newCfg = r.data.find(c => c.filename === fn)
      if (newCfg) {
        setSelected(fn)
        setContent(newCfg.content)
        setOriginal(newCfg.content)
      }
    } catch (e) {
      notify(e.response?.data?.error || 'Create failed', 'error')
    }
  }

  // Tab key in textarea → insert spaces
  const handleKeyDown = (e) => {
    if (e.key === 'Tab') {
      e.preventDefault()
      const ta = textareaRef.current
      const start = ta.selectionStart
      const end = ta.selectionEnd
      const newContent = content.substring(0, start) + '  ' + content.substring(end)
      setContent(newContent)
      requestAnimationFrame(() => {
        ta.selectionStart = ta.selectionEnd = start + 2
      })
    }
  }

  if (loading) return (
    <div className="flex items-center gap-2 text-slate-400 py-8">
      <Loader2 size={18} className="animate-spin" /> Loading netplan files…
    </div>
  )

  return (
    <div className="flex gap-4 h-[calc(100vh-280px)] min-h-[500px]">
      {/* File list sidebar */}
      <div className="w-56 flex-shrink-0 flex flex-col gap-2">
        <div className="flex items-center justify-between mb-1">
          <span className="text-xs text-slate-400 font-semibold uppercase tracking-wider">Config Files</span>
          <button
            onClick={() => setShowNewForm(s => !s)}
            className="text-slate-500 hover:text-sky-400 transition-colors"
            title="New file"
          >
            <Plus size={15} />
          </button>
        </div>

        {showNewForm && (
          <div className="bg-navy-700 rounded p-2 space-y-1.5">
            <input
              value={newFileName}
              onChange={e => setNewFileName(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && createFile()}
              placeholder="90-custom.yaml"
              className="w-full bg-navy-800 border border-navy-500 rounded px-2 py-1 text-xs text-slate-200 placeholder-slate-500 focus:outline-none focus:border-sky-500"
            />
            <div className="flex gap-1">
              <Btn onClick={createFile} variant="primary" size="sm">Create</Btn>
              <Btn onClick={() => setShowNewForm(false)} size="sm">Cancel</Btn>
            </div>
          </div>
        )}

        <div className="space-y-0.5">
          {configs.map(cfg => (
            <button
              key={cfg.filename}
              onClick={() => selectFile(cfg.filename)}
              className={`w-full flex items-center gap-2 px-3 py-2.5 rounded text-left text-xs transition-all ${
                selected === cfg.filename
                  ? 'bg-navy-500 text-sky-300 border-l-2 border-sky-400'
                  : 'text-slate-400 hover:text-slate-200 hover:bg-navy-700'
              }`}
            >
              <FileText size={13} />
              <span className="font-mono truncate">{cfg.filename}</span>
            </button>
          ))}
        </div>

        {/* Danger zone */}
        <div className="mt-auto pt-3 border-t border-navy-600 space-y-2">
          <p className="text-xs text-slate-500">
            /etc/netplan/
          </p>
          <div className="bg-yellow-900/30 border border-yellow-800/50 rounded p-2">
            <p className="text-yellow-400 text-xs flex items-center gap-1 mb-1">
              <ShieldAlert size={12} /> Warning
            </p>
            <p className="text-yellow-200/70 text-xs leading-relaxed">
              Applying an invalid config may disconnect you from this server.
            </p>
          </div>
        </div>
      </div>

      {/* Editor panel */}
      <div className="flex-1 flex flex-col min-w-0">
        {selected ? (
          <>
            {/* Toolbar */}
            <div className="flex items-center gap-2 mb-3 flex-wrap">
              <span className="text-slate-400 text-xs font-mono bg-navy-700 px-2 py-1 rounded">
                {selected}
                {isDirty && <span className="ml-1 text-yellow-400">●</span>}
              </span>
              <div className="flex-1" />
              <Btn onClick={validate} loading={validating} size="sm">
                <CheckCircle size={12} /> Validate
              </Btn>
              <Btn onClick={save} loading={saving} variant="primary" size="sm" disabled={!isDirty}>
                <Save size={12} /> Save
              </Btn>
              <Btn onClick={apply} loading={applying} variant="success" size="sm">
                <Play size={12} /> Save & Apply
              </Btn>
            </div>

            {/* Validation result */}
            {validResult && (
              <div className={`flex items-center gap-2 px-3 py-2 rounded mb-2 text-xs ${
                validResult.valid
                  ? 'bg-green-900/40 border border-green-800 text-green-300'
                  : 'bg-red-900/40 border border-red-800 text-red-300'
              }`}>
                {validResult.valid
                  ? <><CheckCircle size={13} /> Configuration is valid</>
                  : <><AlertCircle size={13} /> {validResult.error}</>}
              </div>
            )}

            {/* Apply output */}
            {applyOutput && (
              <pre className="bg-black/40 border border-navy-600 rounded p-3 text-xs font-mono text-green-300 mb-2 whitespace-pre-wrap max-h-20 overflow-y-auto">
                {applyOutput}
              </pre>
            )}

            {/* YAML editor */}
            <textarea
              ref={textareaRef}
              value={content}
              onChange={e => { setContent(e.target.value); setValidResult(null) }}
              onKeyDown={handleKeyDown}
              spellCheck={false}
              className="flex-1 bg-navy-900 border border-navy-600 rounded p-4 font-mono text-sm text-green-300 leading-relaxed resize-none focus:outline-none focus:border-sky-500 focus:ring-1 focus:ring-sky-500/30 transition-colors"
              style={{ tabSize: 2 }}
              placeholder="# netplan YAML configuration"
            />
            <p className="text-xs text-slate-600 mt-1.5">
              Tab = 2 spaces · Ctrl+S not bound — use the Save button
            </p>
          </>
        ) : (
          <div className="flex items-center justify-center h-full text-slate-500">
            <div className="text-center">
              <FileText size={36} className="mx-auto mb-3 opacity-30" />
              <p>Select a file to edit</p>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

// ── Main page ─────────────────────────────────────────────────────────────────

const TABS = [
  { id: 'interfaces', label: 'Interfaces & Routes', icon: Network },
  { id: 'netplan',    label: 'Netplan Editor',      icon: FileText },
]

export default function NetworkMgmt() {
  const [tab, setTab] = useState('interfaces')
  const [toast, setToast] = useState({ msg: '', type: 'ok' })

  const notify = (msg, type = 'ok') => setToast({ msg, type })
  const clearToast = () => setToast({ msg: '', type: 'ok' })

  return (
    <div className="space-y-5">
      {/* Tab bar */}
      <div className="flex gap-1 bg-navy-800 border border-navy-600 rounded-lg p-1 w-fit">
        {TABS.map(t => {
          const Icon = t.icon
          return (
            <button key={t.id} onClick={() => setTab(t.id)}
              className={`flex items-center gap-2 px-4 py-2 rounded text-sm font-medium transition-all ${
                tab === t.id
                  ? 'bg-sky-600 text-white shadow'
                  : 'text-slate-400 hover:text-sky-300 hover:bg-navy-700'
              }`}>
              <Icon size={15} />
              {t.label}
            </button>
          )
        })}
      </div>

      {tab === 'interfaces' && <InterfacesTab notify={notify} />}
      {tab === 'netplan'    && <NetplanTab    notify={notify} />}

      <Toast msg={toast.msg} type={toast.type} onClose={clearToast} />
    </div>
  )
}
