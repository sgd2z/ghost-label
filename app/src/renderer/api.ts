// The core's HTTP API (same origin; the token cookie is set when the window loads).

export class ApiError extends Error {}

// Reads give up after this long, so a view shows an error with Retry instead of staying blank.
const READ_TIMEOUT_MS = 30000

async function call(method: string, path: string, body?: unknown) {
  const ctrl = method === 'GET' ? new AbortController() : null
  const timer = ctrl ? window.setTimeout(() => ctrl.abort(), READ_TIMEOUT_MS) : 0
  let r: Response
  try {
    r = await fetch(path, { method, signal: ctrl?.signal, headers: body !== undefined ? { 'Content-Type': 'application/json' } : {}, body: body !== undefined ? JSON.stringify(body) : undefined })
  } catch (error) {
    if (ctrl?.signal.aborted) throw new ApiError(`The app's core didn't answer ${path} within ${READ_TIMEOUT_MS / 1000} s.`)
    throw new ApiError(`Couldn't reach the app's core (${(error as Error).message || error}).`)
  } finally { window.clearTimeout(timer) }
  const text = await r.text()
  let data: any = null
  try { data = text ? JSON.parse(text) : null } catch { data = { error: text } }
  if (!r.ok) throw new ApiError((data && data.error) || `${r.status} ${r.statusText}`)
  return data
}

export const api = {
  get: (p: string) => call('GET', p),
  post: (p: string, b: unknown = {}) => call('POST', p, b),
  put: (p: string, b: unknown = {}) => call('PUT', p, b),
  del: (p: string) => call('DELETE', p),
}

/** Start a job (POST returns its row at once) and resolve with its result when it finishes:
 *  long AI work never holds one of the window's few HTTP connections open. */
export async function runJob(path: string, body: unknown = {}): Promise<any> {
  const job = await call('POST', path, body)
  if (!job?.id || !job.status) return job
  return new Promise((resolve, reject) => {
    let done = false
    const finish = (row: any) => {
      if (done || !row || row.id !== job.id) return
      if (row.status === 'finished') { done = true; off(); clearInterval(poll); resolve(row.result || row.result_json || {}) }
      else if (['failed', 'cancelled', 'interrupted'].includes(row.status)) { done = true; off(); clearInterval(poll); reject(new ApiError(row.error || `The job was ${row.status}`)) }
    }
    const off = on('job', (e) => finish(e.job))
    // Events can be missed (a reconnecting stream): look every few seconds too.
    const poll = window.setInterval(() => { call('GET', `/api/jobs/${job.id}`).then(finish).catch(() => {}) }, 4000)
    finish(job)
  })
}

type Listener = (e: any) => void
const listeners: Record<string, Set<Listener>> = {}

export function on(type: string, fn: Listener) {
  ;(listeners[type] ||= new Set()).add(fn)
  return () => listeners[type].delete(fn)
}

export function connectEvents() {
  const es = new EventSource('/api/events')
  es.onmessage = (m) => {
    try {
      const e = JSON.parse(m.data)
      for (const fn of listeners[e.type] || []) fn(e)
      for (const fn of listeners['*'] || []) fn(e)
    } catch { /* ignore */ }
  }
  es.onerror = () => { /* EventSource reconnects by itself */ }
}

export const desktop: any = (window as any).desktop || {
  openFile: async () => prompt('Path to a .glalbum file'),
  saveFile: async (name: string) => prompt('Save share as (full path)', name),
  chooseFolder: async () => prompt('Folder path'),
  reveal: async () => {}, openPath: async () => {}, info: async () => ({ version: 'web', platform: 'web' }),
  onOpenShare: () => {},
}
