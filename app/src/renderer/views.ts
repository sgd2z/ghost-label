// Library views: Home, Artists, Artist, Albums, Album, Songs, Favorites, Search, plus sharing.

import { api, desktop, on, runJob } from './api'
import { clear, feedbackDialog, fmtTime, h, icon, modal, toast } from './dom'
import { player, Track } from './player'
import { withBusy } from './waiting'
import { queueStatus } from './queue'

export function cover(src: string | null | undefined, cls = 'cover') {
  return src ? h('img', { class: cls, src, loading: 'lazy', alt: '' }) : h('div', { class: cls + ' blank' })
}

export function heart(kind: string, id: string, on: boolean, size = 16) {
  const btn = h('button.icon-btn.heart', { title: on ? 'Remove from favorites' : 'Add to favorites' })
  const paint = () => { clear(btn).append(icon('heart', size, on ? 'icon filled' : 'icon')) }
  btn.addEventListener('click', async (e) => {
    e.stopPropagation(); e.preventDefault()
    const r = await api.post('/api/favorite', { kind, id, on: !on }); on = r.favorite; paint()
  })
  paint()
  return btn
}

export function albumCard(a: any) {
  const pct = a.trackCount ? Math.round((a.recorded / a.trackCount) * 100) : 0
  return h('a.card', { href: '#/album/' + a.id },
    h('div.card-art', cover(a.cover), h('button.card-play', { title: 'Play', onclick: (e: Event) => { e.preventDefault(); playAlbum(a.id) } }, icon('play', 18))),
    h('div.card-title', a.title), h('div.card-sub', a.artistName || ''),
    a.trackCount && a.recorded < a.trackCount ? h('div.card-meta', `${a.recorded}/${a.trackCount} recorded`, h('div.mini-progress', h('div', { style: { width: pct + '%' } }))) : null)
}

export function artistCard(a: any) {
  return h('a.card.artist', { href: '#/artist/' + a.id }, cover(a.portrait, 'cover round'),
    h('div.card-title', a.name), h('div.card-sub', (a.genres || []).slice(0, 2).join(' · ') || a.origin || ''))
}

export function grid(items: HTMLElement[]) { return h('div.grid', items) }
/** A titled row of cards that fills the content width: as many cards as fit on one line, sized
 *  with the window. When more cards exist than fit, the rest are hidden behind 'See all' (a link
 *  when the shelf has its own page, otherwise it expands the shelf in place). */
export function shelf(title: string, items: HTMLElement[], more?: string) {
  const row = h('div.row', items)
  const toggle = h('button.link.muted.small', { type: 'button', style: 'display:none' }, 'See all') as HTMLButtonElement
  const link = more ? h('a.muted.small', { href: more, style: 'display:none' }, 'See all') : null
  let expanded = false
  const fit = () => {
    const columns = getComputedStyle(row).gridTemplateColumns.split(' ').filter(Boolean).length || items.length
    const overflow = items.length > columns
    items.forEach((el, i) => { el.hidden = !expanded && i >= columns })
    if (link) link.style.display = overflow ? '' : 'none'
    toggle.style.display = !link && overflow ? '' : 'none'
    toggle.textContent = expanded ? 'Show less' : 'See all'
  }
  toggle.addEventListener('click', () => { expanded = !expanded; row.classList.toggle('expanded', expanded); fit() })
  new ResizeObserver(fit).observe(row)
  return h('section.shelf', h('div.shelf-head', h('h2', title), link, toggle), row)
}

export async function playAlbum(albumId: string, start = 0) {
  const a = await api.get('/api/albums/' + albumId)
  player.playQueue(a.tracks, start, a.title)
}

function statusPill(t: Track) {
  // Lemonade selected: a song needing reference audio went to the built-in engine, or went without it.
  if (t.status === 'recorded' && t.recordNote) return h('span.pill', { title: t.recordNote }, t.engine === 'acestep' ? 'built-in engine' : 'no reference audio')
  if (t.status === 'recorded') return null
  if (t.status === 'recording') return h('span.pill.rec', icon('record', 8, 'icon rec'), t.audio ? ' recording a new take' : ' recording')
  if (t.status === 'queued' && t.audio) return h('span.pill', 'new take queued')
  if (t.status === 'failed') return h('span.pill-group', h('span.pill.err', { title: t.error || '' }, 'failed'),
    h('button.small.retry', { title: 'Record this song again', onclick: (e: Event) => { e.stopPropagation(); player.retry(t.id) } }, 'Retry'))
  return h('span.pill', t.status === 'written' ? 'written' : 'not recorded')
}

