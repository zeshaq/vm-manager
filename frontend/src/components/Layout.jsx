import { NavLink, useLocation } from 'react-router-dom'
import {
  LayoutDashboard,
  Server,
  PlusCircle,
  HardDrive,
  FolderOpen,
  Activity,
  Terminal,
  LogOut,
  Cpu,
  Container,
  Network,
  Files,
  Layers,
} from 'lucide-react'


const navItems = [
  { to: '/', label: 'Host Info', icon: Cpu, exact: true },
  { to: '/dashboard', label: 'Dashboard', icon: LayoutDashboard },
  { to: '/metrics', label: 'Metrics', icon: Activity },
  { to: '/vms', label: 'Virtual Machines', icon: Server },
  { to: '/vms/create', label: 'Create VM', icon: PlusCircle },
  { to: '/storage', label: 'Storage', icon: HardDrive },
  { to: '/docker', label: 'Docker', icon: Container },
  { to: '/network', label: 'Network', icon: Network },
  { to: '/files',      label: 'File Manager', icon: Files },
  { to: '/kubernetes', label: 'Kubernetes',   icon: Layers },
  { to: '/projects',   label: 'Projects',     icon: FolderOpen },
]

function NavItem({ to, label, icon: Icon, exact }) {
  return (
    <NavLink
      to={to}
      end={exact}
      className={({ isActive }) =>
        `flex items-center gap-3 px-4 py-3 rounded-md mx-2 my-0.5 text-sm font-medium transition-all ${
          isActive
            ? 'bg-navy-500 text-sky-400 border-l-2 border-sky-400'
            : 'text-slate-400 hover:text-sky-300 hover:bg-navy-600'
        }`
      }
    >
      <Icon size={18} />
      <span>{label}</span>
    </NavLink>
  )
}

export default function Layout({ children, username, onLogout }) {
  const location = useLocation()

  const getPageTitle = () => {
    const p = location.pathname
    if (p === '/') return 'Host Overview'
    if (p === '/dashboard') return 'System Dashboard'
    if (p === '/vms') return 'Virtual Machines'
    if (p === '/vms/create') return 'Create VM'
    if (p.endsWith('/edit')) return 'Edit VM'
    if (p.endsWith('/monitor')) return 'VM Monitor'
    if (p.startsWith('/vms/')) return 'VM Detail'
    if (p === '/storage') return 'Storage'
    if (p === '/docker') return 'Docker'
    if (p === '/metrics') return 'Metrics'
    if (p === '/network') return 'Network'
    if (p === '/files')      return 'File Manager'
    if (p === '/kubernetes') return 'Kubernetes'
    if (p === '/projects')   return 'Projects'
    return 'Hypercloud'
  }

  return (
    <div className="flex h-screen bg-navy-900 overflow-hidden">
      {/* Sidebar */}
      <aside className="w-64 bg-navy-800 border-r border-navy-400 flex flex-col flex-shrink-0">
        {/* Logo */}
        <div className="px-6 py-5 border-b border-navy-400">
          <div className="flex items-center gap-3">
            <img src="/logo.svg" alt="Hypercloud" className="w-8 h-8" />
            <span className="text-sky-400 font-bold text-lg tracking-tight">Hypercloud</span>
          </div>
        </div>

        {/* Nav */}
        <nav className="flex-1 py-4 overflow-y-auto">
          {navItems.map(item => (
            <NavItem key={item.to} {...item} />
          ))}

          {/* External links */}
          <div className="mt-4 pt-4 border-t border-navy-500 mx-2">
            <a
              href="/host-terminal"
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-3 px-4 py-3 rounded-md text-sm font-medium text-slate-400 hover:text-sky-300 hover:bg-navy-600 transition-all"
            >
              <Terminal size={18} />
              <span>Host Terminal</span>
            </a>
          </div>
        </nav>

        {/* User section */}
        <div className="px-4 py-4 border-t border-navy-400">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <div className="w-7 h-7 rounded-full bg-sky-500 flex items-center justify-center text-white text-xs font-bold uppercase">
                {username ? username[0] : 'U'}
              </div>
              <span className="text-slate-300 text-sm font-medium">{username || 'User'}</span>
            </div>
            <button
              onClick={onLogout}
              className="text-slate-400 hover:text-red-400 transition-colors p-1 rounded"
              title="Logout"
            >
              <LogOut size={16} />
            </button>
          </div>
        </div>
      </aside>

      {/* Main content */}
      <div className="flex-1 flex flex-col overflow-hidden">
        {/* Top bar */}
        <header className="bg-navy-800 border-b border-navy-400 px-6 py-4 flex-shrink-0">
          <h1 className="text-slate-200 font-semibold text-lg">{getPageTitle()}</h1>
        </header>

        {/* Page content */}
        <main className="flex-1 overflow-y-auto p-6">
          {children}
        </main>
      </div>
    </div>
  )
}
