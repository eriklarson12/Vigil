import { Link, NavLink, Route, Routes } from 'react-router-dom'
import { API_BASE } from './api'
import { IncidentDetail } from './pages/IncidentDetail'
import { IncidentList } from './pages/IncidentList'
import { StatsPage } from './pages/Stats'

export default function App() {
  return (
    <div className="mx-auto max-w-7xl px-6 py-8">
      <header className="mb-6 flex items-baseline justify-between gap-4">
        <div className="flex items-baseline gap-4">
          <Link to="/" className="text-xl font-semibold tracking-tight text-slate-100">
            Vigil
          </Link>
          <nav className="flex items-baseline gap-3 text-sm">
            <Tab to="/">incidents</Tab>
            <Tab to="/stats">stats</Tab>
          </nav>
        </div>
        {/* Dev only: knowing which API you are pointed at matters locally, but on
            the public dashboard it is clutter that advertises the backend origin. */}
        {import.meta.env.DEV ? (
          <span className="font-mono text-xs text-slate-600">{API_BASE}</span>
        ) : null}
      </header>

      <Routes>
        <Route path="/" element={<IncidentList />} />
        <Route path="/incidents/:id" element={<IncidentDetail />} />
        <Route path="/stats" element={<StatsPage />} />
        <Route path="*" element={<p className="text-slate-400">Not found.</p>} />
      </Routes>
    </div>
  )
}

function Tab({ to, children }: { to: string; children: React.ReactNode }) {
  return (
    <NavLink
      to={to}
      end
      className={({ isActive }) =>
        isActive ? 'text-sky-300' : 'text-slate-500 hover:text-slate-300'
      }
    >
      {children}
    </NavLink>
  )
}