export function trackList(tracks: Track[], opts: { showAlbum?: boolean; source?: string } = {}) {
  const box = h('div.tracklist')
  const rows = new Map<string, HTMLElement>()
  // Keep status, length and the playing marker live while the studio records.
  const offTrack = on('track', (e) => {
    if (!box.isConnected) return offTrack()
    const t = e.track as Track
    const i = tracks.findIndex((x) => x.id === t?.id)
    if (i < 0) return
    tracks[i] = { ...tracks[i], ...t }
    const old = rows.get(t.id)
    if (old) { const fresh = row(tracks[i], i); old.replaceWith(fresh) }
  })
  const offDeleted = on('deleted', (e) => {
    if (!box.isConnected) { offDeleted(); return }
    for (const id of e.trackIds || []) { rows.get(id)?.remove(); rows.delete(id); const k = tracks.findIndex((x) => x.id === id); if (k >= 0) tracks.splice(k, 1) }
  })
  const offPlayer = player.subscribe(() => {
    if (!box.isConnected) { offPlayer(); return }
    for (const [id, el] of rows) el.classList.toggle('current', player.current?.id === id)
  })
  const row = (t: Track, i: number) => {
    const el = rowEl(t, i)
    rows.set(t.id, el)
    return el
  }
  const rowEl = (t: Track, i: number) => {
    const isCurrent = player.current?.id === t.id
    return h('div.track-row', { class: isCurrent ? 'current' : '', ondblclick: () => player.playQueue(tracks, i, opts.source || '') },
      h('button.icon-btn.num', { onclick: () => player.playQueue(tracks, i, opts.source || '') }, h('span', String(opts.showAlbum ? i + 1 : t.number)), icon('play', 14)),
      h('div.track-main', h('div.track-title', { title: t.feedback?.length ? feedbackTip(t.feedback) : '' }, t.title, t.explicit ? h('span.explicit', 'E') : null, t.language && t.language !== 'en' ? h('span.lang', t.language) : null),
        h('div.track-sub', opts.showAlbum ? `${t.artistName} — ${t.albumTitle}` : (t.theme || '')),
        queueStatus({ trackId: t.id }, { labels: false, max: 2, compact: true })),
      statusPill(t),
      heart('track', t.id, !!t.favorite, 14),
      h('span.time', fmtTime(t.duration || t.targetSeconds)),
      h('button.icon-btn', { title: 'More', onclick: (e: Event) => trackMenu(e, t, tracks) }, icon('more', 16)))
  }
  tracks.forEach((t, i) => box.append(row(t, i)))
  return box
}

function menu(e: Event, items: [string, () => any][]) {
  e.stopPropagation()
  document.querySelectorAll('.menu').forEach((m) => m.remove())
  const r = (e.currentTarget as HTMLElement).getBoundingClientRect()
  const m = h('div.menu', { style: { top: `${r.bottom + 4}px`, left: `${Math.max(8, r.right - 200)}px` } },
    items.map(([label, fn]) => h('button', { onclick: () => { m.remove(); fn() } }, label)))
  document.body.append(m)
  setTimeout(() => document.addEventListener('click', () => m.remove(), { once: true }))
}

function trackMenu(e: Event, t: Track, _all: Track[]) {
  menu(e, [
    ['Play next', () => player.enqueue([t], true)],
    ['Add to Up next', () => player.enqueue([t])],
    [t.status === 'recorded' || t.status === 'failed' ? 'Record again' : 'Record now', async () => {
      let feedback = ''
      if (t.status === 'recorded' || t.status === 'failed') {
        const answer = await feedbackDialog(`Record “${t.title}” again`, 'Record', t.feedback || [])
        if (answer === null) return
        feedback = answer
      }
      await api.post(`/api/tracks/${t.id}/rerecord`, feedback ? { feedback } : {})
      toast(feedback ? 'Revising the song from your feedback, then recording a new take; the current one plays until it’s ready'
        : t.status === 'recorded' ? 'Recording a new take; the current one plays until it’s ready' : 'Sent to the studio')
    }],
    ['Rewrite lyrics', async () => { await api.post(`/api/tracks/${t.id}/rewrite`); toast('Rewriting lyrics…') }],
    ['Show lyrics & style', () => showTrackText(t)],
    ['Go to album', () => (location.hash = '#/album/' + t.albumId)],
    ['Go to artist', () => (location.hash = '#/artist/' + t.artistId)],
    ['Delete song…', () => deleteItem('track', t.id, `Delete “${t.title}”? Its audio file is removed from disk.`)],
  ])
}

