import { useState, useEffect, useCallback, useRef } from 'react'
import {
  Boxes, ChevronRight, ChevronLeft, CheckCircle, XCircle,
  AlertTriangle, Loader2, RefreshCw, Download, Server,
  Cpu, MemoryStick, HardDrive, Network, Key, Lock,
  Shield, Terminal, ExternalLink, Copy, Check
} from 'lucide-react'
import api from '../api'

// ── helpers ───────────────────────────────────────────────────────────────────

const inputCls = 'w-full bg-navy-700 border border-navy-500 text-slate-100 rounded-md px-3 py-2 text-sm focus:outline-none focus:border-sky-500 placeholder-slate-600'
const labelCls = 'block text-xs font-medium text-slate-400 mb-1'

function Field({ label, hint, children, required }) {
  return (
    <div>
      <label className={labelCls}>
        {label} {required && <span className="text-red-400">*</span>}
      </label>
      {children}
      {hint && <p className="text-slate-600 text-xs mt-1">{hint}</p>}
    </div>
  )
}

function StatusIcon({ ok }) {
  if (ok === null || ok === undefined)
    return <span className="w-4 h-4 rounded-full bg-slate-600 inline-block" />
  return ok
    ? <CheckCircle size={16} className="text-green-400 flex-shrink-0" />
    : <XCircle    size={16} className="text-red-400   flex-shrink-0" />
}

// ── Step indicator ────────────────────────────────────────────────────────────

const STEPS = [
  'Preflight', 'Pull Secret', 'Cluster', 'Nodes', 'Network', 'Review', 'Deploy',
]

function StepBar({ current }) {
  return (
    <div className="flex items-center gap-0 mb-8 select-none">
      {STEPS.map((s, i) => {
        const done    = i < current
        const active  = i === current
        return (
          <div key={s} className="flex items-center">
            <div className={`flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-medium transition-all ${
              active ? 'bg-sky-600 text-white' :
              done   ? 'bg-navy-600 text-green-400' :
                       'bg-navy-700 text-slate-500'
            }`}>
              {done ? <Check size={11} /> : <span>{i + 1}</span>}
              <span className="hidden sm:inline">{s}</span>
            </div>
            {i < STEPS.length - 1 && (
              <div className={`h-px w-4 flex-shrink-0 ${done ? 'bg-green-700' : 'bg-navy-600'}`} />
            )}
          </div>
        )
      })}
    </div>
  )
}

// ── Step 0: Preflight ─────────────────────────────────────────────────────────

function StepPreflight({ onNext }) {
  const [data, setData]   = useState(null)
  const [loading, setL]   = useState(true)

  useEffect(() => {
    api.get('/openshift/preflight')
      .then(r => setData(r.data))
      .catch(() => setData({}))
      .finally(() => setL(false))
  }, [])

  const ok = data && data.libvirt && data.internet
    && (data.disk_ok !== false) && (data.ram_ok !== false)

  const checks = data ? [
    { label: 'libvirt / KVM available',        ok: data.libvirt },
    { label: 'Internet access to api.openshift.com', ok: data.internet },
    { label: `Disk space (${data.disk_free_gb ?? '?'} GB free, need 50+ GB)`,  ok: data.disk_ok },
    { label: `RAM available (${data.ram_free_gb ?? '?'} GB free, need 16+ GB)`, ok: data.ram_ok },
  ] : []

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-slate-100 font-bold text-lg mb-1">Prerequisites</h2>
        <p className="text-slate-400 text-sm">Checking if this host can run OpenShift.</p>
      </div>

      <div className="bg-navy-800 border border-navy-600 rounded-xl p-5 space-y-3">
        {loading
          ? <div className="flex items-center gap-2 text-slate-400 text-sm"><Loader2 size={16} className="animate-spin" /> Checking…</div>
          : checks.map(c => (
            <div key={c.label} className="flex items-center gap-3">
              <StatusIcon ok={c.ok} />
              <span className={`text-sm ${c.ok ? 'text-slate-200' : 'text-red-300'}`}>{c.label}</span>
            </div>
          ))
        }
      </div>

      {!loading && !ok && (
        <div className="bg-yellow-900/20 border border-yellow-700/40 rounded-xl p-4 text-sm text-yellow-300 flex gap-2">
          <AlertTriangle size={16} className="flex-shrink-0 mt-0.5" />
          <span>Some checks failed. You can still proceed but deployment may fail.</span>
        </div>
      )}

      <NavButtons onNext={onNext} nextLabel="Continue" />
    </div>
  )
}

