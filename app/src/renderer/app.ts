import { gpuHint, recText } from './recstatus'
import { api, connectEvents, desktop, on } from './api'
import { activityPill, initActivity, spinner } from './waiting'
import { initQueue } from './queue'
import { clear, fmtTime, h, icon, toast } from './dom'
import { activeLine, parseLyrics, player } from './player'
import { viewAlbum, viewAlbums, viewArtist, viewArtists, viewFavorites, viewHome, viewSearch, viewSongs, openShareFile } from './views'
import { viewCreate } from './create'
import { viewLinks, viewLinkSet } from './links'
import { viewStudio } from './studio'
import { viewSettings } from './settings'
import { viewOnboarding } from './onboarding'

export const app = {
  state: null as any,
  sidePanel: (localStorage.getItem('sidePanel') || 'none') as 'none' | 'queue' | 'lyrics',
  rerender: () => route(),
}

const root = document.getElementById('root')!
const content = h('main.content')
const side = h('aside.side-panel')
const nav = h('nav.sidebar')
const bar = h('footer.player-bar')
root.append(h('div.shell', nav, h('div.center', content, side), bar))

const NAV = [
  ['home', 'Home', 'home'], ['artists', 'Artists', 'artists'], ['albums', 'Albums', 'albums'], ['songs', 'Songs', 'songs'],
  ['favorites', 'Favorites', 'heart'], null, ['create', 'Create', 'sparkle'], ['links', 'From links', 'link'],
  ['studio', 'Studio', 'studio'], ['settings', 'Settings', 'settings'],
] as const

