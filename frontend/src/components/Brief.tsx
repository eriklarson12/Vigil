import { Fragment } from 'react'
import { parseMrkdwn, safeUrl } from '../format'
import type { SlackAction, SlackBlock, SlackBriefPayload, SlackText } from '../types'
import { Empty } from './Panel'

interface Props {
  payload: SlackBriefPayload | null
  /** brief_posted exists but carries no payload (incident predates R1). */
  postedWithoutPayload: boolean
}

export function Brief({ payload, postedWithoutPayload }: Props) {
  if (!payload) {
    return <Empty>{postedWithoutPayload ? 'Posted to Slack.' : 'Brief not posted yet.'}</Empty>
  }

  const attachment = payload.attachments?.[0]
  const blocks = attachment?.blocks ?? []

  return (
    <div
      className="border-l-4 pl-4"
      style={{ borderColor: attachment?.color ?? '#64748b' }}
    >
      {blocks.map((block, i) => (
        <BlockView key={i} block={block} />
      ))}
    </div>
  )
}

function BlockView({ block }: { block: SlackBlock }) {
  switch (block.type) {
    case 'header':
      return (
        <h3 className="mb-3 text-base font-semibold text-slate-100">{block.text?.text}</h3>
      )
    case 'section':
      return (
        <p className="mb-3 whitespace-pre-wrap text-sm leading-relaxed text-slate-300">
          <Mrkdwn text={block.text?.text ?? ''} />
        </p>
      )
    case 'context':
      return (
        <p className="mb-3 text-xs text-slate-500">
          {(block.elements ?? []).map((el, i) => (
            <Fragment key={i}>
              <Mrkdwn text={(el as SlackText).text ?? ''} />{' '}
            </Fragment>
          ))}
        </p>
      )
    case 'actions':
      return (
        <div className="mb-1 flex gap-2">
          {((block.elements ?? []) as SlackAction[]).map((el, i) => (
            <ActionButton key={i} action={el} />
          ))}
        </div>
      )
    default:
      return null
  }
}

/**
 * The Slack buttons are rendered as inert chips: the dashboard is read-only in
 * v1, so "Mark resolved" must not look clickable here.
 */
function ActionButton({ action }: { action: SlackAction }) {
  const label = action.text?.text ?? ''
  const classes =
    action.style === 'primary'
      ? 'border-emerald-600/50 bg-emerald-600/15 text-emerald-300'
      : 'border-slate-700 bg-slate-800/60 text-slate-400'
  return (
    <span
      className={`rounded border px-2.5 py-1 text-xs ${classes}`}
      title={safeUrl(action.url) ?? 'Slack action — not clickable from the dashboard'}
    >
      {label}
    </span>
  )
}

function Mrkdwn({ text }: { text: string }) {
  return (
    <>
      {parseMrkdwn(text).map((node, i) => {
        switch (node.type) {
          case 'bold':
            return (
              <strong key={i} className="font-semibold text-slate-100">
                {node.text}
              </strong>
            )
          case 'italic':
            return (
              <em key={i} className="text-slate-400">
                {node.text}
              </em>
            )
          case 'code':
            return (
              <code key={i} className="rounded bg-slate-800 px-1 font-mono text-xs text-sky-300">
                {node.text}
              </code>
            )
          case 'link': {
            const href = safeUrl(node.url)
            if (!href) return <Fragment key={i}>{node.text}</Fragment>
            return (
              <a
                key={i}
                href={href}
                target="_blank"
                rel="noreferrer"
                className="text-sky-400 underline decoration-sky-700 underline-offset-2"
              >
                {node.text}
              </a>
            )
          }
          default:
            return <Fragment key={i}>{node.text}</Fragment>
        }
      })}
    </>
  )
}