// ── Step 1: Pull Secret ───────────────────────────────────────────────────────

function StepPullSecret({ form, set, onNext, onBack }) {
  const [validating, setV]   = useState(false)
  const [result, setResult]  = useState(null)

  const validate = async () => {
    setV(true)
    setResult(null)
    try {
      const r = await api.post('/openshift/validate-pull-secret', { pull_secret: form.pull_secret })
      setResult(r.data)
    } catch (e) {
      setResult({ valid: false, error: e.response?.data?.error || 'Validation failed' })
    } finally {
      setV(false)
    }
  }

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-slate-100 font-bold text-lg mb-1">Pull Secret</h2>
        <p className="text-slate-400 text-sm">
          Get your pull secret from{' '}
          <a href="https://console.redhat.com/openshift/install/pull-secret" target="_blank" rel="noopener noreferrer"
            className="text-sky-400 hover:underline inline-flex items-center gap-1">
            console.redhat.com <ExternalLink size={11} />
          </a>
        </p>
      </div>

      <Field label="Pull Secret JSON" required>
        <textarea
          value={form.pull_secret}
          onChange={e => { set('pull_secret', e.target.value); setResult(null) }}
          rows={6}
          placeholder='{"auths":{"cloud.openshift.com":{"auth":"..."},...}}'
          className={inputCls + ' font-mono text-xs resize-none'}
        />
      </Field>

      <div className="flex items-center gap-3">
        <button onClick={validate} disabled={!form.pull_secret || validating}
          className="flex items-center gap-2 bg-navy-600 hover:bg-navy-500 border border-navy-400 text-slate-200 px-4 py-2 rounded-md text-sm transition-colors disabled:opacity-40">
          {validating ? <Loader2 size={14} className="animate-spin" /> : <Shield size={14} />}
          Validate
        </button>

        {result && (
          <div className={`flex items-center gap-2 text-sm ${result.valid ? 'text-green-400' : 'text-red-400'}`}>
            {result.valid ? <CheckCircle size={15} /> : <XCircle size={15} />}
            {result.valid
              ? `Valid — ${result.registries?.length} registries`
              : result.error || `Missing: ${result.missing?.join(', ')}`
            }
          </div>
        )}
      </div>

      <NavButtons onBack={onBack} onNext={onNext}
        nextDisabled={!form.pull_secret || (result && !result.valid)} />
    </div>
  )
}

// ── Step 2: Cluster config ────────────────────────────────────────────────────

function StepCluster({ form, set, onNext, onBack }) {
  const [versions, setVersions]   = useState([])
  const [loadingV, setLV]         = useState(true)

  useEffect(() => {
    api.get('/openshift/versions')
      .then(r => {
        const v = r.data.versions || []
        setVersions(v)
        // Auto-select newest if form still has the static default
        if (v.length > 0 && (form.ocp_version === '4.21' || !form.ocp_version)) {
          set('ocp_version', v[0])
        }
      })
      .finally(() => setLV(false))
  }, [])

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-slate-100 font-bold text-lg mb-1">Cluster Configuration</h2>
        <p className="text-slate-400 text-sm">Basic cluster identity and OpenShift version.</p>
      </div>

      {/* Deployment type */}
      <Field label="Deployment Type" required>
        <div className="grid grid-cols-2 gap-3 mt-1">
          {[
            { id: 'sno', title: 'Single Node (SNO)', sub: 'All roles on 1 VM. Min 8 vCPU / 32 GB RAM.' },
            { id: 'multi', title: 'Multi-node', sub: 'Separate control plane + workers.' },
          ].map(opt => (
            <button key={opt.id} type="button" onClick={() => set('deployment_type', opt.id)}
              className={`text-left p-4 rounded-xl border transition-all ${
                form.deployment_type === opt.id
                  ? 'border-sky-500 bg-sky-500/10'
                  : 'border-navy-500 bg-navy-700 hover:border-navy-400'
              }`}>
              <div className="font-semibold text-slate-100 text-sm mb-1">{opt.title}</div>
              <div className="text-slate-400 text-xs">{opt.sub}</div>
            </button>
          ))}
        </div>
      </Field>

      <div className="grid grid-cols-2 gap-4">
        <Field label="Cluster Name" required hint="e.g. ocp-lab (no spaces, lowercase)">
          <input value={form.cluster_name} onChange={e => set('cluster_name', e.target.value.toLowerCase().replace(/[^a-z0-9-]/g,''))}
            placeholder="ocp-lab" className={inputCls} />
        </Field>
        <Field label="Base Domain" required hint="e.g. example.com">
          <input value={form.base_domain} onChange={e => set('base_domain', e.target.value)}
            placeholder="example.com" className={inputCls} />
        </Field>
      </div>

      <Field label="OpenShift Version" required>
        {loadingV
          ? <div className="text-slate-500 text-sm flex items-center gap-2"><Loader2 size={13} className="animate-spin" /> Loading versions…</div>
          : <select value={form.ocp_version} onChange={e => set('ocp_version', e.target.value)} className={inputCls}>
              {versions.map(v => <option key={v} value={v}>{v}</option>)}
            </select>
        }
      </Field>

      <Field label="SSH Public Key" hint="Paste your ~/.ssh/id_rsa.pub — lets you SSH into nodes post-install">
        <textarea value={form.ssh_public_key} onChange={e => set('ssh_public_key', e.target.value)}
          rows={3} placeholder="ssh-rsa AAAA..." className={inputCls + ' font-mono text-xs resize-none'} />
      </Field>

      <NavButtons onBack={onBack} onNext={onNext}
        nextDisabled={!form.cluster_name || !form.base_domain || !form.ocp_version} />
    </div>
  )
}

