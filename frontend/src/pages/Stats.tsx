import { fetchStats, usePolling } from '../api'
import { Empty, Panel } from '../components/Panel'
import { SeverityChip } from '../components/SeverityChip'
import { WeeklyBars } from '../components/WeeklyBars'
import { formatSeconds, percent } from '../format'
import type { Stats } from '../types'
import { ApiError } from './IncidentList'

export function StatsPage() {
  const { data, error, loading } = usePolling(fetchStats, 10_000)

  if (loading && !data) return <p className="text-sm text-slate-500">Loading stats…</p>
  if (error && !data) return <ApiError error={error} />
  if (!data) return null

  return <StatsView stats={data} />
}

/** Split from the page so the render test can drive it with a fixture. */
export function StatsView({ stats }: { stats: Stats }) {
  const { overall, triage, llm } = stats
  const culpritRate = triage.triaged > 0 ? triage.culprit_named / triage.triaged : null

  return (
    <div className="space-y-4">
      <Panel title="Response times" subtitle={`${overall.mtta_n} briefed · ${overall.mttr_n} resolved`}>
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-6">
          <Metric label="MTTA p50" value={formatSeconds(overall.mtta_p50)} hint="alert to brief" />
          <Metric label="MTTA p90" value={formatSeconds(overall.mtta_p90)} />
          <Metric label="MTTR p50" value={formatSeconds(overall.mttr_p50)} hint="alert to resolved" />
          <Metric label="MTTR p90" value={formatSeconds(overall.mttr_p90)} />
          <Metric label="Open" value={String(overall.open)} />
          <Metric label="Resolved" value={String(overall.resolved)} />
        </div>
        {overall.mtta_n === 1 ? (
          <p className="mt-3 text-xs text-slate-500">
            One sample, so p50 and p90 are the same number.
          </p>
        ) : null}
      </Panel>

      <Panel title="Triage quality" subtitle={`${triage.triaged} triaged`}>
        {triage.triaged === 0 ? (
          <Empty>No incident has finished triage yet.</Empty>
        ) : (
          <>
            <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
              <Metric
                label="Culprit named"
                value={percent(culpritRate)}
                hint={`${triage.culprit_named} of ${triage.triaged}`}
              />
              <Metric label="Candidates scored" value={median(triage.scored_p50)} hint="median" />
              <Metric label="Runbook chunks" value={median(triage.chunks_p50)} hint="median" />
              <Metric
                label="Degraded runs"
                value={String(triage.degraded)}
                hint="brief posted anyway"
              />
            </div>
            {triage.degraded_nodes.length > 0 ? (
              <div className="mt-4 flex flex-wrap gap-2">
                {triage.degraded_nodes.map((d) => (
                  <span
                    key={d.node}
                    className="rounded border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 font-mono text-xs text-amber-300"
                  >
                    {d.node} ×{d.count}
                  </span>
                ))}
              </div>
            ) : null}
          </>
        )}
      </Panel>

      <Panel title="Model spend" subtitle={`ceiling ${llm.ceiling} calls per incident`}>
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <Metric label="Calls per incident" value={median(llm.total_mean)} hint="mean, incl. postmortem" />
          <Metric label="Triage only" value={median(llm.triage_mean)} hint="rank + brief" />
          <Metric
            label="Over ceiling"
            value={String(llm.over_ceiling)}
            hint={llm.over_ceiling === 0 ? 'constraint holds' : 'constraint violated'}
            alert={llm.over_ceiling > 0}
          />
          <Metric
            label="Budget today"
            value={`${llm.today_used} / ${llm.daily_budget}`}
            hint="daily generation calls"
          />
        </div>
      </Panel>

      <Panel title="Time to brief, by week" subtitle="median, last 8 weeks">
        <WeeklyBars weeks={stats.by_week} />
      </Panel>

      <Panel title="By severity">
        {stats.by_severity.length === 0 ? (
          <Empty>No incidents yet.</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-xs uppercase tracking-wider text-slate-500">
                <tr>
                  <th className="py-2 font-medium">Severity</th>
                  <th className="py-2 font-medium">Open</th>
                  <th className="py-2 font-medium">Resolved</th>
                  <th className="py-2 font-medium">MTTA p50</th>
                  <th className="py-2 font-medium">MTTA p90</th>
                  <th className="py-2 font-medium">MTTR p50</th>
                  <th className="py-2 font-medium">MTTR p90</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {stats.by_severity.map((s) => (
                  <tr key={s.severity ?? 'unset'}>
                    <td className="py-2">
                      <SeverityChip severity={s.severity} />
                    </td>
                    <td className="py-2 font-mono text-xs text-slate-300">{s.open}</td>
                    <td className="py-2 font-mono text-xs text-slate-300">{s.resolved}</td>
                    <td className="py-2 font-mono text-xs text-slate-300">
                      {formatSeconds(s.mtta_p50)}
                    </td>
                    <td className="py-2 font-mono text-xs text-slate-400">
                      {formatSeconds(s.mtta_p90)}
                    </td>
                    <td className="py-2 font-mono text-xs text-slate-300">
                      {formatSeconds(s.mttr_p50)}
                    </td>
                    <td className="py-2 font-mono text-xs text-slate-400">
                      {formatSeconds(s.mttr_p90)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  )
}

function Metric({
  label,
  value,
  hint,
  alert = false,
}: {
  label: string
  value: string
  hint?: string
  alert?: boolean
}) {
  return (
    <div>
      <div className="text-xs uppercase tracking-wider text-slate-500">{label}</div>
      <div
        className={`mt-1 font-mono text-2xl ${alert ? 'text-rose-300' : 'text-slate-100'}`}
      >
        {value}
      </div>
      {hint ? <div className="mt-0.5 text-xs text-slate-600">{hint}</div> : null}
    </div>
  )
}

const median = (n: number | null): string => (n === null ? '—' : String(n))
