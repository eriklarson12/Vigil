import { describe, expect, it } from 'vitest'
import { breakdown, topFeature, RELEVANCE_GATE, WEIGHTS } from './scoring'
import type { FeatureScores } from './types'

// The two vectors below come from the Python golden tests in
// tests/unit/test_scoring.py::TestGoldenValues. If those change, this fails too —
// which is the point: the bar must always sum to the stored heuristic_score.
const FULL_PATH_MATCH: FeatureScores = {
  f_time: Math.exp(-1),
  f_path: 1,
  f_risk: 0,
  f_size: Math.log10(100) / 3,
  f_msg: 0.3,
  f_deploy: 0,
}

const GATED: FeatureScores = {
  f_time: Math.exp(-1 / 6),
  f_path: 0,
  f_risk: 0,
  f_size: Math.log10(11) / 3,
  f_msg: 0,
  f_deploy: 0,
}

describe('breakdown', () => {
  it('reproduces the Python golden score for a full path match', () => {
    const b = breakdown(FULL_PATH_MATCH)!
    expect(b.gated).toBe(false)
    expect(b.rawScore).toBeCloseTo(0.45703, 5)
    expect(b.gatedScore).toBeCloseTo(0.45703, 5)
  })

  it('applies the relevance gate when f_path and f_deploy are both zero', () => {
    const b = breakdown(GATED)!
    expect(b.gated).toBe(true)
    expect(b.rawScore).toBeCloseTo(0.288658, 5)
    expect(b.gatedScore).toBeCloseTo(0.086597, 5)
    expect(b.gatedScore).toBeCloseTo(b.rawScore * RELEVANCE_GATE, 10)
  })

  it('sizes each segment by weight × value on a shared 0..1 track', () => {
    const b = breakdown(FULL_PATH_MATCH)!
    const path = b.segments.find((s) => s.key === 'f_path')!
    expect(path.contribution).toBeCloseTo(WEIGHTS.f_path, 10)
    expect(path.widthPct).toBeCloseTo(25, 10)

    const total = b.segments.reduce((sum, s) => sum + s.widthPct, 0)
    expect(total + b.remainderPct).toBeCloseTo(100, 10)
  })

  it('leaves the track fully empty for an all-zero candidate', () => {
    const zeros = Object.fromEntries(Object.keys(WEIGHTS).map((k) => [k, 0])) as FeatureScores
    const b = breakdown(zeros)!
    expect(b.rawScore).toBe(0)
    expect(b.remainderPct).toBe(100)
    expect(b.segments.every((s) => s.widthPct === 0)).toBe(true)
  })

  it('returns null when feature_scores was never persisted', () => {
    expect(breakdown(null)).toBeNull()
    expect(breakdown(undefined)).toBeNull()
  })

  it('treats a missing or non-finite feature as zero rather than NaN', () => {
    const partial = { ...FULL_PATH_MATCH, f_risk: Number.NaN } as FeatureScores
    const b = breakdown(partial)!
    expect(Number.isFinite(b.rawScore)).toBe(true)
    expect(b.segments.find((s) => s.key === 'f_risk')!.contribution).toBe(0)
  })

  it('weights sum to 1.0, matching src/vigil/commits/scoring.py', () => {
    const total = Object.values(WEIGHTS).reduce((a, b) => a + b, 0)
    expect(total).toBeCloseTo(1.0, 10)
  })
})

describe('topFeature', () => {
  it('names the strongest contributor', () => {
    expect(topFeature(FULL_PATH_MATCH)).toBe('f_path')
  })

  it('is null when nothing contributed', () => {
    const zeros = Object.fromEntries(Object.keys(WEIGHTS).map((k) => [k, 0])) as FeatureScores
    expect(topFeature(zeros)).toBeNull()
    expect(topFeature(null)).toBeNull()
  })
})