// ── Step 3: Node resources ────────────────────────────────────────────────────

function StepNodes({ form, set, onNext, onBack }) {
  const isSNO = form.deployment_type === 'sno'

  const NumInput = ({ field, label, min, step = 1 }) => (
    <Field label={label} required>
      <input type="number" value={form[field]} onChange={e => set(field, e.target.value)}
        min={min} step={step} className={inputCls} />
    </Field>
  )

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-slate-100 font-bold text-lg mb-1">Node Resources</h2>
        <p className="text-slate-400 text-sm">
          {isSNO ? 'Resources for the single node (runs all OpenShift roles).'
                 : 'Resources per node type.'}
        </p>
      </div>

      {/* SNO */}
      {isSNO && (
        <div className="bg-navy-800 border border-navy-600 rounded-xl p-5 space-y-4">
          <h3 className="text-slate-300 text-sm font-semibold flex items-center gap-2">
            <Server size={14} className="text-sky-400" /> Single Node
          </h3>
          <div className="grid grid-cols-3 gap-4">
            <NumInput field="cp_vcpus"   label="vCPUs"   min={8} />
            <NumInput field="cp_ram_gb"  label="RAM (GB)" min={32} />
            <NumInput field="cp_disk_gb" label="Disk (GB)" min={120} step={10} />
          </div>
          <div className="text-xs text-slate-500 flex gap-4">
            <span>Min: 8 vCPU</span><span>Min: 32 GB RAM</span><span>Min: 120 GB disk</span>
          </div>
        </div>
      )}

      {/* Multi-node */}
      {!isSNO && (
        <>
          <div className="bg-navy-800 border border-navy-600 rounded-xl p-5 space-y-4">
            <div className="flex items-center justify-between">
              <h3 className="text-slate-300 text-sm font-semibold flex items-center gap-2">
                <Server size={14} className="text-sky-400" /> Control Plane Nodes
              </h3>
              <Field label="Count">
                <select value={form.control_plane_count}
                  onChange={e => set('control_plane_count', e.target.value)}
                  className="bg-navy-700 border border-navy-500 rounded px-2 py-1.5 text-sm text-slate-200 focus:outline-none">
                  <option value="1">1</option>
                  <option value="3">3 (HA)</option>
                </select>
              </Field>
            </div>
            <div className="grid grid-cols-3 gap-4">
              <NumInput field="cp_vcpus"   label="vCPUs / node"   min={4} />
              <NumInput field="cp_ram_gb"  label="RAM GB / node"   min={16} />
              <NumInput field="cp_disk_gb" label="Disk GB / node"  min={100} step={10} />
            </div>
            <p className="text-slate-600 text-xs">Recommended: 8 vCPU / 32 GB / 120 GB</p>
          </div>

          <div className="bg-navy-800 border border-navy-600 rounded-xl p-5 space-y-4">
            <div className="flex items-center justify-between">
              <h3 className="text-slate-300 text-sm font-semibold flex items-center gap-2">
                <Server size={14} className="text-green-400" /> Worker Nodes
              </h3>
              <Field label="Count">
                <select value={form.worker_count}
                  onChange={e => set('worker_count', e.target.value)}
                  className="bg-navy-700 border border-navy-500 rounded px-2 py-1.5 text-sm text-slate-200 focus:outline-none">
                  {[0,1,2,3,4,5].map(n => <option key={n} value={n}>{n}</option>)}
                </select>
              </Field>
            </div>
            <div className="grid grid-cols-3 gap-4">
              <NumInput field="w_vcpus"   label="vCPUs / node"  min={2} />
              <NumInput field="w_ram_gb"  label="RAM GB / node"  min={8} />
              <NumInput field="w_disk_gb" label="Disk GB / node" min={50} step={10} />
            </div>
            <p className="text-slate-600 text-xs">Recommended: 4 vCPU / 16 GB / 100 GB</p>
          </div>
        </>
      )}

      <Field label="VM Disk Storage Path" hint="Where to create qcow2 disks on this host">
        <input value={form.storage_path} onChange={e => set('storage_path', e.target.value)}
          className={inputCls} />
      </Field>

      <NavButtons onBack={onBack} onNext={onNext} />
    </div>
  )
}