function renderNav() {
  const current = location.hash.replace(/^#\/?/, '').split('/')[0] || 'home'
  const search = h('input.search', { type: 'search', placeholder: 'Search your library', value: decodeURIComponent((location.hash.match(/search\/(.*)$/) || [])[1] || ''),
    onkeydown: (e: KeyboardEvent) => { if (e.key === 'Enter') location.hash = '#/search/' + encodeURIComponent((e.target as HTMLInputElement).value) } })
  clear(nav).append(
    h('div.brand', h('img.brand-mark', { src: '/icon.png', alt: '' }), h('span', 'Ghost Label')),
    h('div.search-wrap', icon('search', 15), search),
    ...NAV.map((n) => n === null ? h('div.nav-sep') :
      h('a.nav-item', { href: '#/' + n[0], class: current === n[0] || (current === 'artist' && n[0] === 'artists') || (current === 'album' && n[0] === 'albums') ? 'active' : '' }, icon(n[2], 17), h('span', n[1]))),
    h('div.nav-foot', studioBadge),
  )
}

const studioBadge = h('div.studio-badge')
function renderStudioBadge(status: any) {
  clear(studioBadge)
  if (!status) return
  const cur = status.current
  if (cur) {
    studioBadge.append(h('div.badge-title', icon('record', 10, 'icon rec'), ' Recording'),
      h('div.progress', h('div', { style: { width: `${Math.round((cur.overall || 0) * 100)}%` } })),
      h('div.badge-sub', `${cur.stage || 'starting'}${status.queue.length > 1 ? ` · ${status.queue.length - 1} queued` : ''}`))
  } else if (status.engine?.state && status.engine.state !== 'stopped') {
    studioBadge.append(h('div.badge-sub', `Engine ${status.engine.state}`))
  }
}

let routeSeq = 0
let shownHash = ''
async function route() {
  const seq = ++routeSeq
  renderNav()
  const parts = location.hash.replace(/^#\/?/, '').split('/')
  const [page, id] = parts
  // A refresh of the page already shown keeps it on screen until the new one is ready; a new
  // page shows a loading line at once (never an empty main area).
  const refresh = shownHash === location.hash && content.childElementCount > 0
  if (!refresh) {
    clear(content).append(h('div.empty.loading', spinner(), h('span', ' Loading…')))
    content.scrollTop = 0
  }
  try {
    if (!app.state) app.state = await api.get('/api/state')
    if (!app.state.onboarded && page !== 'settings' && page !== 'onboarding') { location.hash = '#/onboarding'; return }
    const views: Record<string, () => Promise<HTMLElement> | HTMLElement> = {
      '': viewHome, home: viewHome, artists: viewArtists, albums: viewAlbums, songs: viewSongs, favorites: viewFavorites,
      artist: () => viewArtist(id), album: () => viewAlbum(id), search: () => viewSearch(decodeURIComponent(parts.slice(1).join('/'))),
      create: () => viewCreate(id), links: () => (id ? viewLinkSet(id) : viewLinks()), studio: viewStudio,
      settings: () => viewSettings(id), onboarding: viewOnboarding, nowplaying: viewNowPlaying,
    }
    const el = await (views[page] || viewHome)()
    if (seq !== routeSeq) return  // a newer navigation won
    clear(content).append(el)
    shownHash = location.hash
  } catch (error) {
    if (seq !== routeSeq) return
    if (refresh) { toast(`Couldn't refresh: ${(error as Error).message || error}`, 'error'); return }
    clear(content).append(h('div.empty', h('h2', 'This page didn’t load'), h('p', String((error as Error).message || error)),
      h('button.primary', { onclick: () => route() }, 'Retry')))
    shownHash = ''
  }
}

// MARK: player bar

const barArt = h('img.bar-art', { alt: '' })
const barTitle = h('div.bar-title')
const barSub = h('div.bar-sub')
const playBtn = h('button.round.play', { title: 'Play/Pause', onclick: () => player.toggle() })
const scrub = h('input.scrub', { type: 'range', min: '0', max: '1000', value: '0', oninput: (e: Event) => {
  const v = Number((e.target as HTMLInputElement).value) / 1000
  if (player.audio.duration) player.seek(v * player.audio.duration)
} }) as HTMLInputElement
const tNow = h('span.time', '0:00')
const tEnd = h('span.time', '–:––')
const vol = h('input.vol', { type: 'range', min: '0', max: '100', value: String(Math.round(player.settings.volume * 100)),
  oninput: (e: Event) => player.setVolume(Number((e.target as HTMLInputElement).value) / 100) }) as HTMLInputElement
const favBtn = h('button.icon-btn', { title: 'Favorite', onclick: async () => {
  const t = player.current; if (!t) return
  const r = await api.post('/api/favorite', { kind: 'track', id: t.id, on: !t.favorite }); player.updateTrack({ ...t, favorite: r.favorite })
} })
const status = h('div.bar-status')

function sideToggle(kind: 'queue' | 'lyrics') {
  app.sidePanel = app.sidePanel === kind ? 'none' : kind
  localStorage.setItem('sidePanel', app.sidePanel)
  renderSide(); renderBar()
}

bar.append(
  h('div.bar-left', h('a', { href: '#/nowplaying', title: 'Now playing' }, barArt), h('div.bar-text', barTitle, barSub), favBtn),
  h('div.bar-center',
    h('div.controls', h('button.icon-btn', { title: 'Previous', onclick: () => player.prev() }, icon('prev', 18)), playBtn,
      h('button.icon-btn', { title: 'Next', onclick: () => player.next() }, icon('next', 18))),
    h('div.scrub-row', tNow, scrub, tEnd), status),
  h('div.bar-right',
    h('button.icon-btn', { id: 'lyrics-btn', title: 'Lyrics', onclick: () => sideToggle('lyrics') }, icon('lyrics', 18)),
    h('button.icon-btn', { id: 'queue-btn', title: 'Up next', onclick: () => sideToggle('queue') }, icon('queue', 18)),
    icon('volume', 16), vol),
)

function renderBar() {
  const t = player.current
  if (t?.cover) { if (barArt.getAttribute('src') !== t.cover) barArt.setAttribute('src', t.cover); barArt.style.visibility = 'visible' }
  else barArt.style.visibility = 'hidden'
  barTitle.textContent = t ? t.title : 'Nothing playing'
  barSub.textContent = t ? `${t.artistName || ''} — ${t.albumTitle || ''}` : ''
  clear(playBtn).append(icon(player.playing ? 'pause' : 'play', 20))
  clear(favBtn).append(icon('heart', 16, t?.favorite ? 'icon filled' : 'icon'))
  const d = player.audio.duration || t?.duration || 0
  tNow.textContent = fmtTime(player.audio.currentTime || 0)
  tEnd.textContent = fmtTime(d)
  if (document.activeElement !== scrub) scrub.value = String(d ? Math.round((player.audio.currentTime / d) * 1000) : 0)
  document.getElementById('lyrics-btn')?.classList.toggle('on', app.sidePanel === 'lyrics')
  document.getElementById('queue-btn')?.classList.toggle('on', app.sidePanel === 'queue')
  clear(status)
  if (t && player.waiting) {
    const rec = player.recording && player.recording.trackId === t.id ? player.recording : null
    const other = player.recording && !rec ? player.recording : null
    const engineDown = player.engine.state === 'failed' && player.engine.error
    let text: string
    let error = ''
    if (rec) text = ` ${recText(rec)}`
    else if (t.status === 'failed') { error = t.error || ''; text = ` Recording failed: ${error}` }
    else if (engineDown) { error = player.engine.error || ''; text = ` The engine stopped: ${error}` }
    else if (other) text = ` Waiting for the studio (another song first: ${recText(other)})`
    else text = ' Waiting for the studio…'
    const hint = gpuHint(error)
    status.append(h('span.pill.rec', { class: error ? 'error' : '', title: [text.trim(), hint].filter(Boolean).join('\n') },
      rec ? spinner() : icon('record', 9, 'icon rec'), text),
      ...(error ? [h('button.small.retry', { onclick: () => player.retry(t.id) }, 'Retry')] : []),
      ...(hint ? [h('span.small.warn.rec-hint', ' ' + hint)] : []))
  }
}

// MARK: side panel (Up next / Lyrics)

let lyricLines: HTMLElement[] = []
let lyricsFor = ''
function renderSide() {
  side.classList.toggle('open', app.sidePanel !== 'none')
  clear(side)
  lyricsFor = ''
  if (app.sidePanel === 'queue') {
    side.append(h('h3', 'Up next'), h('div.muted.small', player.source ? 'Playing from ' + player.source : ''),
      h('div.queue', player.queue.map((t, i) => h('div.queue-row', { class: i === player.index ? 'current' : '', onclick: () => player.go(i) },
        h('img', { src: t.cover || '' }), h('div', h('div.qt', t.title), h('div.muted.small', `${t.artistName || ''}${t.status !== 'recorded' ? ' · not recorded yet' : ''}`)),
        h('button.icon-btn', { title: 'Remove', onclick: (e: Event) => { e.stopPropagation(); if (i !== player.index) { player.queue.splice(i, 1); if (i < player.index) player.index--; player.persist(); renderSide() } } }, icon('x', 14))))))
  } else if (app.sidePanel === 'lyrics') {
    side.append(h('h3', 'Lyrics'), lyricsBox)
    renderLyrics(lyricsBox, true)
  }
}
const lyricsBox = h('div.lyrics')

export function renderLyrics(box: HTMLElement, force = false) {
  const t = player.current
  const key = (t?.id || '') + (t?.lyrics || '').length
  if (force || key !== lyricsFor || box.childElementCount === 0) {
    lyricsFor = key
    clear(box)
    lyricLines = []
    if (!t) { box.append(h('p.muted', 'Play something to see its lyrics.')); return }
    if (t.instrumental) { box.append(h('p.muted', 'Instrumental')); return }
    if (!t.lyrics) { box.append(h('p.muted', 'The lyrics are written just before the song is recorded.')); return }
    for (const line of parseLyrics(t.lyrics)) {
      const el = h('div', { class: line.section ? 'lyric-section' : 'lyric-line' }, line.text)
      ;(el as any)._weight = line.weight
      lyricLines.push(el)
      box.append(el)
    }
  }
  const d = player.audio.duration || 0
  if (!d || !lyricLines.length) return
  const lines = lyricLines.map((el) => ({ text: '', weight: (el as any)._weight }))
  const i = activeLine(lines, player.audio.currentTime / d)
  lyricLines.forEach((el, j) => { el.classList.toggle('active', j === i); el.classList.toggle('past', j < i) })
  const active = lyricLines[i]
  if (active && (active as any)._scrolled !== true) {
    lyricLines.forEach((el) => ((el as any)._scrolled = false))
    ;(active as any)._scrolled = true
    active.scrollIntoView({ block: 'center', behavior: 'smooth' })
  }
}

// MARK: now playing

async function viewNowPlaying() {
  const t = player.current
  if (!t) return h('div.empty', h('h2', 'Nothing playing'), h('p', 'Pick an album and press play.'))
  const box = h('div.lyrics.big')
  const art = h('img.np-art', { src: t.cover || '' })
  const view = h('div.nowplaying',
    h('div.np-left', art, h('h1', t.title), h('a.muted', { href: '#/artist/' + t.artistId }, t.artistName || ''),
      h('a.muted.small', { href: '#/album/' + t.albumId }, t.albumTitle || ''),
      t.style ? h('p.np-style', t.style) : null),
    h('div.np-right', box))
  const tick = () => {
    if (!view.isConnected) return
    renderLyrics(box)
    const lvl = player.level()
    art.style.transform = `scale(${1 + lvl * 0.04})`
    requestAnimationFrame(tick)
  }
  requestAnimationFrame(tick)
  lyricsFor = ''
  return view
}

// MARK: boot

// Elapsed time keeps counting between engine messages.
setInterval(() => { if (player.waiting) renderBar() }, 1000)
player.subscribe(() => {
  renderBar()
  if (app.sidePanel === 'lyrics') renderLyrics(lyricsBox)
  if (app.sidePanel === 'queue') {
    const sig = player.index + ':' + player.queue.map((t) => t.id + t.status).join(',')
    if (side.dataset.sig !== sig) { side.dataset.sig = sig; renderSide() }
  }
})
let lastStudio: any = null
on('studio', (e) => { lastStudio = e.studio; renderStudioBadge(e.studio) })
on('recording', (e) => {
  if (lastStudio?.current && lastStudio.current.trackId === e.trackId) {
    lastStudio.current = { ...lastStudio.current, ...e.progress }
    renderStudioBadge(lastStudio)
  }
})
on('engine', () => api.get('/api/studio').then(renderStudioBadge).catch(() => {}))
on('job', (e) => {
  const j = e.job
  if (j.status === 'finished' && ['build', 'custom', 'links', 'links-refine', 'install', 'runtime'].includes(j.kind)) toast(`${j.title}: done`)
  // Artwork and taste jobs report to the button that started them.
  if (j.status === 'failed' && !['artwork', 'taste'].includes(j.kind)) toast(`${j.title}: ${j.error}`, 'error')
})
let libTimer = 0
on('library', () => {
  const page = location.hash.replace(/^#\/?/, '').split('/')[0] || 'home'
  if (['home', 'albums', 'artists', 'songs'].includes(page)) { clearTimeout(libTimer); libTimer = window.setTimeout(() => { const y = content.scrollTop; route().then(() => (content.scrollTop = y)) }, 800) }
})
window.addEventListener('hashchange', route)
window.addEventListener('keydown', (e) => {
  if ((e.target as HTMLElement).matches('input, textarea, select')) return
  if (e.code === 'Space') { e.preventDefault(); player.toggle() }
})
desktop.onOpenShare((path: string) => openShareFile(path))
connectEvents()
initQueue()
initActivity()
document.body.append(activityPill())
// Any failed save/request that a view didn't handle still shows up instead of failing silently.
window.addEventListener('unhandledrejection', (e) => {
  const err = (e as PromiseRejectionEvent).reason
  if (err?.name === 'AbortError') return
  toast(String(err?.message || err || 'Something failed'), 'error')
})
api.get('/api/studio').then((st) => { lastStudio = st; renderStudioBadge(st) }).catch(() => {})
renderBar(); renderSide(); route()