/** Confirm, then delete (core stops any recording of it first and removes the files). */
export async function deleteItem(kind: 'track' | 'album' | 'artist', id: string, question: string, after?: () => void) {
  if (!confirm(question)) return
  const path = { track: '/api/tracks/', album: '/api/albums/', artist: '/api/artists/' }[kind] + id
  try {
    const r = await api.del(path)
    toast(kind === 'track' ? 'Song deleted' : `Deleted (${r.tracks?.length || 0} songs)`)
    after?.()
  } catch (error: any) { toast(`Couldn’t delete: ${error?.message || error}`, 'error') }
}

/** "New cover" / "New photo": regenerates with the current image settings, then shows it. */
async function newArtwork(btn: HTMLButtonElement, kind: 'cover' | 'portrait', id: string, img: () => HTMLElement | null, history: string[] = []) {
  const feedback = await feedbackDialog(kind === 'cover' ? 'New cover' : 'New photo', 'Make it', history)
  if (feedback === null) return
  try {
    const r = await withBusy(btn, () => runJob(kind === 'cover' ? `/api/albums/${id}/cover` : `/api/artists/${id}/portrait`, feedback ? { feedback } : {}), 'Making it…')
    const fresh = await api.get(kind === 'cover' ? `/api/albums/${id}` : `/api/artists/${id}`)
    const src = kind === 'cover' ? fresh.cover : fresh.portrait
    const el = img()
    if (el && src) {
      const fresh2 = cover(src, el.className.replace(' blank', ''))
      if (fresh.imageFeedback?.length) fresh2.title = feedbackTip(fresh.imageFeedback)
      el.replaceWith(fresh2)
    }
    if (r.source === 'provider') toast(kind === 'cover' ? `New cover made with ${r.model}` : `New photo made with ${r.model}`)
    else toast(`Used placeholder art: ${r.error || 'image generation is off'}. Turn on images in Settings › AI provider › Images.`, 'error')
  } catch (error: any) { toast(`Couldn’t make it: ${error?.message || error}`, 'error') }
}

async function showTrackText(t: Track) {
  const fresh = await api.get('/api/tracks/' + t.id)
  modal(fresh.title, h('div.track-text',
    h('h4', 'Style (what the music model hears)'), h('p.mono', fresh.style || '—'),
    fresh.language ? h('p.small.muted', 'Vocal language: ' + fresh.language) : null,
    h('h4', 'Lyrics'), fresh.instrumental ? h('p.muted', 'Instrumental (no vocals)') : h('pre.lyrics-pre', fresh.lyrics || 'Not written yet')), [{ label: 'Close', primary: true }])
}

// MARK: pages

export async function viewHome() {
  const d = await api.get('/api/home')
  const page = h('div.page')
  const greeting = new Date().getHours() < 12 ? 'Good morning' : new Date().getHours() < 18 ? 'Good afternoon' : 'Good evening'
  page.append(h('header.page-head', h('h1', greeting),
    h('div.head-actions',
      h('button.primary', { onclick: () => (location.hash = '#/create') }, icon('sparkle', 16), ' Discover new artists'),
      h('button', { onclick: () => (location.hash = '#/links') }, icon('link', 16), ' Make music like these links'))))
  if (!d.totals.albums) {
    page.append(h('div.empty', h('h2', 'Your library is empty'), h('p', 'Ask for new artists in Create, or paste some songs you love in From links.'),
      h('button.primary', { onclick: () => (location.hash = '#/create') }, 'Create your first artists')))
    return page
  }
  page.append(h('div.stats', `${d.totals.artists} artists · ${d.totals.albums} albums · ${d.totals.recorded}/${d.totals.tracks} songs recorded`))
  for (const s of d.shelves) page.append(shelf(s.title, s.albums.map(albumCard), s.id === 'recent' ? '#/albums' : undefined))
  if (d.artists.length) page.append(shelf('Artists', d.artists.map(artistCard), '#/artists'))
  return page
}

