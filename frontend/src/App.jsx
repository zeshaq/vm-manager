import { Routes, Route, Navigate } from 'react-router-dom'
import { useState, useEffect } from 'react'
import api from './api'
import Layout from './components/Layout'
import Login from './pages/Login'
import Home from './pages/Home'
import VMList from './pages/VMList'
import VMDetail from './pages/VMDetail'
import CreateVM from './pages/CreateVM'
import EditVM from './pages/EditVM'
import Storage from './pages/Storage'
import Projects from './pages/Projects'
import Dashboard from './pages/Dashboard'
import Monitor from './pages/Monitor'
import Docker from './pages/Docker'
import NetworkMgmt from './pages/NetworkMgmt'

function PrivateRoute({ children, authState }) {
  if (authState === 'loading') {
    return (
      <div className="flex items-center justify-center h-screen bg-navy-900">
        <div className="text-sky-400 text-lg">Loading...</div>
      </div>
    )
  }
  if (!authState.authenticated) {
    return <Navigate to="/login" replace />
  }
  return children
}

export default function App() {
  const [authState, setAuthState] = useState('loading')

  useEffect(() => {
    api.get('/auth/check')
      .then(res => setAuthState(res.data))
      .catch(() => setAuthState({ authenticated: false }))
  }, [])

  const handleLogout = async () => {
    await api.post('/logout')
    setAuthState({ authenticated: false })
    window.location.href = '/login'
  }

  return (
    <Routes>
      <Route path="/login" element={<Login onLogin={() => api.get('/auth/check').then(r => setAuthState(r.data))} />} />
      <Route
        path="/*"
        element={
          <PrivateRoute authState={authState}>
            <Layout username={authState?.username} onLogout={handleLogout}>
              <Routes>
                <Route path="/" element={<Home />} />
                <Route path="/vms" element={<VMList />} />
                <Route path="/vms/create" element={<CreateVM />} />
                <Route path="/vms/:uuid" element={<VMDetail />} />
                <Route path="/vms/:uuid/edit" element={<EditVM />} />
                <Route path="/vms/:uuid/monitor" element={<Monitor />} />
                <Route path="/storage" element={<Storage />} />
                <Route path="/docker" element={<Docker />} />
                <Route path="/network" element={<NetworkMgmt />} />
                <Route path="/projects" element={<Projects />} />
                <Route path="/dashboard" element={<Dashboard />} />
                <Route path="*" element={<Navigate to="/" replace />} />
              </Routes>
            </Layout>
          </PrivateRoute>
        }
      />
    </Routes>
  )
}