// ── Step 4: Network ───────────────────────────────────────────────────────────

function StepNetwork({ form, set, onNext, onBack }) {
  const isSNO = form.deployment_type === 'sno'

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-slate-100 font-bold text-lg mb-1">Network Configuration</h2>
        <p className="text-slate-400 text-sm">
          The machine network is the libvirt network your VMs will use.
        </p>
      </div>

      <div className="grid grid-cols-2 gap-4">
        <Field label="libvirt Network" hint="Name of the libvirt network (default = virbr0 NAT)">
          <input value={form.libvirt_network} onChange={e => set('libvirt_network', e.target.value)}
            placeholder="default" className={inputCls} />
        </Field>
        <Field label="Machine Network CIDR" required hint="The IP range of the libvirt network">
          <input value={form.machine_cidr} onChange={e => set('machine_cidr', e.target.value)}
            placeholder="192.168.122.0/24" className={inputCls + ' font-mono'} />
        </Field>
      </div>

      {!isSNO && (
        <div className="grid grid-cols-2 gap-4">
          <Field label="API VIP" required hint="Reserved IP for the cluster API (must be in machine CIDR, not assigned to any VM)">
            <input value={form.api_vip} onChange={e => set('api_vip', e.target.value)}
              placeholder="192.168.122.100" className={inputCls + ' font-mono'} />
          </Field>
          <Field label="Ingress VIP" required hint="Reserved IP for app routes (*.apps.cluster.domain)">
            <input value={form.ingress_vip} onChange={e => set('ingress_vip', e.target.value)}
              placeholder="192.168.122.101" className={inputCls + ' font-mono'} />
          </Field>
        </div>
      )}

      <div className="bg-navy-800 border border-navy-600 rounded-xl p-4 space-y-3">
        <h3 className="text-slate-300 text-xs font-semibold uppercase tracking-wide">Internal Network Ranges</h3>
        <div className="grid grid-cols-2 gap-4">
          <Field label="Cluster Network CIDR" hint="Pod network">
            <input value={form.cluster_cidr} onChange={e => set('cluster_cidr', e.target.value)}
              className={inputCls + ' font-mono text-xs'} />
          </Field>
          <Field label="Service Network CIDR" hint="Service (ClusterIP) network">
            <input value={form.service_cidr} onChange={e => set('service_cidr', e.target.value)}
              className={inputCls + ' font-mono text-xs'} />
          </Field>
        </div>
        <p className="text-slate-600 text-xs">These are internal to the cluster — defaults work for most setups.</p>
      </div>

      {isSNO && (
        <div className="bg-yellow-900/20 border border-yellow-700/40 rounded-xl p-4 text-xs text-yellow-300 space-y-1">
          <div className="font-semibold flex items-center gap-1.5"><AlertTriangle size={13} /> DNS required after install</div>
          <div>Add these to your DNS (or /etc/hosts) once the SNO node boots:</div>
          <div className="font-mono bg-navy-900 rounded p-2 mt-1 space-y-0.5">
            <div>&lt;node-ip&gt;  api.{form.cluster_name || 'cluster'}.{form.base_domain || 'domain'}</div>
            <div>&lt;node-ip&gt;  *.apps.{form.cluster_name || 'cluster'}.{form.base_domain || 'domain'}</div>
          </div>
        </div>
      )}

      <NavButtons onBack={onBack} onNext={onNext}
        nextDisabled={!form.machine_cidr || (!isSNO && (!form.api_vip || !form.ingress_vip))} />
    </div>
  )
}