export async function viewArtists() {
  const list = await api.get('/api/artists')
  return h('div.page', h('header.page-head', h('h1', 'Artists'), h('span.muted', `${list.length}`)),
    list.length ? grid(list.map(artistCard)) : h('div.empty', h('p', 'No artists yet.')))
}

export async function viewAlbums() {
  const list = await api.get('/api/albums')
  return h('div.page', h('header.page-head', h('h1', 'Albums'), h('span.muted', `${list.length}`)),
    list.length ? grid(list.map(albumCard)) : h('div.empty', h('p', 'No albums yet.')))
}

export async function viewSongs() {
  const list = await api.get('/api/songs')
  const recorded = list.filter((t: Track) => t.status === 'recorded')
  return h('div.page', h('header.page-head', h('h1', 'Songs'), h('span.muted', `${recorded.length} recorded of ${list.length}`),
    h('div.head-actions', h('button', { onclick: () => player.playQueue(shuffle(recorded), 0, 'Songs (shuffled)') }, icon('shuffle', 16), ' Shuffle recorded'))),
    trackList(list, { showAlbum: true, source: 'Songs' }))
}

function shuffle<T>(a: T[]) { const b = a.slice(); for (let i = b.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [b[i], b[j]] = [b[j], b[i]] } return b }

export async function viewFavorites() {
  const f = await api.get('/api/favorites')
  const page = h('div.page', h('header.page-head', h('h1', 'Favorites'),
    h('div.head-actions', h('button', { onclick: async (e: Event) => { await withBusy(e.currentTarget as HTMLButtonElement, () => runJob('/api/taste/refresh'), 'Updating…'); toast('Taste profile updated from your favorites') } }, 'Update my taste from these'))))
  page.append(h('p.muted', 'Favorites and what you finish or skip shape the taste profile the AI uses for everything it makes next.'))
  if (f.tracks.length) page.append(h('h2', 'Songs'), trackList(f.tracks, { showAlbum: true, source: 'Favorite songs' }))
  if (f.albums.length) page.append(shelf('Albums', f.albums.map(albumCard)))
  if (f.artists.length) page.append(shelf('Artists', f.artists.map(artistCard)))
  if (!f.tracks.length && !f.albums.length && !f.artists.length) page.append(h('div.empty', h('p', 'Tap the heart on songs, albums or artists you love.')))
  return page
}

export async function viewSearch(q: string) {
  const r = await api.get('/api/search?q=' + encodeURIComponent(q))
  const page = h('div.page', h('header.page-head', h('h1', `Results for “${q}”`)))
  if (r.artists.length) page.append(shelf('Artists', r.artists.map(artistCard)))
  if (r.albums.length) page.append(shelf('Albums', r.albums.map(albumCard)))
  if (r.tracks.length) page.append(h('h2', 'Songs'), trackList(r.tracks, { showAlbum: true, source: 'Search' }))
  if (!r.artists.length && !r.albums.length && !r.tracks.length) page.append(h('div.empty', h('p', 'Nothing matched. Search covers names, titles, bios, liner notes and lyrics.')))
  return page
}

/** The listener's feedback behind a take or an image, for a tooltip. */
export function feedbackTip(items: any[]) {
  return 'Your feedback:\n' + items.map((f: any) => '• ' + (typeof f === 'string' ? f : f.text)).join('\n')
}

