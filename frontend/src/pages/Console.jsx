import { useEffect, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'
import RFB from '@novnc/novnc/lib/rfb'
import {
  Maximize2, Minimize2, Clipboard, Power, RefreshCw,
  Monitor, ChevronLeft,
} from 'lucide-react'

export default function Console() {
  const { uuid } = useParams()
  const screenRef  = useRef(null)
  const rfbRef     = useRef(null)
  const [status,   setStatus]   = useState('connecting')  // connecting|connected|disconnected|error
  const [vmName,   setVmName]   = useState(uuid?.slice(0, 8))
  const [fullscreen, setFullscreen] = useState(false)
  const [clipText, setClipText] = useState('')
  const [showClip, setShowClip] = useState(false)
  const [viewOnly, setViewOnly] = useState(false)

  // Fetch VM name
  useEffect(() => {
    fetch(`/api/vms/${uuid}`)
      .then(r => r.json())
      .then(d => { if (d.name) setVmName(d.name) })
      .catch(() => {})
  }, [uuid])

  // Connect noVNC
  useEffect(() => {
    if (!screenRef.current) return

    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
    const wsUrl = `${proto}://${window.location.host}/ws/vnc/${uuid}`

    const rfb = new RFB(screenRef.current, wsUrl, {
      wsProtocols: ['binary'],
      credentials:  {},
    })

    rfb.scaleViewport  = true
    rfb.resizeSession  = false
    rfb.viewOnly       = viewOnly

    rfb.addEventListener('connect',    ()    => setStatus('connected'))
    rfb.addEventListener('disconnect', (e)   => setStatus(e.detail?.clean ? 'disconnected' : 'error'))
    rfb.addEventListener('credentialsrequired', () => {
      // Most libvirt VNC has no password — send empty
      rfb.sendCredentials({ password: '' })
    })
    rfb.addEventListener('desktopname', e => {
      if (e.detail?.name) setVmName(e.detail.name)
    })

    rfbRef.current = rfb
    return () => {
      try { rfb.disconnect() } catch (_) {}
    }
  }, [uuid])

  // Sync viewOnly after connect
  useEffect(() => {
    if (rfbRef.current) rfbRef.current.viewOnly = viewOnly
  }, [viewOnly])

  // Fullscreen
  const toggleFullscreen = () => {
    if (!document.fullscreenElement) {
      document.documentElement.requestFullscreen()
      setFullscreen(true)
    } else {
      document.exitFullscreen()
      setFullscreen(false)
    }
  }

  useEffect(() => {
    const handler = () => setFullscreen(!!document.fullscreenElement)
    document.addEventListener('fullscreenchange', handler)
    return () => document.removeEventListener('fullscreenchange', handler)
  }, [])

  // Clipboard paste → VM
  const pasteToVm = () => {
    if (rfbRef.current && clipText) {
      rfbRef.current.clipboardPasteFrom(clipText)
    }
    setShowClip(false)
  }

  // Ctrl+Alt+Del
  const sendCAD = () => {
    rfbRef.current?.sendCtrlAltDel()
  }

  const reconnect = () => {
    window.location.reload()
  }

  const STATUS_COLOR = {
    connecting:   'text-sky-400',
    connected:    'text-emerald-400',
    disconnected: 'text-slate-400',
    error:        'text-red-400',
  }

  return (
    <div className="flex flex-col h-screen bg-gray-950 text-white select-none">

      {/* Top bar */}
      <div className="flex items-center gap-3 px-4 py-2 bg-navy-900 border-b border-navy-600 flex-shrink-0 z-10">
        {/* Back */}
        <button
          onClick={() => window.close()}
          className="text-slate-400 hover:text-slate-200 transition-colors"
          title="Close"
        >
          <ChevronLeft size={18}/>
        </button>

        <Monitor size={16} className="text-sky-400"/>
        <span className="text-slate-200 font-medium text-sm flex-1">{vmName}</span>

        {/* Status */}
        <span className={`text-xs font-medium ${STATUS_COLOR[status]}`}>
          {status === 'connecting'   && '● Connecting…'}
          {status === 'connected'    && '● Connected'}
          {status === 'disconnected' && '● Disconnected'}
          {status === 'error'        && '● Connection failed'}
        </span>

        <div className="flex items-center gap-1 ml-2">
          {/* Ctrl+Alt+Del */}
          <button
            onClick={sendCAD}
            disabled={status !== 'connected'}
            className="px-2 py-1 text-xs rounded text-slate-400 hover:text-slate-200 hover:bg-navy-700 disabled:opacity-40 transition-colors"
            title="Send Ctrl+Alt+Del"
          >
            Ctrl+Alt+Del
          </button>

          {/* Clipboard */}
          <div className="relative">
            <button
              onClick={() => setShowClip(s => !s)}
              disabled={status !== 'connected'}
              className="p-1.5 rounded text-slate-400 hover:text-slate-200 hover:bg-navy-700 disabled:opacity-40 transition-colors"
              title="Paste text to VM"
            >
              <Clipboard size={15}/>
            </button>
            {showClip && (
              <div className="absolute right-0 top-9 w-72 bg-navy-800 border border-navy-500 rounded-lg shadow-xl p-3 z-50">
                <p className="text-xs text-slate-400 mb-2">Text to paste into VM</p>
                <textarea
                  className="w-full h-24 bg-navy-700 border border-navy-500 rounded px-2 py-1.5 text-sm text-slate-100 resize-none focus:outline-none focus:border-sky-500"
                  value={clipText}
                  onChange={e => setClipText(e.target.value)}
                  placeholder="Type or paste here…"
                  autoFocus
                />
                <div className="flex gap-2 mt-2">
                  <button
                    onClick={pasteToVm}
                    className="flex-1 py-1.5 bg-sky-600 hover:bg-sky-500 rounded text-sm font-medium transition-colors"
                  >
                    Send
                  </button>
                  <button
                    onClick={() => setShowClip(false)}
                    className="px-3 py-1.5 text-slate-400 hover:text-slate-200 rounded hover:bg-navy-700 transition-colors text-sm"
                  >
                    Cancel
                  </button>
                </div>
              </div>
            )}
          </div>

          {/* View only toggle */}
          <button
            onClick={() => setViewOnly(v => !v)}
            className={`p-1.5 rounded text-xs transition-colors ${
              viewOnly
                ? 'bg-amber-500/20 text-amber-400 border border-amber-500/30'
                : 'text-slate-400 hover:text-slate-200 hover:bg-navy-700'
            }`}
            title={viewOnly ? 'View only (click to enable input)' : 'Input enabled'}
          >
            {viewOnly ? 'View only' : 'Interactive'}
          </button>

          {/* Reconnect */}
          {(status === 'disconnected' || status === 'error') && (
            <button
              onClick={reconnect}
              className="p-1.5 rounded text-slate-400 hover:text-emerald-400 hover:bg-navy-700 transition-colors"
              title="Reconnect"
            >
              <RefreshCw size={15}/>
            </button>
          )}

          {/* Fullscreen */}
          <button
            onClick={toggleFullscreen}
            className="p-1.5 rounded text-slate-400 hover:text-slate-200 hover:bg-navy-700 transition-colors"
            title={fullscreen ? 'Exit fullscreen' : 'Fullscreen'}
          >
            {fullscreen ? <Minimize2 size={15}/> : <Maximize2 size={15}/>}
          </button>
        </div>
      </div>

      {/* VNC canvas area */}
      <div className="flex-1 relative overflow-hidden bg-black">
        {status === 'connecting' && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 z-10 pointer-events-none">
            <RefreshCw size={28} className="text-sky-400 animate-spin"/>
            <p className="text-slate-400 text-sm">Connecting to {vmName}…</p>
          </div>
        )}
        {(status === 'disconnected' || status === 'error') && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 z-10">
            <Power size={28} className={status === 'error' ? 'text-red-400' : 'text-slate-500'}/>
            <p className="text-slate-300 font-medium">
              {status === 'error' ? 'Connection failed' : 'Disconnected'}
            </p>
            <p className="text-slate-500 text-sm">VM may be stopped or VNC unavailable</p>
            <button
              onClick={reconnect}
              className="mt-2 px-4 py-2 bg-sky-600 hover:bg-sky-500 rounded text-sm font-medium transition-colors"
            >
              Reconnect
            </button>
          </div>
        )}
        {/* noVNC mounts here */}
        <div
          ref={screenRef}
          className="w-full h-full"
          style={{ cursor: status === 'connected' ? 'none' : 'default' }}
        />
      </div>
    </div>
  )
}
