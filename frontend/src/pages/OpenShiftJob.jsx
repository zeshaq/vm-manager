import { useState, useEffect, useCallback, useRef } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import {
  Boxes, ArrowLeft, CheckCircle, XCircle, Loader2,
  Terminal, Download, Copy, Check, ExternalLink, RefreshCw,
} from 'lucide-react'
import api from '../api'

export default function OpenShiftJob() {
  const { jobId }           = useParams()
  const navigate            = useNavigate()
  const [job, setJob]       = useState(null)
  const [copied, setCopied] = useState('')
  const [loading, setLoad]  = useState(true)
  const logRef              = useRef(null)
  const timerRef            = useRef(null)

  const poll = useCallback(async (silent = false) => {
    try {
      const r = await api.get(`/openshift/jobs/${jobId}`)
      setJob(r.data)
    } catch (e) {
      if (e.response?.status === 404) navigate('/openshift')
    } finally {
      if (!silent) setLoad(false)
    }
  }, [jobId, navigate])

  useEffect(() => {
    poll()
    timerRef.current = setInterval(() => poll(true), 5000)
    return () => clearInterval(timerRef.current)
  }, [poll])

  // Auto-scroll logs
  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
  }, [job?.logs?.length])

  // Stop polling when done
  useEffect(() => {
    if (job?.status === 'complete' || job?.status === 'failed') {
      clearInterval(timerRef.current)
    }
  }, [job?.status])

  const copy = (text, key) => {
    navigator.clipboard.writeText(text)
    setCopied(key)
    setTimeout(() => setCopied(''), 2000)
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-24 text-slate-500 gap-2">
        <Loader2 size={16} className="animate-spin" /> Loading…
      </div>
    )
  }
  if (!job) return null

  const isComplete = job.status === 'complete'
  const isFailed   = job.status === 'failed'
  const isRunning  = !isComplete && !isFailed

  const LOG_COLOR = { info: 'text-slate-300', warn: 'text-yellow-300', error: 'text-red-400' }

  const borderColor = isComplete ? 'border-green-500/30' : isFailed ? 'border-red-500/30' : 'border-sky-500/30'
  const bgColor     = isComplete ? 'bg-green-500/10'      : isFailed ? 'bg-red-500/10'      : 'bg-sky-500/10'

  return (
    <div className="max-w-3xl space-y-5">

      {/* Back + header */}
      <div className="flex items-center gap-3">
        <button onClick={() => navigate('/openshift')}
          className="p-2 rounded-md text-slate-400 hover:text-sky-400 hover:bg-navy-700 transition-colors">
          <ArrowLeft size={16} />
        </button>
        <div className="p-2.5 bg-red-500/10 rounded-xl">
          <Boxes size={18} className="text-red-400" />
        </div>
        <div>
          <h1 className="text-slate-100 font-bold text-lg">
            {job.config?.cluster_name || job.id}
          </h1>
          <div className="flex items-center gap-3 text-xs text-slate-500">
            <span className={`font-mono px-1.5 py-0.5 rounded ${
              job.config?.deployment_type === 'sno'
                ? 'bg-purple-500/15 text-purple-300'
                : 'bg-blue-500/15 text-blue-300'
            }`}>
              {job.config?.deployment_type === 'sno' ? 'SNO' : 'Multi-node'}
            </span>
            <span>OCP {job.config?.ocp_version}</span>
            <span className="font-mono">{job.id}</span>
          </div>
        </div>
      </div>

      {/* Status card */}
      <div className={`flex items-center gap-4 bg-navy-800 border ${borderColor} rounded-xl p-5`}>
        <div className={`p-3 rounded-xl ${bgColor}`}>
          {isComplete ? <CheckCircle size={24} className="text-green-400" />
          : isFailed  ? <XCircle    size={24} className="text-red-400" />
          :             <Loader2    size={24} className="text-sky-400 animate-spin" />}
        </div>
        <div className="flex-1">
          <div className="text-slate-100 font-bold">{job.phase || 'Starting…'}</div>
          <div className="text-slate-400 text-sm mt-0.5">
            {isComplete ? 'OpenShift is installed and ready!'
            : isFailed  ? 'Deployment failed — see logs below.'
            :             'Deployment in progress — this may take 45–90 minutes.'}
          </div>
        </div>
        <div className="flex flex-col items-end gap-2">
          <div className="text-3xl font-bold text-sky-400">{job.progress ?? 0}%</div>
          {isFailed && (
            <button onClick={() => navigate('/openshift/deploy')}
              className="flex items-center gap-1.5 bg-sky-600 hover:bg-sky-500 text-white text-xs font-semibold px-3 py-1.5 rounded-md transition-colors">
              <RefreshCw size={12} /> New Deployment
            </button>
          )}
        </div>
      </div>

      {/* Progress bar */}
      <div className="w-full bg-navy-700 rounded-full h-2">
        <div className={`h-2 rounded-full transition-all duration-1000 ${
          isComplete ? 'bg-green-500' : isFailed ? 'bg-red-500' : 'bg-sky-500'
        }`} style={{ width: `${job.progress ?? 0}%` }} />
      </div>

      {/* Cluster config summary */}
      {job.config && (
        <div className="bg-navy-800 border border-navy-600 rounded-xl overflow-hidden">
          <div className="px-4 py-3 border-b border-navy-700 text-xs font-semibold text-slate-400 uppercase tracking-wider">
            Configuration
          </div>
          <div className="grid grid-cols-2 gap-0">
            {[
              ['Cluster Name',  job.config.cluster_name],
              ['Base Domain',   job.config.base_domain],
              ['OCP Version',   job.config.ocp_version],
              ['Type',          job.config.deployment_type === 'sno' ? 'Single Node (SNO)' : 'Multi-node'],
              ['Network',       job.config.libvirt_network],
              ['Machine CIDR',  job.config.machine_cidr],
            ].map(([k, v]) => v ? (
              <div key={k} className="px-4 py-2.5 border-b border-navy-700/50 flex gap-3">
                <span className="text-slate-500 text-xs w-28 flex-shrink-0">{k}</span>
                <span className="text-slate-200 text-xs font-mono">{v}</span>
              </div>
            ) : null)}
          </div>
        </div>
      )}

      {/* Logs */}
      <div className="bg-navy-900 border border-navy-600 rounded-xl overflow-hidden">
        <div className="px-4 py-3 border-b border-navy-700 flex items-center gap-2">
          <Terminal size={13} className="text-sky-400" />
          <span className="text-slate-300 text-xs font-semibold">Deployment Log</span>
          <span className="text-slate-600 text-xs ml-auto">{job.logs?.length ?? 0} entries</span>
        </div>
        <div ref={logRef} className="p-4 h-80 overflow-y-auto font-mono text-xs space-y-0.5">
          {(job.logs || []).map((entry, i) => (
            <div key={i} className={`flex gap-2 ${LOG_COLOR[entry.level] || 'text-slate-300'}`}>
              <span className="text-slate-600 flex-shrink-0">{entry.ts}</span>
              <span className="break-all">{entry.msg}</span>
            </div>
          ))}
          {!job.logs?.length && (
            <span className="text-slate-600">Waiting for first log entry…</span>
          )}
        </div>
      </div>

      {/* Results */}
      {isComplete && job.result && (
        <div className="bg-green-900/10 border border-green-700/40 rounded-xl p-5 space-y-4">
          <h3 className="text-green-400 font-semibold text-sm flex items-center gap-2">
            <CheckCircle size={15} /> Cluster Ready
          </h3>

          {[
            { label: 'Console URL', key: 'console_url', value: job.result.console_url, link: true },
            { label: 'API URL',     key: 'api_url',     value: job.result.api_url },
          ].filter(i => i.value).map(item => (
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
