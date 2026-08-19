/**
 * Render smoke test against payloads captured from a live `vigil-sim demo` run
 * (src/__fixtures__/). It proves the panels survive real data — including the
 * cert_expiry no-culprit path — without needing a browser.
 *
 * Refresh the fixtures with:
 *   curl -s localhost:8000/api/incidents/<id> | python -m json.tool > src/__fixtures__/<name>.json
 */
import { renderToStaticMarkup } from 'react-dom/server'
import { StaticRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'

import badDeploy from './__fixtures__/bad_deploy.json'
import certExpiry from './__fixtures__/cert_expiry.json'
import incidents from './__fixtures__/incidents.json'
import { Brief } from './components/Brief'
import { CommitCandidates } from './components/CommitCandidates'
import { Postmortem } from './components/Postmortem'
import { Timeline } from './components/Timeline'
import { SeverityChip, StatusChip } from './components/SeverityChip'
import { formatDuration } from './format'
import { breakdown } from './scoring'
import type { IncidentDetail, IncidentSummary, SlackBriefPayload } from './types'

const scenarios: Record<string, IncidentDetail> = {
  bad_deploy: badDeploy as unknown as IncidentDetail,
  cert_expiry: certExpiry as unknown as IncidentDetail,
}

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
    if (name === 'cert_expiry') {
      expect(html).toContain('No likely culprit identified')
      expect(html).toContain('gated')
    } else {
      expect(html).toContain('likely culprit')
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
