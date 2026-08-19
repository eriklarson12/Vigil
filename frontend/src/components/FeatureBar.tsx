import { FEATURE_COLORS, FEATURE_LABELS, type Breakdown } from '../scoring'

/**
 * Stacked contribution bar for one commit candidate. Segments are drawn against
 * a shared 0..1 track: the empty remainder is the point of the chart.
 */
export function FeatureBar({ data }: { data: Breakdown }) {
  return (
    <div className="flex h-3 w-full overflow-hidden rounded-sm bg-slate-800">
      {data.segments
        .filter((s) => s.widthPct > 0)
        .map((s) => (
          <div
            key={s.key}
            className="h-full"
            style={{ width: `${s.widthPct}%`, backgroundColor: FEATURE_COLORS[s.key] }}
            title={`${FEATURE_LABELS[s.key]}: ${s.value.toFixed(3)} × weight = ${s.contribution.toFixed(3)}`}
          />
        ))}
    </div>
  )
}

export function FeatureLegend() {
  return (
    <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-400">
      {Object.entries(FEATURE_LABELS).map(([key, label]) => (
        <span key={key} className="inline-flex items-center gap-1.5">
          <span
            className="inline-block h-2 w-2 rounded-sm"
            style={{ backgroundColor: FEATURE_COLORS[key as keyof typeof FEATURE_COLORS] }}
          />
          {label}
        </span>
      ))}
    </div>
  )
}
