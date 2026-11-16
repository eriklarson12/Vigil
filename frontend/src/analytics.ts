/** Product events. Vercel's free plan records pageviews only, so named events
 * go to Umami. Unset `VITE_UMAMI_WEBSITE_ID` and no third-party script loads. */

const UMAMI_SCRIPT = 'https://cloud.umami.is/script.js'

/** The whole vocabulary. Incident data here is synthetic, but an event still
 * MUST NOT carry free text (a brief, a commit message). */
export type AnalyticsEvents = {
  candidate_expand: { rank: number; culprit: boolean }
  brief_link: { host: string }
}

declare global {
  interface Window {
    umami?: { track: (event: string, data?: Record<string, unknown>) => void }
  }
}

/** Production builds only: a dev server or test run sends nothing. */
export function loadUmami(
  websiteId: string | undefined = import.meta.env.VITE_UMAMI_WEBSITE_ID,
  enabled: boolean = import.meta.env.PROD,
): void {
  if (!enabled || !websiteId || typeof document === 'undefined') return
  const script = document.createElement('script')
  script.defer = true
  script.src = UMAMI_SCRIPT
  script.dataset.websiteId = websiteId
  script.dataset.doNotTrack = 'true'
  document.head.appendChild(script)
}

/** Never throws: with no tracker (unset ID, ad blocker, script still loading)
 * the event is dropped. */
export function track<E extends keyof AnalyticsEvents>(event: E, data: AnalyticsEvents[E]): void {
  if (typeof window === 'undefined') return
  try {
    window.umami?.track(event, data)
  } catch {
    // An analytics failure must not reach the action that reported it.
  }
}