// ── Step 5: Review ────────────────────────────────────────────────────────────

function StepReview({ form, onDeploy, onBack, deploying }) {
  const isSNO = form.deployment_type === 'sno'
  const nControl = isSNO ? 1 : parseInt(form.control_plane_count)
  const nWorkers = isSNO ? 0 : parseInt(form.worker_count)
  const totalVMs = nControl + nWorkers
  const totalCPU = isSNO
    ? form.cp_vcpus * 1
    : nControl * form.cp_vcpus + nWorkers * form.w_vcpus
  const totalRAM = isSNO
    ? form.cp_ram_gb * 1
    : nControl * form.cp_ram_gb + nWorkers * form.w_ram_gb

  const Row = ({ label, value, mono = false }) => (
    <div className="flex items-start justify-between py-2 border-b border-navy-700/50 last:border-0">
      <span className="text-slate-400 text-xs">{label}</span>
      <span className={`text-slate-200 text-xs ${mono ? 'font-mono' : 'font-medium'}`}>{value}</span>
    </div>
  )

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-slate-100 font-bold text-lg mb-1">Review & Deploy</h2>
        <p className="text-slate-400 text-sm">Confirm your configuration before deploying.</p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="bg-navy-800 border border-navy-600 rounded-xl p-5">
          <h3 className="text-slate-300 text-xs font-semibold uppercase tracking-wide mb-3">Cluster</h3>
          <Row label="Name"        value={form.cluster_name} mono />
          <Row label="Domain"      value={form.base_domain} mono />
          <Row label="Version"     value={form.ocp_version} />
          <Row label="Type"        value={isSNO ? 'Single Node OpenShift (SNO)' : `Multi-node (${nControl} control + ${nWorkers} workers)`} />
        </div>
        <div className="bg-navy-800 border border-navy-600 rounded-xl p-5">
          <h3 className="text-slate-300 text-xs font-semibold uppercase tracking-wide mb-3">Resources</h3>
          <Row label="Total VMs"   value={totalVMs} />
          <Row label="Total vCPUs" value={totalCPU} />
          <Row label="Total RAM"   value={`${totalRAM} GB`} />
          <Row label="Disk path"   value={form.storage_path} mono />
          <Row label="Network"     value={form.libvirt_network} mono />
          <Row label="Machine CIDR" value={form.machine_cidr} mono />
          {!isSNO && <Row label="API VIP"    value={form.api_vip} mono />}
          {!isSNO && <Row label="Ingress VIP" value={form.ingress_vip} mono />}
        </div>
      </div>

      <div className="bg-navy-800 border border-navy-600 rounded-xl p-5">
        <h3 className="text-slate-300 text-xs font-semibold uppercase tracking-wide mb-2">Post-install DNS</h3>
        <p className="text-slate-500 text-xs mb-2">Add to your DNS resolver or /etc/hosts on client machines:</p>
        <pre className="text-xs font-mono text-green-300 bg-navy-900 rounded p-3">
{`<node/api-vip IP>  api.${form.cluster_name}.${form.base_domain}
<node/ingress-vip IP>  *.apps.${form.cluster_name}.${form.base_domain}
<node/api-vip IP>  oauth-openshift.apps.${form.cluster_name}.${form.base_domain}`}
        </pre>
      </div>

      <div className="bg-yellow-900/20 border border-yellow-700/40 rounded-xl p-4 text-xs text-yellow-300 flex gap-2">
        <AlertTriangle size={13} className="flex-shrink-0 mt-0.5" />
        <span>
          Deployment will create <strong>{totalVMs} KVM VM{totalVMs > 1 ? 's' : ''}</strong> and
          use roughly <strong>{totalCPU} vCPUs</strong> and <strong>{totalRAM} GB RAM</strong>.
          Installation takes <strong>45–90 minutes</strong>.
        </span>
      </div>

      <div className="flex gap-3">
        <button onClick={onBack} className="flex items-center gap-2 bg-navy-700 hover:bg-navy-600 border border-navy-500 text-slate-300 px-5 py-2.5 rounded-md text-sm transition-colors">
          <ChevronLeft size={15} /> Back
        </button>
        <button onClick={onDeploy} disabled={deploying}
          className="flex items-center gap-2 bg-red-600 hover:bg-red-500 disabled:opacity-50 text-white font-bold px-6 py-2.5 rounded-md text-sm transition-colors">
          {deploying ? <Loader2 size={15} className="animate-spin" /> : <Boxes size={15} />}
          {deploying ? 'Starting…' : 'Deploy OpenShift'}
        </button>
      </div>
    </div>
  )
}

