// Where things stand in the global AI queue, shown on the page they belong to. The core sends one
// snapshot of every queue (chat and image requests per model, the music engine's recording queue,
// background tasks) with each item's position in its own resource's queue; a page asks for the
// items that belong to it (an artist, an album, a song, a link set, a job) and shows them live.

import { api, on } from './api'
import { clear, h, toast } from './dom'
import { spinner } from './waiting'

type Refs = { artistId?: string; albumId?: string; trackId?: string; setId?: string; briefId?: string; jobId?: string }
type Item = {
  id: string; source: 'call' | 'recording' | 'job'; kind: string; label: string; refs: Refs
  resource: string; resourceLabel: string; model?: string; state: string; phase?: string
  position?: number; positionText?: string; elapsed?: number | null; waited?: number | null
  progress?: { step?: number; steps?: number; fraction?: number } | null; message?: string; detail?: string
  cancel?: { what: string; label?: string } | null; error?: string | null; ended?: number
}
type Snapshot = { at: number; resources: { id: string; label: string; items: Item[] }[]; recent: Item[]; running: number; queued: number }

let snap: Snapshot | null = null
let received = 0
const subs = new Set<() => void>()
const dismissed = new Set<string>()

export function initQueue() {
  const set = (s: Snapshot) => { snap = s; received = Date.now(); for (const fn of subs) fn() }
  api.get('/api/queue').then(set).catch(() => {})
  on('queue', (e) => set(e.queue))
  // Elapsed seconds tick between snapshots.
  window.setInterval(() => { if (snap && snap.running) for (const fn of subs) fn() }, 1000)
}

export type Match = Refs & {
  /** Only items with none of these refs (e.g. an artist page leaves album and song items to their pages). */
  without?: (keyof Refs)[]
}

function matches(i: Item, m: Match) {
  for (const k of ['artistId', 'albumId', 'trackId', 'setId', 'briefId', 'jobId'] as (keyof Refs)[]) {
    if (m[k] && i.refs?.[k] !== m[k]) return false
  }
  for (const k of m.without || []) if (i.refs?.[k]) return false
  return true
}

/** Items for a page: a job's own card is dropped while one of its AI requests is in a queue (that
 *  request says more: which model, which place in line). */
export function itemsFor(m: Match): Item[] {
  if (!snap) return []
  const all = snap.resources.flatMap((r) => r.items)
  const mine = all.filter((i) => matches(i, m))
  const busyJobs = new Set(mine.filter((i) => i.source !== 'job' && i.refs?.jobId).map((i) => i.refs.jobId))
  return mine.filter((i) => !(i.source === 'job' && busyJobs.has(i.refs.jobId)))
}

function failedFor(m: Match): Item[] {
  if (!snap) return []
  const cutoff = Date.now() / 1000 - 15 * 60
  return snap.recent.filter((i) => i.state === 'failed' && (i.ended || 0) > cutoff && !dismissed.has(i.id) && matches(i, m))
}

function shortModel(model?: string) { return (model || '').split(/[-_/ ]/)[0] || model || 'the model' }
function secs(s?: number | null) {
  if (s == null) return ''
  const t = Math.max(0, Math.round(s + (Date.now() - received) / 1000))
  return t < 60 ? `${t} s` : `${Math.floor(t / 60)} min ${t % 60} s`
}

export function statusText(i: Item): string {
  const pos = i.positionText || ''
  if (i.source === 'call') {
    const images = i.resource.startsWith('image:')
    if (i.state === 'queued') return images ? `Waiting for images · ${pos}` : `Queued · ${pos} for ${shortModel(i.model)}`
    if (i.phase === 'loading') return `Loading ${i.model}… ${secs(i.elapsed)}`
    return `${images ? 'Drawing with' : 'Writing with'} ${shortModel(i.model)} · ${secs(i.elapsed)}`
  }
  if (i.source === 'recording') {
    if (i.state === 'queued') return `Queued to record · ${pos}`
    const p = i.progress
    if (p?.step && p?.steps) return `Recording · step ${p.step}/${p.steps}`
    if (i.phase === 'loading') return `Starting the engine${i.detail ? ` · ${i.detail}` : ''}`
    if (i.phase === 'writing') return 'Writing lyrics before recording'
    return `Recording${p?.fraction ? ` · ${Math.round(p.fraction * 100)}%` : ''} · ${secs(i.elapsed)}`
  }
  if (i.state === 'queued') return `Waiting to start · ${pos}`
  return `${i.message || 'Working'} · ${secs(i.elapsed)}`
}

function line(i: Item, showLabel: boolean) {
  const queued = i.state === 'queued'
  const canCancel = !!i.cancel && (queued || i.source !== 'call')
  const frac = i.progress?.fraction
  const what = i.cancel?.label && i.cancel.what === 'job' && i.cancel.label !== i.label ? ` (cancels “${i.cancel.label}”)` : ''
  return h('div.qline', { class: queued ? 'queued' : 'running', title: `${i.label} — ${i.resourceLabel}` },
    queued ? h('span.qdot') : spinner(),
    h('span.qtext', showLabel ? h('span.qlabel', i.label + ' · ') : null, statusText(i)),
    frac != null && !queued ? h('span.qbar', h('span', { style: { width: `${Math.round(frac * 100)}%` } })) : null,
    canCancel ? h('button.link.small.qcancel', { type: 'button', title: `Cancel${what}`, onclick: async (e: Event) => {
      e.stopPropagation()
      const r = await api.post('/api/queue/cancel', { id: i.id }).catch((err: any) => ({ ok: false, error: err?.message }))
      if (!r?.ok) toast(r?.error || 'Couldn’t cancel it.', 'error')
    } }, 'Cancel') : null)
}

/** A live block listing this page's queued and running items, plus failures of the last minutes.
 *  Empty (and hidden) when nothing of the page's is waiting. */
export function queueStatus(m: Match, opts: { labels?: boolean; max?: number; compact?: boolean } = {}) {
  const box = h('div.qstatus', { class: opts.compact ? 'compact' : '' })
  let seen = false
  const paint = () => {
    // Unsubscribes once it has been on a page and left it (pages are built before they're shown).
    if (box.isConnected) seen = true
    else if (seen) { subs.delete(paint); return }
    const items = itemsFor(m)
    const failed = failedFor(m)
    clear(box)
    box.style.display = items.length || failed.length ? '' : 'none'
    const max = opts.max ?? 3
    for (const i of items.slice(0, max)) box.append(line(i, opts.labels ?? true))
    if (items.length > max) box.append(h('div.qline.more.small.muted', `+${items.length - max} more waiting`))
    for (const f of failed.slice(0, 2)) {
      box.append(h('div.qline.failed', { title: f.error || '' },
        h('span.qtext', (opts.labels ?? true) ? h('span.qlabel', f.label + ' · ') : null, `Failed: ${f.error || 'unknown error'}`),
        h('button.link.small', { type: 'button', title: 'Dismiss', onclick: () => { dismissed.add(f.id); paint() } }, '×')))
    }
  }
  subs.add(paint)
  paint()
  return box
}
