import { describe, expect, it } from 'vitest'
import {
  SEVERITY_COLOR,
  formatDuration,
  parseMrkdwn,
  parseStamp,
  percent,
  safeUrl,
  severityColor,
  shortSha,
} from './format'

describe('formatDuration', () => {
  const open = '2026-07-01T12:00:00+00:00'

  it('formats a closed incident', () => {
    expect(formatDuration(open, '2026-07-01T12:00:42+00:00')).toBe('42s')
    expect(formatDuration(open, '2026-07-01T12:07:30+00:00')).toBe('7m 30s')
    expect(formatDuration(open, '2026-07-01T14:20:00+00:00')).toBe('2h 20m')
    expect(formatDuration(open, '2026-07-03T15:00:00+00:00')).toBe('2d 3h')
  })

  it('runs an open incident to now and marks it as still running', () => {
    const now = new Date('2026-07-01T12:05:00+00:00')
    expect(formatDuration(open, null, now)).toBe('5m 0s…')
  })

  it('never goes negative on clock skew', () => {
    expect(formatDuration(open, '2026-07-01T11:59:00+00:00')).toBe('0s')
  })

  it('degrades to a dash on an unparseable stamp', () => {
    expect(formatDuration('not-a-date', null)).toBe('—')
  })

  it('accepts the space-separated form the API actually emits', () => {
    // Python's str(datetime) — see _jsonable in src/vigil/api/dashboard.py.
    expect(formatDuration('2026-08-19 22:15:47.137894+00:00', '2026-08-19 22:16:29.137894+00:00'))
      .toBe('42s')
  })
})

describe('parseStamp', () => {
  it('parses both the space form and the ISO form to the same instant', () => {
    const space = parseStamp('2026-08-19 22:15:47.137894+00:00')!
    const iso = parseStamp('2026-08-19T22:15:47.137894+00:00')!
    expect(space.toISOString()).toBe(iso.toISOString())
    expect(space.toISOString()).toBe('2026-08-19T22:15:47.137Z')
  })

  it('is null for missing or unparseable input', () => {
    expect(parseStamp(null)).toBeNull()
    expect(parseStamp('')).toBeNull()
    expect(parseStamp('nope')).toBeNull()
  })
})

describe('severityColor', () => {
  it('matches SEV_STYLE in src/vigil/slack/blocks.py', () => {
    expect(SEVERITY_COLOR).toEqual({
      SEV1: '#E01E5A',
      SEV2: '#F2A33C',
      SEV3: '#ECB22E',
      SEV4: '#CCCCCC',
    })
  })

  it('falls back for an incident whose severity was never set', () => {
    expect(severityColor(null)).toBe('#64748b')
  })
})

describe('parseMrkdwn', () => {
  it('parses a Slack link into label and url', () => {
    expect(parseMrkdwn('see <https://github.com/x/y/commit/abc|`abc1234567`>')).toEqual([
      { type: 'text', text: 'see ' },
      { type: 'link', text: '`abc1234567`', url: 'https://github.com/x/y/commit/abc' },
    ])
  })

  it('parses single-asterisk bold, single-underscore italic, and code', () => {
    expect(parseMrkdwn('*Impact*\n_why_ `f_path`')).toEqual([
      { type: 'bold', text: 'Impact' },
      { type: 'text', text: '\n' },
      { type: 'italic', text: 'why' },
      { type: 'text', text: ' ' },
      { type: 'code', text: 'f_path' },
    ])
  })

  it('leaves the confidence bar glyphs untouched', () => {
    const text = '*Likely culprit:* ▓▓▓▓░ 78%'
    expect(parseMrkdwn(text).map((n) => n.text).join('')).toBe('Likely culprit: ▓▓▓▓░ 78%')
  })

  it('leaves underscores inside identifiers alone', () => {
    // Regression: `_([^_\n]+)_` swallowed REQUIRES_CONFIRMATION..confirm_payment
    // as one italic run, deleting both underscores from the rendered brief.
    const text =
      '_The diff removes IntentState.REQUIRES_CONFIRMATION in confirm_payment (payment.py)._'
    expect(parseMrkdwn(text)).toEqual([
      {
        type: 'italic',
        text: 'The diff removes IntentState.REQUIRES_CONFIRMATION in confirm_payment (payment.py).',
      },
    ])
  })

  it('does not italicize a bare snake_case identifier', () => {
    expect(parseMrkdwn('snake_case_name stays intact')).toEqual([
      { type: 'text', text: 'snake_case_name stays intact' },
    ])
  })

  it('still handles two separate italic runs on one line', () => {
    expect(parseMrkdwn('_first_ then _second_')).toEqual([
      { type: 'italic', text: 'first' },
      { type: 'text', text: ' then ' },
      { type: 'italic', text: 'second' },
    ])
  })

  it('returns plain text unchanged', () => {
    expect(parseMrkdwn('no markup here')).toEqual([{ type: 'text', text: 'no markup here' }])
  })
})

describe('misc formatters', () => {
  it('shortens a sha to the brief-length prefix', () => {
    expect(shortSha('a1b2c3d4e5f6a7b8')).toBe('a1b2c3d4e5')
  })

  it('renders a null confidence as a dash, not 0%', () => {
    expect(percent(null)).toBe('—')
    expect(percent(0.78)).toBe('78%')
  })
})

describe('safeUrl', () => {
  it('passes http and https through unchanged', () => {
    expect(safeUrl('https://github.com/o/r/commit/abc')).toBe('https://github.com/o/r/commit/abc')
    expect(safeUrl('http://localhost:8000/x')).toBe('http://localhost:8000/x')
  })

  it('rejects script-bearing schemes', () => {
    // The brief carries LLM-written text and the LLM reads commit messages.
    expect(safeUrl('javascript:alert(1)')).toBeUndefined()
    expect(safeUrl('JaVaScRiPt:alert(1)')).toBeUndefined()
    expect(safeUrl('data:text/html,<script>alert(1)</script>')).toBeUndefined()
    expect(safeUrl('vbscript:msgbox')).toBeUndefined()
  })

  it('is undefined for missing or unparseable input', () => {
    expect(safeUrl(undefined)).toBeUndefined()
    expect(safeUrl('')).toBeUndefined()
  })
})
