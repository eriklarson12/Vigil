import { formatSeconds } from '../format'
import type { WeekStats } from '../types'

const PLOT_H = 128
const MIN_BAR = 3

/**
 * Flexbox rather than SVG, and no charting dependency (roadmap R1 settled the
 * same question for polling and data fetching). An SVG with a fixed viewBox
 * either centers itself in the panel or scales its own labels when stretched;
 * flex columns fill the width and leave the type alone.
 *
 * A week with no incidents draws a baseline rule instead of a bar, so "nothing
 * happened" never reads as "we briefed instantly" — a distinction that matters
 * here because a fixture-mode run really does brief in about 20ms.
 */
export function WeeklyBars({ weeks }: { weeks: WeekStats[] }) {
  const peak = Math.max(...weeks.map((w) => w.mtta_p50 ?? 0), 1)

  return (
    <div>
      <div className="flex items-end gap-2" style={{ height: PLOT_H + 20 }}>
        {weeks.map((w) => {
          const empty = w.incidents === 0 || w.mtta_p50 === null
          const height = empty ? 0 : Math.max(MIN_BAR, ((w.mtta_p50 as number) / peak) * PLOT_H)
          return (
            <div key={w.week} className="flex flex-1 flex-col items-center justify-end">
              <span className="mb-1 font-mono text-[10px] text-slate-400">
                {empty ? '' : formatSeconds(w.mtta_p50)}
              </span>
              {empty ? (
                <div className="h-px w-full bg-slate-700" />
              ) : (
                <div className="w-full rounded-t bg-sky-500/70" style={{ height }} />
              )}
            </div>
          )
        })}
      </div>
      <div className="mt-2 flex gap-2 border-t border-slate-800 pt-2">
        {weeks.map((w) => (
          <div key={w.week} className="flex flex-1 flex-col items-center">
            <span className="text-[11px] text-slate-500">{weekLabel(w.week)}</span>
            <span className="font-mono text-[11px] text-slate-600">{w.incidents}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

/** "2026-08-24" -> "Aug 24". Parsed as a local date; the API sends a bare day. */
function weekLabel(week: string): string {
  const [y, m, d] = week.split('-').map(Number)
  if (!y || !m || !d) return week
  return new Date(y, m - 1, d).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}
