// Mirrors the JSON shape of src/vigil/api/dashboard.py. Every uuid and timestamp
// is stringified by that module's _jsonable(); numeric columns arrive as floats.

export type Severity = 'SEV1' | 'SEV2' | 'SEV3' | 'SEV4'
export type IncidentStatus = 'open' | 'resolved' | 'postmortem_done'

/** Row shape of GET /api/incidents. */
export interface IncidentSummary {
  id: string
  service: string
  title: string
  severity: Severity | null
  status: IncidentStatus
  slack_message_ts: string | null
  created_at: string
  resolved_at: string | null
  has_postmortem: boolean
}

/** `SELECT *` from incidents, so it carries resolution_source too. */
export interface Incident extends Omit<IncidentSummary, 'has_postmortem'> {
  resolution_source: string | null
}

export interface IncidentEvent {
  event_type: string
  payload: Record<string, unknown>
  created_at: string
}

export type FeatureKey = 'f_time' | 'f_path' | 'f_risk' | 'f_size' | 'f_msg' | 'f_deploy'
export type FeatureScores = Record<FeatureKey, number>

export interface CommitFile {
  path: string
  additions?: number
  deletions?: number
  status?: string
}

/**
 * llm_rank / llm_confidence / llm_rationale are null whenever the ranking call
 * degraded. That is a normal state, not an error — the brief always posts.
 */
export interface CommitCandidate {
  sha: string
  message: string | null
  author: string | null
  committed_at: string | null
  files: CommitFile[] | null
  heuristic_score: number | null
  feature_scores: FeatureScores | null
  llm_rank: number | null
  llm_confidence: number | null
  llm_rationale: string | null
}

export interface Postmortem {
  markdown: string
  model_used: string | null
  created_at: string
}

/** Response body of GET /api/incidents/{id}. */
export interface IncidentDetail {
  incident: Incident
  events: IncidentEvent[]
  commit_candidates: CommitCandidate[]
  postmortem: Postmortem | null
}

// --- Block Kit subset emitted by src/vigil/slack/blocks.py -------------------

export interface SlackText {
  type: 'mrkdwn' | 'plain_text'
  text: string
}

export interface SlackBlock {
  type: string
  text?: SlackText
  elements?: Array<SlackText | SlackAction>
}

export interface SlackAction {
  type: 'button'
  text: SlackText
  url?: string
  action_id?: string
  style?: string
}

export interface SlackBriefPayload {
  text: string
  attachments?: Array<{ color?: string; blocks: SlackBlock[] }>
}
