import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { formatTimestamp } from '../format'
import type { Postmortem as PostmortemData } from '../types'
import { Empty } from './Panel'

export function Postmortem({ data }: { data: PostmortemData | null }) {
  if (!data) return <Empty>Postmortem pending — it is written when the incident resolves.</Empty>

  return (
    <article>
      <p className="mb-3 text-xs text-slate-500">
        {data.model_used ?? 'deterministic'} · {formatTimestamp(data.created_at)}
      </p>
      <div className="postmortem text-sm leading-relaxed text-slate-300">
        {/* remark-gfm: the generated postmortem uses a GFM table for its timeline. */}
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{data.markdown}</ReactMarkdown>
      </div>
    </article>
  )
}
