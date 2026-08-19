import type { ReactNode } from 'react'

export function Panel({
  title,
  subtitle,
  children,
}: {
  title: string
  subtitle?: ReactNode
  children: ReactNode
}) {
  return (
    <section className="rounded-lg border border-slate-800 bg-slate-900/50">
      <header className="flex items-baseline justify-between gap-4 border-b border-slate-800 px-4 py-2.5">
        <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-400">{title}</h2>
        {subtitle ? <span className="text-xs text-slate-500">{subtitle}</span> : null}
      </header>
      <div className="p-4">{children}</div>
    </section>
  )
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="text-sm italic text-slate-500">{children}</p>
}
