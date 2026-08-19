// Mirror of src/vigil/commits/scoring.py — that file is the source of truth.
// If WEIGHTS or RELEVANCE_GATE change there, change them here and update
// src/scoring.test.ts in the same commit.

import type { FeatureKey, FeatureScores } from './types'

export const WEIGHTS: Record<FeatureKey, number> = {
  f_time: 0.3,
  f_path: 0.25,
  f_risk: 0.15,
  f_size: 0.1,
  f_msg: 0.1,
  f_deploy: 0.1,
}

/** Multiplier applied when f_path and f_deploy are both zero. */
export const RELEVANCE_GATE = 0.3

/** Render order, widest weight first, so bars read left-to-right by importance. */
export const FEATURE_ORDER: FeatureKey[] = [
  'f_time',
  'f_path',
  'f_risk',
  'f_size',
  'f_msg',
  'f_deploy',
]

export const FEATURE_LABELS: Record<FeatureKey, string> = {
  f_time: 'recency',
  f_path: 'path match',
  f_risk: 'risky files',
  f_size: 'diff size',
  f_msg: 'message',
  f_deploy: 'deploy window',
}

export const FEATURE_COLORS: Record<FeatureKey, string> = {
  f_time: '#38bdf8',
  f_path: '#a78bfa',
  f_risk: '#fb7185',
  f_size: '#facc15',
  f_msg: '#34d399',
  f_deploy: '#f97316',
}

export interface Segment {
  key: FeatureKey
  /** Raw feature value in [0, 1]. */
  value: number
  /** WEIGHTS[key] * value — the contribution to the un-gated score. */
  contribution: number
  /** Percentage of the full-scale (1.0) track this segment occupies. */
  widthPct: number
}

export interface Breakdown {
  segments: Segment[]
  /** Sum of contributions before the relevance gate. */
  rawScore: number
  /** rawScore, times RELEVANCE_GATE when gated — matches heuristic_score. */
  gatedScore: number
  gated: boolean
  /** Percentage of the track left empty; bars share a 0..1 scale. */
  remainderPct: number
}

/**
 * Turn a persisted feature_scores blob into stacked-bar segments.
 *
 * All bars are drawn against the same 0..1 scale so candidates are visually
 * comparable — without that, every bar fills its track and the panel says nothing.
 */
export function breakdown(scores: FeatureScores | null | undefined): Breakdown | null {
  if (!scores) return null

  const segments: Segment[] = FEATURE_ORDER.map((key) => {
    const value = Number.isFinite(scores[key]) ? scores[key] : 0
    const contribution = WEIGHTS[key] * value
    return { key, value, contribution, widthPct: contribution * 100 }
  })

  const rawScore = segments.reduce((sum, s) => sum + s.contribution, 0)
  const gated = (scores.f_path ?? 0) === 0 && (scores.f_deploy ?? 0) === 0
  const gatedScore = gated ? rawScore * RELEVANCE_GATE : rawScore

  return {
    segments,
    rawScore,
    gatedScore,
    gated,
    remainderPct: Math.max(0, 100 - rawScore * 100),
  }
}

/** The strongest single contributor — used as the one-line "why" on a row. */
export function topFeature(scores: FeatureScores | null | undefined): FeatureKey | null {
  const b = breakdown(scores)
  if (!b) return null
  const best = b.segments.reduce((a, s) => (s.contribution > a.contribution ? s : a))
  return best.contribution > 0 ? best.key : null
}
