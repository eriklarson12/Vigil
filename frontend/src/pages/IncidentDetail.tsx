import { useCallback } from 'react'
import { Link, useParams } from 'react-router-dom'
import { fetchIncident, usePolling } from '../api'
import { Brief } from '../components/Brief'
import { CommitCandidates } from '../components/CommitCandidates'
import { Empty, Panel } from '../components/Panel'
import { Postmortem } from '../components/Postmortem'
import { SeverityChip, StatusChip } from '../components/SeverityChip'
import { Timeline } from '../components/Timeline'
import { formatDuration, formatTimestamp } from '../format'
import type { IncidentDetail as Detail, SlackBriefPayload } from '../types'
import { ApiError } from './IncidentList'

export function IncidentDetail() {
  const { id = '' } = useParams()
  const fetcher = useCallback((signal: AbortSignal) => fetchIncident(id, signal), [id])
  const { data: detail, error, loading } = usePolling(fetcher, 10_000, {
    deps: [id],
    // Nothing changes after the postmortem lands — stop hitting the API.
    stopWhen: (d) => d.incident.status === 'postmortem_done',
  })

  if (loading && !detail) return <p className="text-sm text-slate-500">Loading incident…</p>
  if (error && !detail) return <ApiError error={error} />
  if (!detail) return <Empty>Incident not found.</Empty>

  const brief = findBrief(detail)
  const triage = detail.events.find((e) => e.event_type === 'triage_finalized')
  const culprit = (triage?.payload?.culprit as string | null | undefined) ?? null

  return (
    <div className="space-y-4">
      <Header detail={detail} />

      <div className="grid gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <div className="space-y-4">
          <Panel title="Commit candidates" subtitle={`${detail.commit_candidates.length} scored`}>
            <CommitCandidates
              candidates={detail.commit_candidates}
              culpritSha={culprit}
              triageFinalized={Boolean(triage)}
            />
          </Panel>

          <Panel title="Slack brief">
            <Brief payload={brief.payload} postedWithoutPayload={brief.postedWithoutPayload} />
          </Panel>

          <Panel title="Postmortem">
            <Postmortem data={detail.postmortem} />
          </Panel>
        </div>

        <Panel title="Timeline" subtitle={`${detail.events.length} events`}>
          <Timeline events={detail.events} />
        </Panel>
      </div>
    </div>
  )
}

function Header({ detail }: { detail: Detail }) {
  const i = detail.incident
  return (
    <header className="rounded-lg border border-slate-800 bg-slate-900/50 p-4">
      <Link to="/" className="text-xs text-slate-500 hover:text-sky-300">
        ← all incidents
      </Link>
      <div className="mt-2 flex flex-wrap items-center gap-3">
        <SeverityChip severity={i.severity} />
        <h1 className="text-lg font-semibold text-slate-100">{i.title}</h1>
        <StatusChip status={i.status} />
      </div>
      <dl className="mt-3 grid grid-cols-2 gap-x-6 gap-y-1 text-xs text-slate-400 sm:grid-cols-4">
        <Field label="service" value={i.service} mono />
        <Field label="opened" value={formatTimestamp(i.created_at)} />
        <Field label="duration" value={formatDuration(i.created_at, i.resolved_at)} mono />
        <Field label="resolved via" value={i.resolution_source ?? '—'} />
      </dl>
    </header>
  )
}

function Field({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <dt className="uppercase tracking-wider text-slate-600">{label}</dt>
      <dd className={mono ? 'font-mono text-slate-300' : 'text-slate-300'}>{value}</dd>
    </div>
  )
}

/**
 * The brief lives on the brief_posted event as slack_payload (every SLACK_MODE
 * records it since roadmap R1). Incidents recorded before that only carry ts/via.
 */
function findBrief(detail: Detail): {
  payload: SlackBriefPayload | null
  postedWithoutPayload: boolean
} {
  const event = detail.events.find((e) => e.event_type === 'brief_posted')
  if (!event) return { payload: null, postedWithoutPayload: false }
  const payload = event.payload?.slack_payload as SlackBriefPayload | undefined
  return payload
    ? { payload, postedWithoutPayload: false }
    : { payload: null, postedWithoutPayload: true }
}
