import { useState } from 'react'
import { firstLine, formatTimestamp, percent, shortSha } from '../format'
import { FEATURE_LABELS, FEATURE_ORDER, breakdown, WEIGHTS } from '../scoring'
import type { CommitCandidate } from '../types'
import { Empty } from './Panel'
import { FeatureBar, FeatureLegend } from './FeatureBar'

interface Props {
  candidates: CommitCandidate[]
  /** From the triage_finalized event; null on the honest no-culprit path. */
  culpritSha: string | null
  /** True once triage finished, so "no culprit" can be told apart from "not yet". */
  triageFinalized: boolean
}

export function CommitCandidates({ candidates, culpritSha, triageFinalized }: Props) {
  const [open, setOpen] = useState<string | null>(null)

  if (candidates.length === 0) {
    return (
      <Empty>
        {triageFinalized
          ? 'No commits in the lookback window scored above the floor.'
          : 'Scoring in progress…'}
      </Empty>
    )
  }

  return (
    <div className="space-y-3">
      {triageFinalized && !culpritSha ? (
        <p className="rounded border border-slate-700 bg-slate-800/50 px-3 py-2 text-sm text-slate-300">
          <strong className="text-slate-100">No likely culprit identified.</strong> The candidates
          below were scored but none cleared the confidence floor — Vigil says so rather than
          guessing.
        </p>
      ) : null}

      <FeatureLegend />

      <ul className="divide-y divide-slate-800">
        {candidates.map((c) => {
          const data = breakdown(c.feature_scores)
          const isCulprit = culpritSha ? c.sha.startsWith(culpritSha) || culpritSha.startsWith(c.sha) : false
          const expanded = open === c.sha
          return (
            <li key={c.sha} className="py-3">
              <button
                type="button"
                onClick={() => setOpen(expanded ? null : c.sha)}
                className="w-full cursor-pointer text-left"
              >
                <div className="flex items-center gap-3">
                  <code className="font-mono text-sm text-sky-300">{shortSha(c.sha)}</code>
                  {isCulprit ? (
                    <span className="rounded bg-rose-500/20 px-1.5 py-0.5 text-xs font-semibold text-rose-300">
                      likely culprit
                    </span>
                  ) : null}
                  {data?.gated ? (
                    <span
                      className="rounded bg-slate-700/60 px-1.5 py-0.5 text-xs text-slate-400"
                      title="No path match and no deploy correlation — score multiplied by 0.3"
                    >
                      gated ×0.3
                    </span>
                  ) : null}
                  <span className="ml-auto font-mono text-sm text-slate-300">
                    {c.heuristic_score?.toFixed(3) ?? '—'}
                  </span>
                </div>

                <p className="mt-1 truncate text-sm text-slate-300">{firstLine(c.message)}</p>

                <div className="mt-2">
                  {data ? <FeatureBar data={data} /> : <Empty>no feature scores stored</Empty>}
                </div>

                <p className="mt-1 text-xs text-slate-500">
                  {c.author ?? 'unknown author'} · {formatTimestamp(c.committed_at)}
                  {c.llm_confidence !== null
                    ? ` · LLM rank ${c.llm_rank ?? '—'}, confidence ${percent(c.llm_confidence)}`
                    : ' · not ranked by the LLM'}
                </p>
              </button>

              {expanded ? <Details candidate={c} /> : null}
            </li>
          )
        })}
      </ul>
    </div>
  )
}

function Details({ candidate }: { candidate: CommitCandidate }) {
  const data = breakdown(candidate.feature_scores)
  return (
    <div className="mt-3 space-y-3 rounded border border-slate-800 bg-slate-950/60 p-3">
      {data ? (
        <table className="w-full text-xs">
          <thead className="text-slate-500">
            <tr>
              <th className="text-left font-normal">feature</th>
              <th className="text-right font-normal">value</th>
              <th className="text-right font-normal">weight</th>
              <th className="text-right font-normal">contribution</th>
            </tr>
          </thead>
          <tbody className="font-mono text-slate-300">
            {FEATURE_ORDER.map((key) => {
              const seg = data.segments.find((s) => s.key === key)!
              return (
                <tr key={key}>
                  <td className="py-0.5 font-sans">{FEATURE_LABELS[key]}</td>
                  <td className="text-right">{seg.value.toFixed(4)}</td>
                  <td className="text-right text-slate-500">{WEIGHTS[key].toFixed(2)}</td>
                  <td className="text-right">{seg.contribution.toFixed(4)}</td>
                </tr>
              )
            })}
            <tr className="border-t border-slate-800">
              <td className="py-0.5 font-sans text-slate-400">
                {data.gated ? 'sum × 0.3 (gated)' : 'sum'}
              </td>
              <td />
              <td />
              <td className="text-right text-slate-100">{data.gatedScore.toFixed(4)}</td>
            </tr>
          </tbody>
        </table>
      ) : null}

      {candidate.llm_rationale ? (
        <p className="text-sm italic text-slate-300">“{candidate.llm_rationale}”</p>
      ) : null}

      {candidate.files?.length ? (
        <ul className="space-y-0.5 font-mono text-xs text-slate-400">
          {candidate.files.map((f) => (
            <li key={f.path}>
              <span className="text-emerald-400">+{f.additions ?? 0}</span>{' '}
              <span className="text-rose-400">-{f.deletions ?? 0}</span> {f.path}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  )
}
