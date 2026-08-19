import { severityColor } from '../format'
import type { Severity } from '../types'

export function SeverityChip({ severity }: { severity: Severity | null }) {
  const color = severityColor(severity)
  return (
    <span
      className="inline-flex items-center rounded px-2 py-0.5 text-xs font-semibold tracking-wide"
      style={{ backgroundColor: `${color}22`, color, border: `1px solid ${color}66` }}
    >
      {severity ?? 'UNSET'}
    </span>
  )
}

const STATUS_STYLE: Record<string, string> = {
  open: 'bg-amber-500/15 text-amber-300 border-amber-500/40',
  resolved: 'bg-sky-500/15 text-sky-300 border-sky-500/40',
  postmortem_done: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/40',
}

export function StatusChip({ status }: { status: string }) {
  const style = STATUS_STYLE[status] ?? 'bg-slate-700/40 text-slate-300 border-slate-600'
  return (
    <span className={`inline-flex rounded border px-2 py-0.5 text-xs ${style}`}>
      {status.replace(/_/g, ' ')}
    </span>
  )
}
