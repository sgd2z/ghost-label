// Waiting states for anything that waits on the AI provider: a spinner, the model being waited
// on, elapsed seconds, and the core's status line. Driven by the core's `llm-activity` events
// (the requests actually in flight); nothing here invents progress.

import { api, on } from './api'
import { clear, h } from './dom'

type Call = { model: string; base: string; state: string; timeout: number; startedAt: number; kind?: string; purpose?: string }
let calls: Call[] = []
// The last request the server refused because it doesn't have the model (shown in the pill).
let problem: { model: string; message: string } | null = null
const subs = new Set<() => void>()

export function initActivity() {
  const set = (list: any[]) => {
    const now = Date.now()
    calls = (list || []).map((c) => ({ ...c, startedAt: now - (Number(c.age) || 0) * 1000 }))
    subs.forEach((f) => f())
  }
  on('llm-activity', (e) => { problem = e.problem || null; set(e.calls) })
  api.get('/api/llm/activity').then(set).catch(() => {})
  api.get('/api/llm/problem').then((r) => { problem = r?.problem || null; subs.forEach((f) => f()) }).catch(() => {})
}

export function onActivity(fn: () => void) { subs.add(fn); return () => { subs.delete(fn) } }
export function activeCalls() { return calls }
export function modelProblem() { return problem }

export const spinner = () => h('span.spinner', { 'aria-hidden': 'true' })

const secs = (since: number) => `${Math.max(0, Math.round((Date.now() - since) / 1000))} s`

/** "Waiting for qwen3-4b to respond… 12 s" while a request is in flight, else `fallback… 12 s`. */
export function waitingText(since: number, fallback = 'Working…') {
  if (calls.length) {
    // An image being drawn names the image model, not the chat model.
    const c = calls.find((x) => x.kind === 'image' && x.state !== 'queued') || calls.find((x) => x.state !== 'queued') || calls[0]
    // "(2 running, 4 queued)": background work waits its turn instead of piling onto the server.
    const queued = calls.filter((x) => x.state === 'queued').length
    const running = calls.length - queued
    const more = calls.length > 1 ? ` (${queued ? `${running} running, ${queued} queued` : `${calls.length} requests`})` : ''
    if (c.kind === 'image' && c.state === 'loading') return `Loading ${c.model}… ${secs(since)}${more}`
    if (c.kind === 'image') return c.state === 'queued' ? `Queued for ${c.model}… ${secs(since)}${more}` : `${c.model} is drawing the ${c.purpose || 'image'}… ${secs(since)}${more}`
    // A labelled request says what it is for: "Writing the photo prompt (qwen3-coder)… 4 s".
    if (c.purpose) return `Writing the ${c.purpose} (${c.model})… ${secs(since)}${more}`
    return c.state === 'queued' ? `Queued for ${c.model}… ${secs(since)}${more}` : `Waiting for ${c.model} to respond… ${secs(since)}${more}`
  }
  return `${fallback} ${secs(since)}`
}

/** Ticks `paint` every second and on activity changes until the returned stop() is called. */
function ticker(paint: () => void, statusTag?: string, status?: HTMLElement) {
  paint()
  const t = window.setInterval(paint, 1000)
  const off = onActivity(paint)
  const offStatus = statusTag && status ? on('llm', (e) => { if (e.tag === statusTag) status.textContent = e.text }) : () => {}
  return () => { window.clearInterval(t); off(); offStatus() }
}

/** A block: spinner + waiting line + optional status line. */
export function waitPanel(fallback: string, statusTag?: string) {
  const since = Date.now()
  const line = h('span')
  const status = h('div.small.muted.wait-status')
  const el = h('div.waiting', h('div.wait-line', spinner(), line), status)
  const stop = ticker(() => { line.textContent = waitingText(since, fallback) }, statusTag, status)
  return { el, stop, since }
}

/** Turns a button into a disabled waiting button until done() is called (restores it). */
export function busy(btn: HTMLButtonElement, fallback = 'Working…', statusTag?: string, status?: HTMLElement) {
  const since = Date.now()
  const original = [...btn.childNodes]
  const wasDisabled = btn.disabled
  btn.disabled = true
  btn.classList.add('busy')
  const label = h('span')
  clear(btn).append(spinner(), label)
  const stop = ticker(() => { label.textContent = waitingText(since, fallback) }, statusTag, status)
  return () => { stop(); btn.classList.remove('busy'); btn.disabled = wasDisabled; clear(btn).append(...original) }
}

/** Run `work` with `btn` in its waiting state. */
export async function withBusy<T>(btn: HTMLButtonElement, work: () => Promise<T>, fallback?: string): Promise<T> {
  const done = busy(btn, fallback)
  try { return await work() } finally { done() }
}

/** A small floating indicator whenever any AI request is in flight anywhere in the app. */
export function activityPill() {
  const text = h('span')
  const spin = spinner()
  const el = h('div.llm-pill', { title: 'AI requests in flight' }, spin, text)
  const paint = () => {
    // A refused model stays visible (until a request to it works again), with or without calls.
    const stuck = problem && !calls.some((c) => c.model === problem!.model && c.state === 'sent')
    el.style.display = calls.length || stuck ? '' : 'none'
    el.classList.toggle('problem', !!stuck && !calls.length)
    spin.style.display = calls.length ? '' : 'none'
    el.title = stuck ? problem!.message : 'AI requests in flight'
    if (calls.length) text.textContent = waitingText(Math.min(...calls.map((c) => c.startedAt))) + (stuck ? ` · ⚠ ${problem!.model} isn't on the server` : '')
    else if (stuck) text.textContent = `⚠ ${problem!.message}`
  }
  el.addEventListener('click', () => { if (problem && !calls.length) { location.hash = '#/settings'; } })
  ticker(paint)
  return el
}
