import type { Severity } from './types'

// Same hex values as SEV_STYLE in src/vigil/slack/blocks.py, so a severity looks
// identical in Slack and on the dashboard.
export const SEVERITY_COLOR: Record<Severity, string> = {
  SEV1: '#E01E5A',
  SEV2: '#F2A33C',
  SEV3: '#ECB22E',
  SEV4: '#CCCCCC',
}

export const UNKNOWN_SEVERITY_COLOR = '#64748b'

export function severityColor(severity: Severity | null | undefined): string {
  return severity ? (SEVERITY_COLOR[severity] ?? UNKNOWN_SEVERITY_COLOR) : UNKNOWN_SEVERITY_COLOR
}

/**
 * The API stringifies timestamps with Python's str(datetime), which uses a space
 * separator ("2026-08-19 22:15:47.137894+00:00"). Only some engines accept that
 * in Date(), so normalize to the ISO 'T' form before parsing.
 */
export function parseStamp(value: string | null | undefined): Date | null {
  if (!value) return null
  const d = new Date(value.replace(' ', 'T'))
  return Number.isNaN(d.getTime()) ? null : d
}

export function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = parseStamp(iso)
  if (!d) return iso
  return d.toLocaleString(undefined, {
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })
}

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = parseStamp(iso)
  if (!d) return iso
  return d.toLocaleTimeString(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })
}

/**
 * Elapsed time between two ISO stamps. `to` is null for an open incident, in
 * which case the duration runs to `now` and is marked as still running.
 */
export function formatDuration(from: string, to: string | null, now: Date = new Date()): string {
  const start = parseStamp(from)
  const end = to ? parseStamp(to) : now
  if (!start || !end) return '—'
  const seconds = Math.max(0, Math.round((end.getTime() - start.getTime()) / 1000))
  const suffix = to ? '' : '…'

  if (seconds < 60) return `${seconds}s${suffix}`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ${seconds % 60}s${suffix}`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ${minutes % 60}m${suffix}`
  return `${Math.floor(hours / 24)}d ${hours % 24}h${suffix}`
}

export function shortSha(sha: string, length = 10): string {
  return sha.slice(0, length)
}

export function firstLine(text: string | null | undefined): string {
  if (!text) return ''
  return text.split('\n', 1)[0]
}

export function percent(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return `${(value * 100).toFixed(digits)}%`
}

/**
 * Only http(s) links are rendered as links. Brief text is partly LLM-written and
 * the LLM reads commit messages, so a crafted message is a path to a
 * `javascript:` href on the page.
 */
export function safeUrl(url: string | undefined): string | undefined {
  if (!url) return undefined
  try {
    const parsed = new URL(url, 'https://invalid.example')
    return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? url : undefined
  } catch {
    return undefined
  }
}

export interface MrkdwnNode {
  type: 'text' | 'bold' | 'italic' | 'code' | 'link'
  text: string
  url?: string
}

// Underscores only delimit italics at a word boundary, as in Slack itself.
// Without the lookaround, `IntentState.REQUIRES_CONFIRMATION ... confirm_payment`
// is read as one italic run and both underscores vanish from the rendered text.
const MRKDWN_RE =
  /<([^|>]+)\|([^>]*)>|`([^`]+)`|\*([^*\n]+)\*|(?<![A-Za-z0-9_])_([^\n]+?)_(?![A-Za-z0-9_])/g

/**
 * Slack mrkdwn is not markdown: links are `<url|label>`, bold is single `*`,
 * italic is single `_`. Parsed here rather than piped through a markdown
 * renderer, which would mangle all three.
 */
export function parseMrkdwn(text: string): MrkdwnNode[] {
  const nodes: MrkdwnNode[] = []
  let cursor = 0

  for (const m of text.matchAll(MRKDWN_RE)) {
    const start = m.index ?? 0
    if (start > cursor) nodes.push({ type: 'text', text: text.slice(cursor, start) })

    if (m[1] !== undefined) nodes.push({ type: 'link', text: m[2], url: m[1] })
    else if (m[3] !== undefined) nodes.push({ type: 'code', text: m[3] })
    else if (m[4] !== undefined) nodes.push({ type: 'bold', text: m[4] })
    else if (m[5] !== undefined) nodes.push({ type: 'italic', text: m[5] })

    cursor = start + m[0].length
  }

  if (cursor < text.length) nodes.push({ type: 'text', text: text.slice(cursor) })
  return nodes
}
