// One wording for "what the studio is doing" in the player pill and the Studio page, built only
// from what core and the engine report: stage, the engine's load step, step N of M, planner tokens.

export type RecState = {
  trackId: string
  stage?: string
  overall?: number
  detail?: string
  tokens?: number
  step?: number
  steps?: number
  started?: number      // ms epoch: this song's recording began (core clock = this machine)
  since?: number        // ms epoch: the current stage began
  loadElapsed?: number  // s, reported by the engine while loading
  estimated?: boolean   // overall is a time-based guess, not the backend's progress
  chunk?: number        // VAE decode: tiled chunk being decoded, of `chunks`
  chunks?: number
}

const STAGES: Record<string, string> = {
  writing: 'Writing the lyrics',
  loading: 'Loading the model',
  planning: 'Planning the song',
  composing: 'Composing',
  rendering: 'Decoding the audio',
  mastering: 'Finishing',
  encoding: 'Saving',
}

export function secs(s: number) {
  s = Math.max(0, Math.round(s))
  return s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${String(s % 60).padStart(2, '0')} s`
}

export function recText(r: RecState, now = Date.now()) {
  const parts = [STAGES[r.stage || ''] || r.stage || 'Starting']
  if (r.stage === 'loading') {
    if (r.detail) parts[0] = r.detail
  } else if (r.stage === 'rendering') {
    if (r.detail) parts[0] = r.detail
    if (r.chunk && r.chunks) parts.push(`chunk ${r.chunk} of ${r.chunks}`)
  } else if (r.step && r.steps) parts.push(`step ${r.step} of ${r.steps}`)
  else if (r.tokens) parts.push(`${r.tokens} tokens`)
  else if (r.detail && r.estimated) parts[0] = r.detail   // a backend with no progress says what it's waiting on
  const total = r.started ? (now - r.started) / 1000 : 0
  const stage = r.since ? (now - r.since) / 1000 : 0
  // The stage's own timer, plus the song's total once the stage isn't the first one.
  if (stage && total - stage > 2) parts.push(`${secs(stage)} (total ${secs(total)})`)
  else if (total) parts.push(secs(total))
  return parts.join(' · ')
}

export function recMerge(prev: RecState | null, trackId: string, p: any): RecState {
  const base: RecState = prev && prev.trackId === trackId ? prev : { trackId, started: p?.started || Date.now() }
  const next: RecState = { ...base, ...p, trackId }
  if (p?.stage && p.stage !== base.stage) next.since = p.since || Date.now()
  // Counters belong to their stage: don't carry "step 8 of 8" into decoding.
  if (p?.stage && p.stage !== base.stage) {
    if (!('step' in p)) { delete next.step; delete next.steps }
    if (!('tokens' in p)) delete next.tokens
    if (!('chunk' in p)) { delete next.chunk; delete next.chunks }
    if (!('detail' in p)) delete next.detail
  }
  return next
}

/** For errors caused by GPU memory: what to do about it. */
export function gpuHint(error?: string | null) {
  if (!error) return ''
  return /GPU memory|out of memory|HIP error|hipErrorOutOfMemory|CUDA error: out/i.test(error)
    ? 'Another app is probably holding GPU memory. Unload the model in LM Studio (or Ollama), then press play again.'
    : ''
}