// ── Step 6: Progress ──────────────────────────────────────────────────────────

function StepProgress({ jobId }) {
  const [job, setJob]       = useState(null)
  const [copied, setCopied] = useState('')
  const logRef              = useRef(null)
  const timerRef            = useRef(null)

  const poll = useCallback(async () => {
    try {
      const r = await api.get(`/openshift/jobs/${jobId}`)
      setJob(r.data)
    } catch (_) {}
  }, [jobId])

  useEffect(() => {
    poll()
    timerRef.current = setInterval(poll, 5000)
    return () => clearInterval(timerRef.current)
  }, [poll])

  useEffect(() => {
    if (logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight
    }
  }, [job?.logs?.length])

  const copy = (text, key) => {
    navigator.clipboard.writeText(text)
    setCopied(key)
    setTimeout(() => setCopied(''), 2000)
  }

  const isComplete = job?.status === 'complete'
  const isFailed   = job?.status === 'failed'

  const LOG_COLOR = { info: 'text-slate-300', warn: 'text-yellow-300', error: 'text-red-400' }

  return (
    <div className="space-y-5">
      {/* Status header */}
      <div className={`flex items-center gap-4 bg-navy-800 border rounded-xl p-5 ${
        isComplete ? 'border-green-500/30' :
        isFailed   ? 'border-red-500/30' :
                     'border-sky-500/30'
      }`}>
        <div className={`p-3 rounded-xl ${isComplete ? 'bg-green-500/10' : isFailed ? 'bg-red-500/10' : 'bg-sky-500/10'}`}>
          {isComplete ? <CheckCircle size={24} className="text-green-400" />
          : isFailed  ? <XCircle    size={24} className="text-red-400" />
          :             <Loader2    size={24} className="text-sky-400 animate-spin" />}
        </div>
        <div className="flex-1">
          <div className="text-slate-100 font-bold">{job?.phase || 'Starting…'}</div>
          <div className="text-slate-400 text-sm">
            {isComplete ? 'OpenShift is installed and ready!' :
             isFailed   ? 'Deployment failed — see logs below.' :
                          'Deployment in progress — this may take 45–90 minutes.'}
          </div>
        </div>
        <div className="text-right">
          <div className="text-3xl font-bold text-sky-400">{job?.progress ?? 0}%</div>
        </div>
      </div>

      {/* Progress bar */}
      <div className="w-full bg-navy-700 rounded-full h-2">
        <div className={`h-2 rounded-full transition-all duration-1000 ${isComplete ? 'bg-green-500' : isFailed ? 'bg-red-500' : 'bg-sky-500'}`}
          style={{ width: `${job?.progress ?? 0}%` }} />
      </div>

      {/* Logs */}
      <div className="bg-navy-900 border border-navy-600 rounded-xl overflow-hidden">
        <div className="px-4 py-3 border-b border-navy-700 flex items-center gap-2">
          <Terminal size={13} className="text-sky-400" />
          <span className="text-slate-300 text-xs font-semibold">Deployment Log</span>
          <span className="text-slate-600 text-xs ml-auto">{job?.logs?.length ?? 0} entries</span>
        </div>
        <div ref={logRef} className="p-4 h-72 overflow-y-auto font-mono text-xs space-y-0.5">
          {(job?.logs || []).map((entry, i) => (
            <div key={i} className={`flex gap-2 ${LOG_COLOR[entry.level] || 'text-slate-300'}`}>
              <span className="text-slate-600 flex-shrink-0">{entry.ts}</span>
              <span className="break-all">{entry.msg}</span>
            </div>
          ))}
          {!job?.logs?.length && (
            <span className="text-slate-600">Waiting for first log entry…</span>
          )}
        </div>
      </div>

      {/* Results */}
      {isComplete && job?.result && (
        <div className="bg-green-900/10 border border-green-700/40 rounded-xl p-5 space-y-4">
          <h3 className="text-green-400 font-semibold text-sm flex items-center gap-2">
            <CheckCircle size={15} /> Cluster Ready
          </h3>

          {[
            { label: 'Console URL', key: 'console_url', value: job.result.console_url, link: true },
            { label: 'API URL',     key: 'api_url',     value: job.result.api_url },
          ].map(item => (
            <div key={item.key}>
              <div className="text-slate-400 text-xs mb-1">{item.label}</div>
              <div className="flex items-center gap-2">
                <code className="text-sky-300 text-xs font-mono bg-navy-900 px-3 py-1.5 rounded flex-1 overflow-x-auto">
                  {item.value}
                </code>
                {item.link && (
                  <a href={item.value} target="_blank" rel="noopener noreferrer"
                    className="p-1.5 rounded text-sky-400 hover:bg-sky-500/10 transition-colors">
                    <ExternalLink size={14} />
                  </a>
                )}
                <button onClick={() => copy(item.value, item.key)}
                  className="p-1.5 rounded text-slate-400 hover:text-sky-300 transition-colors">
                  {copied === item.key ? <Check size={14} className="text-green-400" /> : <Copy size={14} />}
                </button>
              </div>
            </div>
          ))}

          {job.result.kubeadmin_password && (
            <div>
              <div className="text-slate-400 text-xs mb-1">kubeadmin password</div>
              <div className="flex items-center gap-2">
                <code className="text-yellow-300 text-xs font-mono bg-navy-900 px-3 py-1.5 rounded flex-1">
                  {job.result.kubeadmin_password}
                </code>
                <button onClick={() => copy(job.result.kubeadmin_password, 'pw')}
                  className="p-1.5 rounded text-slate-400 hover:text-sky-300 transition-colors">
                  {copied === 'pw' ? <Check size={14} className="text-green-400" /> : <Copy size={14} />}
                </button>
              </div>
            </div>
          )}

          <a href={`/api/openshift/jobs/${jobId}/kubeconfig`}
            className="inline-flex items-center gap-2 bg-sky-600 hover:bg-sky-500 text-white font-semibold px-4 py-2 rounded-md text-sm transition-colors">
            <Download size={14} /> Download kubeconfig
          </a>
        </div>
      )}
    </div>
  )
}

