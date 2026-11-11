// The studio: what's recording now, what's next (record-ahead queue), engine state.

import { api, on } from './api'
import { clear, fmtTime, h, icon, toast } from './dom'
import { player } from './player'
import { gpuHint, recMerge, recText, type RecState } from './recstatus'
import { spinner } from './waiting'

let live: RecState | null = null

export async function viewStudio() {
  const page = h('div.page')
  let timer = 0
  const render = async () => {
    if (!page.isConnected && page.dataset.mounted) { offs.forEach((f) => f()); return }
    page.dataset.mounted = '1'
    const [st, settings] = await Promise.all([api.get('/api/studio'), api.get('/api/settings')])
    clear(page).append(...studioView(st, settings))
  }
  const later = () => { clearTimeout(timer); timer = window.setTimeout(render, 200) }
  const paint = () => {
    if (!live) return
    const bar = page.querySelector<HTMLElement>('.now-rec .progress > div')
    // Only the backend's own progress moves the bar; a time-based guess doesn't.
    if (bar && !live.estimated) bar.style.width = `${Math.round((live.overall || 0) * 100)}%`
    const stage = page.querySelector('.now-rec .stage')
    if (stage) stage.textContent = recText(live)
  }
  const tick = window.setInterval(() => { if (!page.isConnected && page.dataset.mounted) { window.clearInterval(tick); return } paint() }, 1000)
  const offs = [on('studio', later), on('engine', later), on('recorded', later), on('recording', (e) => {
    live = recMerge(live, e.trackId, e.progress || {})
    paint()
  })]
  await render()
  return page
}

function studioView(st: any, settings: any) {
  const eng = st.engine || {}
  const caps = eng.capabilities || {}
  const out: HTMLElement[] = []
  out.push(h('header.page-head', h('h1', 'Studio'),
    h('div.head-actions',
      h('button', { onclick: () => api.post('/api/studio/pause', { paused: !st.paused }) }, st.paused ? 'Resume recording' : 'Pause recording'),
      eng.state === 'ready' ? h('button', { onclick: async () => { await api.post('/api/engine/restart'); toast('Engine stopped; it starts again when needed') } }, 'Unload engine')
        : h('button', { onclick: () => api.post('/api/engine/load') }, 'Load engine now'))))
  const hint = gpuHint(eng.error || st.blocked)
  if (st.blocked) out.push(h('div.engine-card.failed', h('b', 'Recording is waiting: '), st.blocked,
    h('div.small.muted', 'Press play on a song (or Retry on a failed one) to try again.')))
  out.push(h('div.engine-card', { class: eng.state },
    h('div', h('b', 'Engine: '), eng.state || 'stopped', eng.error ? h('span.warn', ' — ' + eng.error) : null),
    eng.state === 'loading' && eng.loading ? h('div.small', spinner(), ` ${eng.loading.stage} · ${Math.round(eng.loading.elapsed || 0)} s`) : null,
    hint ? h('div.small.warn', hint) : null,
    caps.maxSeconds ? h('div.small.muted', `Up to ${Math.round(caps.maxSeconds / 60)} min per song · lyrics ${caps.lyrics ? 'yes' : 'no'} · analysis ${caps.analyze ? 'yes' : 'no'} · reference audio ${caps.referenceAudio ? 'yes' : 'no'}${caps.languages?.length ? ' · ' + caps.languages.length + ' vocal languages' : ''}`) : null,
    h('div.small.muted.mono', eng.command || '')))
  const cur = st.current
  live = cur ? recMerge(live, cur.trackId, cur) : null
  if (cur) {
    const t = st.queue.find((q: any) => q.id === cur.trackId) || {}
    out.push(h('div.now-rec', icon('record', 12, 'icon rec'), h('div.grow', h('div', h('b', 'Recording: '), t.title || cur.trackId, t.artistName ? h('span.muted', ' — ' + t.artistName) : null),
      h('div.progress', h('div', { style: { width: `${cur.estimated ? 0 : Math.round((cur.overall || 0) * 100)}%` } })), h('div.small.muted.stage', recText(live || cur))),
      h('button', { onclick: () => api.post('/api/studio/cancel', { trackId: cur.trackId }) }, 'Stop')))
  } else out.push(h('p.muted', st.paused ? 'Recording is paused.' : 'Idle. Songs are recorded when you play them; the next few are recorded ahead.'))

  out.push(h('h2', 'Coming up'))
  const queue = st.queue.filter((t: any) => t.id !== cur?.trackId)
  if (!queue.length) out.push(h('p.muted.small', settings.studio.prefetch ? 'Everything is recorded.' : 'Nothing queued. Turn on “record while idle” to fill your library in the background.'))
  out.push(h('div.tracklist', queue.map((t: any, i: number) => h('div.track-row',
    h('span.num', String(i + 1)), h('img.mini-cover', { src: t.cover || '' }),
    h('div.track-main', h('div.track-title', t.title), h('div.track-sub', `${t.artistName} — ${t.albumTitle}`)),
    h('span.pill', t.status), h('span.time', fmtTime(t.targetSeconds)),
    h('button.icon-btn', { title: 'Play (records first)', onclick: () => player.playQueue([t], 0, 'Studio') }, icon('play', 14)),
    h('button.icon-btn', { title: 'Remove from queue', onclick: () => api.post('/api/studio/cancel', { trackId: t.id }) }, icon('x', 14))))))

  const look = h('input', { type: 'number', min: '0', max: '10', value: String(settings.studio.lookahead) }) as HTMLInputElement
  const prefetch = h('input', { type: 'checkbox', checked: settings.studio.prefetch }) as HTMLInputElement
  const quality = h('select', [[8, 'Demo (fast)'], [16, 'Balanced'], [32, 'Studio (slow)']].map(([v, l]) => h('option', { value: String(v), selected: settings.engine.quality === v }, String(l)))) as HTMLSelectElement
  const save = () => api.put('/api/settings', { studio: { lookahead: Number(look.value), prefetch: prefetch.checked }, engine: { quality: Number(quality.value) } }).then(() => toast('Saved'))
  ;[look, prefetch, quality].forEach((el) => el.addEventListener('change', save))
  out.push(h('h2', 'Recording ahead'), h('div.form-row',
    h('label', 'Songs recorded ahead of the one playing', look), h('label', 'Quality', quality), h('label.check', prefetch, ' Record while idle')))
  out.push(h('details.log', h('summary', 'Engine log'), logBox('engine')))
  return out
}

export function logBox(name: string) {
  const pre = h('pre.logtext', 'Loading…')
  api.get('/api/logs/' + name).then((r) => { pre.textContent = r.text || '(empty)'; pre.scrollTop = pre.scrollHeight }).catch(() => (pre.textContent = '(no log)'))
  return pre
}
