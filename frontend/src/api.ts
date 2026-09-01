import { useCallback, useEffect, useRef, useState } from 'react'
import type { IncidentDetail, IncidentSummary, Stats } from './types'

export const API_BASE = import.meta.env.VITE_API_URL ?? 'http://localhost:8000'

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const resp = await fetch(`${API_BASE}${path}`, { signal })
  if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText} — GET ${path}`)
  return (await resp.json()) as T
}

export const fetchIncidents = (signal?: AbortSignal) =>
  getJson<IncidentSummary[]>('/api/incidents', signal)

export const fetchIncident = (id: string, signal?: AbortSignal) =>
  getJson<IncidentDetail>(`/api/incidents/${id}`, signal)

export const fetchStats = (signal?: AbortSignal) => getJson<Stats>('/api/stats', signal)

export interface PollResult<T> {
  data: T | null
  error: Error | null
  loading: boolean
}

export interface PollOptions<T> {
  /** Stop polling once this returns true — e.g. the incident is fully done. */
  stopWhen?: (data: T) => boolean
  /** Extra dependencies that should restart the poll (the fetcher's closure). */
  deps?: unknown[]
}

/**
 * Poll `fetcher` every `intervalMs`. Keeps the last good value on a failed poll
 * so the page does not flash empty while the container scales up from zero
 * (ADR-006), and stops the interval once `stopWhen` is satisfied.
 */
export function usePolling<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  intervalMs: number,
  { stopWhen, deps = [] }: PollOptions<T> = {},
): PollResult<T> {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<Error | null>(null)
  const [loading, setLoading] = useState(true)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const run = useCallback(fetcher, deps)
  const alive = useRef(true)
  // Read through a ref so a fresh predicate closure never restarts the interval.
  const stopRef = useRef(stopWhen)
  stopRef.current = stopWhen

  useEffect(() => {
    alive.current = true
    const controller = new AbortController()
    let timer: ReturnType<typeof setInterval> | undefined

    const tick = async () => {
      try {
        const next = await run(controller.signal)
        if (!alive.current) return
        setData(next)
        setError(null)
        if (timer && stopRef.current?.(next)) {
          clearInterval(timer)
          timer = undefined
        }
      } catch (err) {
        if (controller.signal.aborted || !alive.current) return
        setError(err instanceof Error ? err : new Error(String(err)))
      } finally {
        if (alive.current) setLoading(false)
      }
    }

    setLoading(true)
    void tick()
    if (intervalMs > 0) timer = setInterval(() => void tick(), intervalMs)

    return () => {
      alive.current = false
      controller.abort()
      if (timer) clearInterval(timer)
    }
  }, [run, intervalMs])

  return { data, error, loading }
}