// ── Nav buttons ───────────────────────────────────────────────────────────────

function NavButtons({ onBack, onNext, nextLabel = 'Next', nextDisabled = false }) {
  return (
    <div className="flex gap-3 pt-2">
      {onNext && (
        <button onClick={onNext} disabled={nextDisabled}
          className="flex items-center gap-2 bg-sky-600 hover:bg-sky-500 disabled:opacity-40 text-white font-semibold px-5 py-2.5 rounded-md text-sm transition-colors">
          {nextLabel} <ChevronRight size={15} />
        </button>
      )}
      {onBack && (
        <button onClick={onBack}
          className="flex items-center gap-2 bg-navy-700 hover:bg-navy-600 border border-navy-500 text-slate-300 px-5 py-2.5 rounded-md text-sm transition-colors">
          <ChevronLeft size={15} /> Back
        </button>
      )}
    </div>
  )
}

// ── Past deployments ──────────────────────────────────────────────────────────

function PastDeployments({ onResume }) {
  const [jobs, setJobs]   = useState([])
  const [loading, setL]   = useState(true)

  useEffect(() => {
    api.get('/openshift/jobs')
      .then(r => setJobs(r.data.jobs || []))
      .finally(() => setL(false))
  }, [])

  if (loading || jobs.length === 0) return null

  const STATUS_COLOR = {
    complete: 'text-green-400 bg-green-500/10',
    failed:   'text-red-400 bg-red-500/10',
    pending:  'text-yellow-400 bg-yellow-500/10',
  }

  return (
    <div className="mt-6 bg-navy-800 border border-navy-600 rounded-xl overflow-hidden">
      <div className="px-5 py-3 border-b border-navy-600">
        <span className="text-slate-300 text-sm font-semibold">Previous Deployments</span>
      </div>
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-navy-700 bg-navy-700/40 text-slate-400 text-xs">
            <th className="text-left px-5 py-3 font-medium">Cluster</th>
            <th className="text-left px-5 py-3 font-medium">Version</th>
            <th className="text-left px-5 py-3 font-medium">Status</th>
            <th className="text-left px-5 py-3 font-medium">Phase</th>
            <th className="text-right px-5 py-3 font-medium"></th>
          </tr>
        </thead>
        <tbody>
          {jobs.map(j => (
            <tr key={j.id} className="border-b border-navy-700/50 hover:bg-navy-700/20">
              <td className="px-5 py-3 font-mono text-slate-200 text-xs">
                {j.config?.cluster_name || j.id}
              </td>
              <td className="px-5 py-3 text-slate-400 text-xs">{j.config?.ocp_version}</td>
              <td className="px-5 py-3">
                <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${STATUS_COLOR[j.status] || 'text-slate-400 bg-navy-700'}`}>
                  {j.status}
                </span>
              </td>
              <td className="px-5 py-3 text-slate-400 text-xs">{j.phase}</td>
              <td className="px-5 py-3 text-right">
                <button onClick={() => onResume(j.id)}
                  className="text-xs text-sky-400 hover:text-sky-300 transition-colors">
                  View logs →
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ── Main page ─────────────────────────────────────────────────────────────────

const DEFAULTS = {
  pull_secret:          '',
  deployment_type:      'sno',
  cluster_name:         '',
  base_domain:          '',
  ocp_version:          '4.21',
  ssh_public_key:       '',
  // Control plane
  cp_vcpus:             8,
  cp_ram_gb:            32,
  cp_disk_gb:           120,
  control_plane_count:  3,
  // Workers
  w_vcpus:              4,
  w_ram_gb:             16,
  w_disk_gb:            100,
  worker_count:         2,
  // Storage
  storage_path:         '/var/lib/libvirt/images',
  // Network
  libvirt_network:      'default',
  machine_cidr:         '192.168.122.0/24',
  api_vip:              '',
  ingress_vip:          '',
  cluster_cidr:         '10.128.0.0/14',
  service_cidr:         '172.30.0.0/16',
}

export default function OpenShiftPage() {
  const [step, setStep]       = useState(0)
  const [form, setForm]       = useState(DEFAULTS)
  const [jobId, setJobId]     = useState(null)
  const [deploying, setDep]   = useState(false)
  const [error, setError]     = useState('')

  const set = (k, v) => setForm(p => ({ ...p, [k]: v }))

  const deploy = async () => {
    setDep(true)
    setError('')
    try {
      const r = await api.post('/openshift/deploy', form)
      setJobId(r.data.job_id)
      setStep(6)
    } catch (e) {
      setError(e.response?.data?.error || 'Failed to start deployment')
    } finally {
      setDep(false)
    }
  }

  const stepProps = { form, set }

  return (
    <div className="max-w-3xl">
      {/* Header */}
      <div className="flex items-center gap-4 mb-6">
        <div className="p-3 bg-red-500/10 rounded-xl">
          <Boxes size={22} className="text-red-400" />
        </div>
        <div>
          <h1 className="text-slate-100 font-bold text-xl">OpenShift Deployment</h1>
          <p className="text-slate-400 text-sm">Deploy SNO or multi-node OCP on KVM via Assisted Installer</p>
        </div>
      </div>

      {/* Step bar */}
      {step < 6 && <StepBar current={step} />}

      {/* Error */}
      {error && (
        <div className="bg-red-900/30 border border-red-700 text-red-300 text-sm rounded-xl px-4 py-3 mb-5 flex gap-2">
          <XCircle size={15} className="flex-shrink-0 mt-0.5" />
          {error}
        </div>
      )}

      {/* Steps */}
      <div className="bg-navy-800 border border-navy-600 rounded-xl p-6">
        {step === 0 && <StepPreflight onNext={() => setStep(1)} />}
        {step === 1 && <StepPullSecret {...stepProps} onBack={() => setStep(0)} onNext={() => setStep(2)} />}
        {step === 2 && <StepCluster   {...stepProps} onBack={() => setStep(1)} onNext={() => setStep(3)} />}
        {step === 3 && <StepNodes     {...stepProps} onBack={() => setStep(2)} onNext={() => setStep(4)} />}
        {step === 4 && <StepNetwork   {...stepProps} onBack={() => setStep(3)} onNext={() => setStep(5)} />}
        {step === 5 && <StepReview    form={form} onBack={() => setStep(4)} onDeploy={deploy} deploying={deploying} />}
        {step === 6 && jobId && <StepProgress jobId={jobId} />}
      </div>

      {/* Past deployments (shown on step 0 only) */}
      {step === 0 && (
        <PastDeployments onResume={id => { setJobId(id); setStep(6) }} />
      )}
    </div>
  )
}
