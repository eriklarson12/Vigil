import { Link, Route, Routes } from 'react-router-dom'
import { API_BASE } from './api'
import { IncidentDetail } from './pages/IncidentDetail'
import { IncidentList } from './pages/IncidentList'

export default function App() {
  return (
    <div className="mx-auto max-w-7xl px-6 py-8">
      <header className="mb-6 flex items-baseline justify-between gap-4">
        <Link to="/" className="text-xl font-semibold tracking-tight text-slate-100">
          Vigil <span className="text-slate-600">/</span>{' '}
          <span className="font-normal text-slate-400">incidents</span>
        </Link>
        {/* Dev only: knowing which API you are pointed at matters locally, but on
            the public dashboard it is clutter that advertises the backend origin. */}
        {import.meta.env.DEV ? (
          <span className="font-mono text-xs text-slate-600">{API_BASE}</span>
        ) : null}
      </header>

      <Routes>
        <Route path="/" element={<IncidentList />} />
        <Route path="/incidents/:id" element={<IncidentDetail />} />
        <Route path="*" element={<p className="text-slate-400">Not found.</p>} />
      </Routes>
    </div>
  )
}
