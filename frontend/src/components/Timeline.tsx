import { formatTime } from '../format'
import type { IncidentEvent } from '../types'
import { Empty } from './Panel'

const EVENT_STYLE: Record<string, string> = {
  triage_finalized: 'bg-violet-500/15 text-violet-300 border-violet-500/40',
  brief_posted: 'bg-sky-500/15 text-sky-300 border-sky-500/40',
  resolved: 'bg-amber-500/15 text-amber-300 border-amber-500/40',
  postmortem_posted: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/40',
}

export function Timeline({ events }: { events: IncidentEvent[] }) {
  if (events.length === 0) return <Empty>No events yet.</Empty>

  return (
    <ol className="space-y-2 font-mono text-xs">
      {events.map((e, i) => (
        <li key={`${e.created_at}-${i}`} className="flex gap-3">
          <span className="shrink-0 text-slate-500">{formatTime(e.created_at)}</span>
          <span
            className={`h-fit shrink-0 rounded border px-1.5 ${
              EVENT_STYLE[e.event_type] ?? 'bg-slate-700/40 text-slate-300 border-slate-600'
            }`}
          >
            {e.event_type}
          </span>
          <span className="min-w-0 flex-1 break-words text-slate-400">{summarize(e)}</span>
        </li>
      ))}
    </ol>
  )
}

/** Unknown event types must still render — the roadmap adds more of them. */
function summarize(event: IncidentEvent): string {
  const p = event.payload ?? {}
  if (p.truncated) return 'payload truncated at the 8 KB cap'

  if (event.event_type === 'triage_finalized') {
    const errors = (p.errors ?? {}) as Record<string, string>
    const parts = [
      `${p.scored ?? 0} commits scored`,
      `${p.chunks ?? 0} runbook chunks`,
      `severity ${p.severity ?? '—'}`,
      `${p.llm_calls ?? 0} LLM calls`,
      p.culprit ? `culprit ${String(p.culprit).slice(0, 10)}` : 'no culprit',
    ]
    const degraded = Object.keys(errors)
    if (degraded.length) parts.push(`degraded: ${degraded.join(', ')}`)
    return parts.join(' · ')
  }

  const keys = Object.keys(p).filter((k) => k !== 'slack_payload')
  if (keys.length === 0) return event.event_type === 'brief_posted' ? 'brief posted' : ''
  return keys.map((k) => `${k}=${JSON.stringify(p[k])}`).join(' · ')
}