export async function viewArtist(id: string) {
  const a = await api.get('/api/artists/' + id)
  const recordedTop = a.topSongs
  const page = h('div.page')
  const portrait = cover(a.portrait, 'hero-portrait')
  if (a.imageFeedback?.length) portrait.title = feedbackTip(a.imageFeedback)
  page.append(h('header.artist-hero', portrait,
    h('div.hero-text', h('div.eyebrow', a.relation ? `${a.relation} pick` : 'Artist'), h('h1', a.name),
      h('div.muted', [a.origin, a.formed, (a.genres || []).join(' · ')].filter(Boolean).join(' — ')),
      h('div.head-actions',
        h('button.primary', { onclick: () => a.albumsList[0] && playAlbum(a.albumsList[0].id) }, icon('play', 14), ' Play'),
        heart('artist', a.id, a.favorite, 18),
        h('button', { onclick: async () => { await api.post('/api/create/build', { seedArtistId: a.id, count: 3 }); toast(`Finding artists like ${a.name}…`) } }, icon('sparkle', 14), ' More like this'),
        h('button', { onclick: () => newRelease(a) }, 'New release'),
        h('button', { onclick: () => shareArtist(a) }, icon('share', 14), ' Share'),
        h('button', { title: 'Make a new press photo with the current image settings',
          onclick: (e: Event) => newArtwork(e.currentTarget as HTMLButtonElement, 'portrait', a.id, () => page.querySelector('.hero-portrait'), (a.imageFeedback || []).map((f: any) => f.text)) }, 'New photo'),
        h('button.icon-btn', { title: 'More', onclick: (e: Event) => menu(e, [
          ['Export audio files…', () => exportAudio({ artistId: a.id })],
          ['Delete artist…', () => deleteItem('artist', a.id, `Delete ${a.name} with all their albums and songs? The audio files and artwork are removed from disk.`, () => { location.hash = '#/artists' })],
        ]) }, icon('more', 18))))))
  // This artist's own waiting work (photo, releases, vocal tags); album and song items show on their pages.
  page.append(queueStatus({ artistId: a.id, without: ['albumId', 'trackId'] }))
  if (a.seedArtist) page.append(h('p.small.muted', 'Made as more like ', h('a', { href: '#/artist/' + a.seedArtist.id }, a.seedArtist.name)))
  if (a.sourceSetId) page.append(h('p.small.muted', 'Made from ', h('a', { href: '#/links/' + a.sourceSetId }, 'a set of links')))
  if (recordedTop.length) page.append(h('h2', 'Songs'), trackList(recordedTop, { showAlbum: true, source: a.name }))
  page.append(shelf('Discography', a.albumsList.map(albumCard)))
  const about = h('section.about', h('h2', 'About'))
  if (a.bio) about.append(...a.bio.split(/\n+/).map((p: string) => h('p', p)))
  if (a.voice) about.append(h('h3', 'Sound & voice'), h('p', a.voice))
  if (a.members?.length) about.append(h('h3', 'Members'), h('p', a.members.map((m: any) => typeof m === 'string' ? m : `${m.name}${m.role ? ' (' + m.role + ')' : ''}`).join(', ')))
  page.append(about)
  return page
}

function newRelease(a: any) {
  const brief = h('textarea', { rows: 3, placeholder: 'Optional: what should this release be? (e.g. “a stripped-back acoustic EP”)' }) as HTMLTextAreaElement
  const kind = h('select', h('option', { value: 'album' }, 'Album'), h('option', { value: 'songs' }, 'A few songs')) as HTMLSelectElement
  modal(`New release by ${a.name}`, h('div.form', h('label', 'Kind', kind), h('label', 'Direction', brief)), [
    { label: 'Cancel' },
    { label: 'Create', primary: true, onClick: async () => { await api.post('/api/create/build', { artistId: a.id, output: kind.value, brief: brief.value || undefined }); toast('Writing a new release…') } },
  ])
}

