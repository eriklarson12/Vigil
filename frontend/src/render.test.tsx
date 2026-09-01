/**
 * Render smoke test against payloads captured from a live `vigil-sim demo` run
 * (src/__fixtures__/). It proves the panels survive real data — including both
 * no-culprit shapes: cert_expiry (nothing above the score floor) and
 * ambiguous_latency (ranked candidates, all under the confidence floor) — without
 * needing a browser.
 *
 * Refresh the fixtures with:
 *   curl -s localhost:8000/api/incidents/<id> | python -m json.tool > src/__fixtures__/<name>.json
 */
import { renderToStaticMarkup } from 'react-dom/server'
import { StaticRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'

import ambiguousLatency from './__fixtures__/ambiguous_latency.json'
import badDeploy from './__fixtures__/bad_deploy.json'
import certExpiry from './__fixtures__/cert_expiry.json'
import incidents from './__fixtures__/incidents.json'
import stats from './__fixtures__/stats.json'
import { Brief } from './components/Brief'
import { CommitCandidates } from './components/CommitCandidates'
import { Postmortem } from './components/Postmortem'
import { Timeline } from './components/Timeline'
import { SeverityChip, StatusChip } from './components/SeverityChip'
import { WeeklyBars } from './components/WeeklyBars'
import { StatsView } from './pages/Stats'
import { formatDuration } from './format'
import { breakdown } from './scoring'
import type { IncidentDetail, IncidentSummary, SlackBriefPayload, Stats } from './types'

const scenarios: Record<string, IncidentDetail> = {
  bad_deploy: badDeploy as unknown as IncidentDetail,
  cert_expiry: certExpiry as unknown as IncidentDetail,
  ambiguous_latency: ambiguousLatency as unknown as IncidentDetail,
}

// Scenarios where triage deliberately named no culprit.
const NO_CULPRIT = new Set(['cert_expiry', 'ambiguous_latency'])

function culpritOf(d: IncidentDetail): string | null {
  const tf = d.events.find((e) => e.event_type === 'triage_finalized')
  return (tf?.payload?.culprit as string | null | undefined) ?? null
}

function briefOf(d: IncidentDetail): SlackBriefPayload | null {
  const e = d.events.find((ev) => ev.event_type === 'brief_posted')
  return (e?.payload?.slack_payload as SlackBriefPayload | undefined) ?? null
}

const render = (node: React.ReactNode) =>
  renderToStaticMarkup(<StaticRouter location="/">{node}</StaticRouter>)

describe.each(Object.entries(scenarios))('%s', (name, detail) => {
  it('renders the commit-candidates panel', () => {
    const html = render(
      <CommitCandidates
        candidates={detail.commit_candidates}
        culpritSha={culpritOf(detail)}
        triageFinalized
      />,
    )
    expect(html).toContain(detail.commit_candidates[0].sha.slice(0, 10))
    if (NO_CULPRIT.has(name)) {
      expect(html).toContain('No likely culprit identified')
    } else {
      expect(html).toContain('likely culprit')
    }
    if (name === 'cert_expiry') expect(html).toContain('gated')
    if (name === 'ambiguous_latency') {
      // The candidates still carry their LLM ranks; only the culprit is withheld.
      expect(html).toContain('LLM rank 1, confidence 35%')
    }
  })

  it('renders the Slack brief from the stored Block Kit payload', () => {
    const html = render(<Brief payload={briefOf(detail)} postedWithoutPayload={false} />)
    expect(html).toContain(detail.incident.service)
    expect(html).toContain('Impact')
    // Slack mrkdwn must be converted, never printed raw.
    expect(html).not.toContain('*Impact*')
  })

  it('renders the timeline and postmortem', () => {
    expect(render(<Timeline events={detail.events} />)).toContain('triage_finalized')
    expect(render(<Postmortem data={detail.postmortem} />)).toContain('Postmortem')
  })

  it('renders postmortem markdown as markup, not raw source', () => {
    const html = render(<Postmortem data={detail.postmortem} />)
    expect(html).toContain('<h1')
    expect(html).toContain('<h2')
    // The generated timeline is a GFM table; without remark-gfm it stayed as
    // literal pipes on the page.
    expect(html).toContain('<table')
    expect(html).not.toContain('|---|')
  })

  it('keeps snake_case identifiers intact in the brief rationale', () => {
    const html = render(<Brief payload={briefOf(detail)} postedWithoutPayload={false} />)
    if (name === 'bad_deploy') expect(html).toContain('REQUIRES_CONFIRMATION')
  })

  it('reproduces every stored heuristic_score from feature_scores alone', () => {
    for (const c of detail.commit_candidates) {
      const b = breakdown(c.feature_scores)!
      expect(b.gatedScore).toBeCloseTo(c.heuristic_score!, 3)
    }
  })
})

describe('incident list data', () => {
  it('renders chips and durations for every row', () => {
    for (const i of incidents as unknown as IncidentSummary[]) {
      expect(render(<SeverityChip severity={i.severity} />)).toContain(i.severity ?? 'UNSET')
      expect(render(<StatusChip status={i.status} />)).toContain(i.status.replace(/_/g, ' '))
      expect(formatDuration(i.created_at, i.resolved_at)).not.toBe('—')
    }
  })
})

describe('brief link hardening', () => {
  const payloadWith = (text: string): SlackBriefPayload => ({
    text: 'x',
    attachments: [{ color: '#E01E5A', blocks: [{ type: 'section', text: { type: 'mrkdwn', text } }] }],
  })

  it('renders a javascript: link as inert text, keeping the label', () => {
    const html = render(
      <Brief payload={payloadWith('see <javascript:alert(1)|the commit>')} postedWithoutPayload={false} />,
    )
    expect(html).toContain('the commit')
    expect(html).not.toContain('javascript:')
    expect(html).not.toContain('<a ')
  })

  it('still links an ordinary commit url', () => {
    const html = render(
      <Brief
        payload={payloadWith('see <https://github.com/o/r/commit/abc|`abc`>')}
        postedWithoutPayload={false}
      />,
    )
    expect(html).toContain('href="https://github.com/o/r/commit/abc"')
  })
})

describe('stats page', () => {
  const live = stats as unknown as Stats

  it('renders the captured response from a two-scenario demo run', () => {
    const html = render(<StatsView stats={live} />)
    // cert_expiry deliberately names no culprit, so the rate is never 100%
    expect(html).toContain(`${live.triage.culprit_named} of ${live.triage.triaged}`)
    expect(html).toContain(`${live.llm.today_used} / ${live.llm.daily_budget}`)
    expect(html).toContain('constraint holds')
  })

  it('renders an empty database without inventing zeros', () => {
    const empty: Stats = {
      overall: { open: 0, resolved: 0, mtta_n: 0, mttr_n: 0, mtta_p50: null, mtta_p90: null,
                 mttr_p50: null, mttr_p90: null },
      by_severity: [],
      by_week: live.by_week.map((w) => ({ ...w, incidents: 0, mtta_p50: null, mttr_p50: null })),
      triage: { triaged: 0, culprit_named: 0, scored_p50: null, chunks_p50: null,
                degraded: 0, degraded_nodes: [] },
      llm: { ...live.llm, triage_mean: null, total_mean: null, total_max: null },
    }
    const html = render(<StatsView stats={empty} />)
    expect(html).toContain('—')
    expect(html).toContain('No incident has finished triage yet.')
    expect(html).not.toContain('0s')
  })

  it('labels an unset severity instead of dropping the row', () => {
    // incidents.severity is nullable, so the group can come back as null
    const withNull: Stats = {
      ...live,
      by_severity: [{ severity: null, open: 1, resolved: 0, mtta_p50: 12, mtta_p90: 12,
                      mttr_p50: null, mttr_p90: null }],
    }
    const html = render(<StatsView stats={withNull} />)
    expect(html).toContain('UNSET')
    expect(html).toContain('12s')
  })

  it('warns that p50 and p90 are the same number on a single sample', () => {
    const one: Stats = { ...live, overall: { ...live.overall, mtta_n: 1 } }
    expect(render(<StatsView stats={one} />)).toContain('One sample')
    expect(render(<StatsView stats={live} />)).not.toContain('One sample')
  })

  it('surfaces a degraded node and a ceiling breach', () => {
    const bad: Stats = {
      ...live,
      triage: { ...live.triage, degraded: 2,
                degraded_nodes: [{ node: 'fetch_commits', count: 2 }] },
      llm: { ...live.llm, over_ceiling: 1 },
    }
    const html = render(<StatsView stats={bad} />)
    expect(html).toContain('fetch_commits')
    expect(html).toContain('constraint violated')
  })
})

describe('weekly bars', () => {
  const weeks = [
    { week: '2026-08-10', incidents: 0, mtta_p50: null, mttr_p50: null },
    { week: '2026-08-17', incidents: 3, mtta_p50: 40, mttr_p50: 900 },
    { week: '2026-08-24', incidents: 1, mtta_p50: 80, mttr_p50: 900 },
  ]
  // scope to the bar divs: the plot container carries a height of its own
  const barHeights = (html: string) =>
    [...html.matchAll(/bg-sky-500\/70"\s*style="height:\s*([\d.]+)px"/g)].map((m) => Number(m[1]))

  it('scales bars against the tallest week', () => {
    const bars = barHeights(render(<WeeklyBars weeks={weeks} />))
    expect(bars).toHaveLength(2)
    expect(Math.max(...bars) / Math.min(...bars)).toBeCloseTo(2, 5)
  })

  it('draws an empty week as a baseline rule, not a zero-height bar', () => {
    const html = render(<WeeklyBars weeks={weeks} />)
    expect(barHeights(html)).not.toContain(0)
    expect(html).toContain('h-px')
    expect(html.match(/bg-sky-500/g)?.length).toBe(2)
  })

  it('keeps a sub-second week visible instead of collapsing it onto the axis', () => {
    // a fixture-mode run briefs in ~20ms; against a 40s peak that rounds to nothing
    const html = render(
      <WeeklyBars weeks={[weeks[1], { week: '2026-08-31', incidents: 2, mtta_p50: 0.02, mttr_p50: 2 }]} />,
    )
    expect(Math.min(...barHeights(html))).toBeGreaterThanOrEqual(3)
  })

  it('fills the panel width rather than centering a fixed-size drawing', () => {
    // the SVG version had a fixed viewBox, so it sat in the middle of a wide panel
    const html = render(<WeeklyBars weeks={weeks} />)
    expect(html).not.toContain('viewBox')
    expect(html.match(/flex-1/g)?.length).toBe(weeks.length * 2)
  })
})
