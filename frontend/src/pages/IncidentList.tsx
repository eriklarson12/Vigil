import { Link } from 'react-router-dom'
import { fetchIncidents, usePolling } from '../api'
import { SeverityChip, StatusChip } from '../components/SeverityChip'
import { formatDuration, formatTimestamp } from '../format'

export function IncidentList() {
  const { data, error, loading } = usePolling(fetchIncidents, 10_000)

  if (loading && !data) return <p className="text-sm text-slate-500">Loading incidents…</p>
  if (error && !data) return <ApiError error={error} />

  const incidents = data ?? []
  if (incidents.length === 0) {
    return (
      <div className="rounded-lg border border-slate-800 bg-slate-900/50 p-8 text-center">
        <p className="text-slate-300">No incidents yet.</p>
        <p className="mt-2 text-sm text-slate-500">
          Fire one from the simulator:
          <code className="ml-2 rounded bg-slate-800 px-2 py-1 font-mono text-xs text-sky-300">
            uv run vigil-sim demo --scenario bad_deploy
          </code>
        </p>
      </div>
    )
  }

  return (
    <div className="overflow-x-auto rounded-lg border border-slate-800">
      <table className="w-full text-sm">
        <thead className="bg-slate-900 text-left text-xs uppercase tracking-wider text-slate-500">
          <tr>
            <th className="px-4 py-2.5 font-medium">Severity</th>
            <th className="px-4 py-2.5 font-medium">Title</th>
            <th className="px-4 py-2.5 font-medium">Service</th>
            <th className="px-4 py-2.5 font-medium">Status</th>
            <th className="px-4 py-2.5 font-medium">Opened</th>
            <th className="px-4 py-2.5 font-medium">Resolved</th>
            <th className="px-4 py-2.5 font-medium">Duration</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-800 bg-slate-900/30">
          {incidents.map((i) => (
            <tr key={i.id} className="hover:bg-slate-800/50">
              <td className="px-4 py-2.5">
                <SeverityChip severity={i.severity} />
              </td>
              <td className="px-4 py-2.5">
                <Link to={`/incidents/${i.id}`} className="text-slate-100 hover:text-sky-300">
                  {i.title}
                </Link>
                {i.has_postmortem ? (
                  <span className="ml-2 text-xs text-emerald-400" title="Postmortem written">
                    📝
                  </span>
                ) : null}
              </td>
              <td className="px-4 py-2.5 font-mono text-xs text-slate-400">{i.service}</td>
              <td className="px-4 py-2.5">
                <StatusChip status={i.status} />
              </td>
              <td className="px-4 py-2.5 text-xs text-slate-400">{formatTimestamp(i.created_at)}</td>
              <td className="px-4 py-2.5 text-xs text-slate-400">
                {formatTimestamp(i.resolved_at)}
              </td>
              <td className="px-4 py-2.5 font-mono text-xs text-slate-300">
                {formatDuration(i.created_at, i.resolved_at)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function ApiError({ error }: { error: Error }) {
  return (
    <div className="rounded-lg border border-rose-900/60 bg-rose-950/30 p-4 text-sm">
      <p className="text-rose-300">Could not reach the Vigil API.</p>
      <p className="mt-1 font-mono text-xs text-rose-400/80">{error.message}</p>
      <p className="mt-2 text-xs text-slate-500">
        Is <code className="font-mono">uv run vigil-serve</code> running? The dashboard talks to{' '}
        <code className="font-mono">VITE_API_URL</code>.
      </p>
    </div>
  )
}