export async function viewAlbum(id: string) {
  const a = await api.get('/api/albums/' + id)
  const total = a.tracks.reduce((s: number, t: Track) => s + (t.duration || t.targetSeconds || 0), 0)
  const unrecorded = a.tracks.filter((t: Track) => t.status !== 'recorded').length
  const page = h('div.page')
  const art = cover(a.cover, 'hero-cover')
  if (a.imageFeedback?.length) art.title = feedbackTip(a.imageFeedback)
  page.append(h('header.album-hero', art,
    h('div.hero-text', h('div.eyebrow', a.kind || 'Album'), h('h1', a.title),
      h('div', h('a', { href: '#/artist/' + a.artistId }, a.artistName), h('span.muted', ` · ${a.year || ''} · ${a.tracks.length} songs · ${Math.round(total / 60)} min`)),
      h('div.head-actions',
        h('button.primary', { onclick: () => player.playQueue(a.tracks, 0, a.title) }, icon('play', 14), ' Play'),
        heart('album', a.id, a.favorite, 18),
        unrecorded ? h('button', { onclick: async () => { await api.post('/api/studio/request', { albumId: a.id }); toast(`Recording ${unrecorded} songs ahead`) } }, icon('record', 12), ` Record all ahead (${unrecorded})`) : null,
        h('button', { onclick: () => shareArtist(a.artist, [a.id]) }, icon('share', 14), ' Share'),
        h('button', { title: 'Make a new cover with the current image settings',
          onclick: (e: Event) => newArtwork(e.currentTarget as HTMLButtonElement, 'cover', a.id, () => page.querySelector('.hero-cover'), (a.imageFeedback || []).map((f: any) => f.text)) }, 'New cover'),
        h('button.icon-btn', { title: 'More', onclick: (e: Event) => menu(e, [
          ['Export audio files…', () => exportAudio({ albumIds: [a.id] })],
          ['More like this artist', async () => { await api.post('/api/create/build', { seedArtistId: a.artistId, count: 3 }); toast('Finding similar artists…') }],
          ['Delete album…', () => deleteItem('album', a.id, `Delete “${a.title}” and its ${a.tracks.length} songs? The audio files and cover are removed from disk.`, () => { location.hash = '#/artist/' + a.artistId })],
        ]) }, icon('more', 18))))))
  // The album's own waiting work (cover); each song's place in line shows on its row.
  page.append(queueStatus({ albumId: a.id, without: ['trackId'] }))
  page.append(trackList(a.tracks, { source: a.title }))
  if (a.linerNotes || a.concept) {
    const notes = h('section.about', h('h2', 'Liner notes'))
    for (const p of (a.linerNotes || a.concept).split(/\n+/)) notes.append(h('p', p))
    page.append(notes)
  }
  return page
}

// MARK: sharing and export

async function shareArtist(artist: any, albumIds?: string[]) {
  const includeAudio = h('input', { type: 'checkbox', checked: true }) as HTMLInputElement
  const by = h('input', { type: 'text', placeholder: 'Your name (optional)' }) as HTMLInputElement
  modal(`Share ${artist.name}`, h('div.form',
    h('p.muted', albumIds ? 'Exports this album with its artist as a .glalbum file anyone with this app can open.' : 'Exports the artist with all their albums as a .glalbum file.'),
    h('label.check', includeAudio, ' Include recorded audio'), h('label', 'Shared by', by)), [
    { label: 'Cancel' },
    { label: 'Save…', primary: true, onClick: async () => {
      const path = await desktop.saveFile(`${artist.name}.glalbum`, [{ name: 'Ghost Label album', extensions: ['glalbum'] }])
      if (!path) return false
      const r = await api.post('/api/share/export', { artistId: artist.id, albumIds, includeAudio: includeAudio.checked, path, sharedBy: by.value })
      toast(`Saved ${r.albums} album(s)`); desktop.reveal(r.path)
    } },
  ])
}

async function exportAudio(body: any) {
  const folder = await desktop.chooseFolder()
  if (!folder) return
  try {
    const r = await api.post('/api/export/audio', { folder, ...body })
    toast(`Exported ${r.written} file(s)${r.skipped ? `, ${r.skipped} not recorded yet` : ''}`)
    desktop.openPath(r.folder)
  } catch (e) { toast(String((e as Error).message), 'error') }
}

export async function openShareFile(path?: string) {
  path = path || await desktop.openFile([{ name: 'Ghost Label album', extensions: ['glalbum'] }])
  if (!path) return
  try {
    const p = await api.post('/api/share/preview', { path })
    modal(`Add ${p.artist}?`, h('div.form',
      p.sharedBy ? h('p', `Shared by ${p.sharedBy}`) : null,
      p.alreadyInLibrary ? h('p.warn', 'This artist is already in your library; new albums will be added.') : null,
      h('ul', p.albums.map((a: any) => h('li', `${a.title}${a.year ? ' (' + a.year + ')' : ''} — ${a.tracks} songs, ${a.recorded} recorded`)))), [
      { label: 'Cancel' },
      { label: 'Add to library', primary: true, onClick: async () => { const r = await api.post('/api/share/import', { token: p.token }); toast(`Added ${r.albumsAdded} album(s)`); location.hash = '#/artist/' + r.artistId } },
    ])
  } catch (e) { toast(String((e as Error).message), 'error') }
}
