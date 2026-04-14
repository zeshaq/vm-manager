import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Server, Eye, EyeOff } from 'lucide-react'
import api from '../api'

export default function Login({ onLogin }) {
  const navigate = useNavigate()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [showPass, setShowPass] = useState(false)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const handleSubmit = async e => {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      const res = await api.post('/login', { username, password })
      if (res.data.success) {
        await onLogin()
        navigate('/', { replace: true })
      } else {
        setError(res.data.error || 'Invalid credentials')
      }
    } catch (err) {
      setError(err.response?.data?.error || 'Login failed')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen bg-navy-900 flex items-center justify-center px-4">
      <div className="w-full max-w-md">
        {/* Card */}
        <div className="bg-navy-700 border border-navy-400 rounded-xl shadow-2xl p-8">
          {/* Logo */}
          <div className="flex flex-col items-center mb-8">
            <div className="w-14 h-14 bg-navy-500 border border-navy-300 rounded-xl flex items-center justify-center mb-4">
              <Server size={28} className="text-sky-400" />
            </div>
            <h1 className="text-2xl font-bold text-sky-400">VM Manager</h1>
            <p className="text-slate-400 text-sm mt-1">Sign in to manage your virtual machines</p>
          </div>

          {error && (
            <div className="bg-red-900/50 border border-red-700 text-red-300 text-sm rounded-md px-4 py-3 mb-6">
              {error}
            </div>
          )}

          <form onSubmit={handleSubmit} className="space-y-5">
            <div>
              <label className="block text-sm font-medium text-slate-300 mb-1.5">Username</label>
              <input
                type="text"
                value={username}
                onChange={e => setUsername(e.target.value)}
                className="w-full bg-navy-800 border border-navy-400 text-slate-200 focus:border-sky-500 focus:outline-none rounded-md px-3 py-2.5"
                placeholder="Enter your username"
                autoComplete="username"
                required
              />
            </div>

            <div>
              <label className="block text-sm font-medium text-slate-300 mb-1.5">Password</label>
              <div className="relative">
                <input
                  type={showPass ? 'text' : 'password'}
                  value={password}
                  onChange={e => setPassword(e.target.value)}
                  className="w-full bg-navy-800 border border-navy-400 text-slate-200 focus:border-sky-500 focus:outline-none rounded-md px-3 py-2.5 pr-10"
                  placeholder="Enter your password"
                  autoComplete="current-password"
                  required
                />
                <button
                  type="button"
                  onClick={() => setShowPass(!showPass)}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-200"
                >
                  {showPass ? <EyeOff size={16} /> : <Eye size={16} />}
                </button>
              </div>
            </div>

            <button
              type="submit"
              disabled={loading}
              className="w-full bg-sky-500 hover:bg-sky-400 disabled:opacity-50 disabled:cursor-not-allowed text-white font-semibold py-2.5 rounded-md transition-colors mt-2"
            >
              {loading ? 'Signing in...' : 'Sign In'}
            </button>
          </form>
        </div>
      </div>
    </div>
  )
}
