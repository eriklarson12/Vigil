import { afterEach, describe, expect, it, vi } from 'vitest'
import { loadUmami, track } from './analytics'

afterEach(() => {
  vi.unstubAllGlobals()
})

function stubDocument() {
  const appended: Array<{ src: string; defer: boolean; dataset: Record<string, string> }> = []
  vi.stubGlobal('document', {
    createElement: () => ({ src: '', defer: false, dataset: {} }),
    head: { appendChild: (el: (typeof appended)[number]) => appended.push(el) },
  })
  return appended
}

describe('loadUmami', () => {
  it('injects the tracker with the website id', () => {
    const appended = stubDocument()
    loadUmami('site-1', true)
    expect(appended).toHaveLength(1)
    expect(appended[0].src).toBe('https://cloud.umami.is/script.js')
    expect(appended[0].dataset).toEqual({ websiteId: 'site-1', doNotTrack: 'true' })
  })

  it('loads nothing without an id', () => {
    const appended = stubDocument()
    loadUmami(undefined, true)
    loadUmami('', true)
    expect(appended).toHaveLength(0)
  })

  it('loads nothing outside a production build', () => {
    const appended = stubDocument()
    loadUmami('site-1', false)
    expect(appended).toHaveLength(0)
  })
})

describe('track', () => {
  it('forwards the event and its data to the tracker', () => {
    const umami = { track: vi.fn() }
    vi.stubGlobal('window', { umami })
    track('candidate_expand', { rank: 1, culprit: true })
    expect(umami.track).toHaveBeenCalledWith('candidate_expand', { rank: 1, culprit: true })
  })

  it('drops the event when the tracker never loaded', () => {
    vi.stubGlobal('window', {})
    expect(() => track('brief_link', { host: 'github.com' })).not.toThrow()
  })

  it('swallows a tracker that throws', () => {
    vi.stubGlobal('window', {
      umami: {
        track: () => {
          throw new Error('blocked')
        },
      },
    })
    expect(() => track('brief_link', { host: 'github.com' })).not.toThrow()
  })
})
