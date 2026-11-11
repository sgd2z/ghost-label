// From links: paste songs or a playlist plus a note; each track is analysed, the AI writes a
// style brief you can refine, and music gets made from it (artists, one album, or songs).

import { withBusy } from './waiting'
import { api, on } from './api'
import { clear, fmtTime, h, icon, toast } from './dom'
import { albumCard, artistCard, feedbackTip, shelf } from './views'
import { jobList } from './create'
import { player } from './player'
import { queueStatus } from './queue'

const MODES: Record<string, [string, string]> = {
  mashup: ['Mashup', 'Listens to each track and makes one new song blending them.'],
  metadata: ['Metadata only', 'Reads titles, channels, descriptions and catalog matches. Nothing is downloaded.'],
  audio: ['Listen to the audio', 'Downloads a short clip of each track for the local music model to analyse, then deletes it. Only use links you’re allowed to download.'],
  voice: ['Audio + voice reference', 'Also keeps the clips and feeds them to the music model as reference audio, so results lean toward those voices and sounds. For personal use with links you have rights to.'],
}

export async function viewLinks() {
  const sets = await api.get('/api/links/sets')
  const st = await api.get('/api/state')
  const s = await api.get('/api/settings')
  const urls = h('textarea.big', { rows: 6, placeholder: 'Paste YouTube links, a playlist, or any song pages — one per line.' }) as HTMLTextAreaElement
  const note = h('textarea', { rows: 3, placeholder: 'A note for the AI (optional): what you like about these, what to keep, what to change.\ne.g. “the vocals of #2 with the beats of #4, sung in Urdu”' }) as HTMLTextAreaElement
  const mode = h('select', Object.entries(MODES).filter(([k]) => k !== 'mashup').map(([k, [label]]) => h('option', { value: k, selected: s.links.mode === k }, label))) as HTMLSelectElement
  const modeHelp = h('p.small.muted', MODES[mode.value][1])
  mode.addEventListener('change', () => (modeHelp.textContent = MODES[mode.value][1]))
  // What to make: music in their style (a brief you refine), or one mashup song blending them.
  const kind = h('select', h('option', { value: 'brief' }, 'Music like these (artists, albums or songs)'),
    h('option', { value: 'mashup' }, 'A mashup of these (one new song)')) as HTMLSelectElement
  const kindHelp = h('p.small.muted')
  // Mashup of the actual songs: their real lyrics, fetched from LRCLIB or the videos' captions.
  const realLyrics = h('input', { type: 'checkbox' }) as HTMLInputElement
  const realRow = h('div.real-lyrics', h('label.check', realLyrics, ' Use the songs’ real lyrics'),
    h('p.small.muted', 'Finds each song’s lyrics (LRCLIB, an open lyrics database, or the video’s captions) and lets the AI weave their actual lines into the mashup. For personal listening; lyrics belong to their writers.'))
  const modeRow = h('div', h('div.form-row', h('label', 'How to read them', mode)), modeHelp)
  const paintKind = () => {
    const m = kind.value === 'mashup'
    modeRow.style.display = m ? 'none' : ''
    realRow.style.display = m ? '' : 'none'
    kindHelp.textContent = !m
      ? 'The AI writes a style brief you can refine, then makes artists, an album or songs from it.'
      : realLyrics.checked
        ? 'Needs two or more links. Ghost Label downloads a clip of each so the music model can hear it, finds each song’s lyrics, and the AI plans one song that weaves their real lines together; the clips guide the sound as reference audio (no audio from the originals is spliced in). You can check and correct the lyrics on the set page. Then ask for changes to get new takes. Only use links you’re allowed to download.'
        : 'Needs two or more links. Ghost Label downloads a clip of each so the music model can hear it (style, tempo, key, language), the AI plans one new song that blends them with original lyrics, and the clips guide the sound as reference audio — nothing from the originals is copied into the song. Then ask for changes (“more like #1”, “faster”) to get new takes. Only use links you’re allowed to download.'
  }
  kind.addEventListener('change', paintKind); realLyrics.addEventListener('change', paintKind); paintKind()
  const page = h('div.page',
    h('header.page-head', h('h1', 'Make music like these')),
    h('div.create-layout',
      h('div.form',
        h('label', 'Links', urls), h('label', 'Your note', note),
        h('div.form-row', h('label', 'Make', kind)), realRow, kindHelp, modeRow,
        !st.ytdlp.module && !st.ytdlp.binary ? h('p.small.warn', 'yt-dlp isn’t installed, so playlists can’t be expanded and only page titles are read. Install it in Settings › Engine.') : null,
        h('button.primary.large', { onclick: async (e: Event) => {
          try {
            const r = await withBusy(e.currentTarget as HTMLButtonElement, () => api.post('/api/links/sets', { urls: urls.value, note: note.value, mode: mode.value, kind: kind.value === 'mashup' ? 'mashup' : undefined, realLyrics: kind.value === 'mashup' && realLyrics.checked }), 'Reading the links…')
            location.hash = '#/links/' + r.setId
          } catch (e) { toast(String((e as Error).message), 'error') }
        } }, icon('link', 16), ' Analyse')),

      h('aside.create-side', h('h3', 'Activity'), jobList((j) => j.kind.startsWith('links') || (j.kind === 'build' && j.data?.sourceSetId)))),
    sets.length ? h('section', h('h2', 'Your link sets'), h('div.sets', sets.map((x: any) => h('a.set-card', { href: '#/links/' + x.id },
      h('div.card-title', x.briefTitle || x.title || 'Untitled set'), h('div.card-sub', `${x.tracks} tracks · ${x.mode} · ${x.status}`),
      x.note ? h('div.small.muted.clip', x.note) : null)))) : null)
  return page
}

export async function viewLinkSet(id: string) {
  const page = h('div.page')
  let timer = 0
  const render = async () => {
    if (!page.isConnected && page.dataset.mounted) { off1(); off2(); off3(); return }
    page.dataset.mounted = '1'
    const s = await api.get('/api/links/sets/' + id)
    clear(page).append(...setView(s, render))
  }
  const later = () => { clearTimeout(timer); timer = window.setTimeout(render, 250) }
  const off1 = on('links', (e) => { if (e.setId === id) later() })
  const off2 = on('library', later)
  const off3 = on('track', later)
  await render()
  return page
}

function setView(s: any, rerender: () => void) {
  const latest = s.briefs[0]
  const out: HTMLElement[] = []
  out.push(h('header.page-head', h('div', h('div.eyebrow', `Link set · ${MODES[s.mode]?.[0] || s.mode}`), h('h1', latest?.brief?.title || s.title || 'Analysing…')),
    h('div.head-actions', h('button', { onclick: async () => { if (confirm('Delete this link set? Music made from it stays.')) { await api.del('/api/links/sets/' + s.id); location.hash = '#/links' } } }, 'Delete'))))
  if (s.note) out.push(h('blockquote.note', s.note))
  if (s.status === 'analysing') out.push(h('div.notice', icon('record', 10, 'icon rec'), ' Reading and analysing the tracks…'), jobList((j) => j.data?.setId === s.id))

  // Tracks
  out.push(h('h2', `Source tracks (${s.tracks.length})`))
  out.push(h('div.source-tracks', s.tracks.map((t: any) => {
    const cand = (t.catalog || [])[0]
    const model = t.analysis?.model
    const toggle = h('input', { type: 'checkbox', checked: !!t.included, title: 'Use this track in the brief', onchange: async (e: Event) => {
      await api.post(`/api/links/tracks/${t.id}/include`, { included: (e.target as HTMLInputElement).checked }) } })
    return h('div.source-track', { class: t.included ? '' : 'excluded' },
      h('div.st-num', String(t.position)), t.thumbnail ? h('img.st-thumb', { src: t.thumbnail, loading: 'lazy' }) : h('div.st-thumb.blank'),
      h('div.st-main',
        h('div.track-title', h('a', { href: t.url, target: '_blank', rel: 'noreferrer' }, t.title || t.url)),
        h('div.track-sub', [t.channel, t.duration ? fmtTime(t.duration) : '', t.resolver].filter(Boolean).join(' · ')),
        cand ? h('div.small', 'Catalog match: ', h('b', `${cand.artist} — ${cand.title}`), cand.album ? ` (${cand.album})` : '') : null,
        model ? h('div.small.analysis', [model.caption, model.bpm ? `${model.bpm} bpm` : '', model.keyscale, model.language].filter(Boolean).join(' · ')) : null,
        t.analysis?.error || t.analysis?.modelError ? h('div.small.warn', t.analysis.error || t.analysis.modelError) : null,
        t.error ? h('div.small.warn', t.error) : null,
        s.options?.realLyrics ? sourceLyrics(t) : null),
      h('span.pill', t.status), h('label.check', toggle))
  })))

  if (s.status === 'error') out.push(h('div.notice.warn', 'This set stopped with an error — see Activity below.'), jobList((j) => j.data?.setId === s.id))
  if (!latest) return out
  if (s.mode === 'mashup') return out.concat(mashupView(s, latest))

  // Brief
  const b = latest.brief
  const brief = h('section.brief',
    h('div.brief-head', h('h2', `Style brief v${latest.version}`), latest.instruction ? h('span.muted.small', `“${latest.instruction}”`) : null),
    b.summary ? h('p.lead', b.summary) : null,
    h('h4', 'What they share'), h('p', b.shared),
    b.interpretation ? [h('h4', 'Reading your note'), h('p', b.interpretation)] : null,
    b.language_guidance ? [h('h4', 'Language & vocals'), h('p', b.language_guidance)] : null,
    b.per_track?.length ? [h('h4', 'What each track brings'), h('ul', b.per_track.map((p: any) => h('li', h('b', `#${p.number} `), p.contributes)))] : null,
    h('h4', 'The plan'), h('div.plan', (b.plan || []).map((p: any) => h('div.plan-item', h('span.pill', p.kind),
      h('div', p.idea ? h('b', p.idea + ' — ') : null, p.description, p.draws_on?.length ? h('span.muted.small', ` (from #${p.draws_on.join(', #')})`) : null,
        p.blend ? h('div.small.muted', 'Blend: ' + p.blend) : null)))))
  out.push(brief)

  const instruction = h('input', { type: 'text', placeholder: 'Refine: “more like #3”, “drop the trap drums”, “make it a Hindi/Urdu mashup of #1 and #5”…' }) as HTMLInputElement
  const refine = async () => {
    if (!instruction.value.trim()) return
    await api.post(`/api/links/sets/${s.id}/refine`, { instruction: instruction.value.trim() })
    toast('Refining the brief…'); instruction.value = ''
  }
  instruction.addEventListener('keydown', (e) => { if (e.key === 'Enter') refine() })
  out.push(h('div.refine', instruction, h('button', { onclick: refine }, 'Refine')))

  const outSel = h('select', h('option', { value: 'artists' }, 'Artists (as planned)'), h('option', { value: 'album' }, 'One album'), h('option', { value: 'songs' }, 'Just songs')) as HTMLSelectElement
  const count = h('input', { type: 'number', min: '1', max: '20', placeholder: 'auto' }) as HTMLInputElement
  const songs = h('input', { type: 'number', min: '1', max: '30', placeholder: 'auto' }) as HTMLInputElement
  out.push(h('div.generate', h('div.form-row', h('label', 'Make', outSel), h('label', 'How many', count), h('label', 'Songs each', songs)),
    h('button.primary.large', { onclick: async () => {
      try {
        await api.post(`/api/links/briefs/${latest.id}/generate`, { output: outSel.value, count: count.value ? Number(count.value) : undefined, songCount: songs.value ? Number(songs.value) : undefined })
        toast('Making music from this brief…'); setTimeout(rerender, 500)
      } catch (e) { toast(String((e as Error).message), 'error') }
    } }, icon('sparkle', 16), ` Make it from v${latest.version}`)))
  out.push(jobList((j) => j.data?.sourceSetId === s.id || (j.kind === 'links-refine' && j.data?.setId === s.id)))

  if (s.briefs.length > 1) out.push(h('details.history', h('summary', `Earlier versions (${s.briefs.length - 1})`),
    s.briefs.slice(1).map((v: any) => h('div.history-item', h('b', `v${v.version}: ${v.brief.title}`), v.instruction ? h('span.muted', ` — “${v.instruction}”`) : null, h('p.small', v.brief.summary || v.brief.shared)))))
  if (s.artists.length) out.push(shelf('Artists made from this set', s.artists.map(artistCard)))
  if (s.albums.length) out.push(shelf('Releases made from this set', s.albums.map(albumCard)))
  return out
}

const LYRIC_SOURCES: Record<string, string> = { lrclib: 'LRCLIB', 'youtube-captions': 'the video’s captions', edited: 'your text' }

/** A source song's real lyrics (mashups that use them): where they came from, editable, Fetch again. */
// The page re-renders on every set update: unsaved edits and open panels survive it.
const lyricDrafts = new Map<string, string>()
const lyricOpen = new Set<string>()

function sourceLyrics(t: any) {
  const l = t.lyrics || { status: 'pending' }
  const text = h('textarea.lyrics-edit', { rows: 10, oninput: () => lyricDrafts.set(t.id, text.value) }) as HTMLTextAreaElement
  text.value = lyricDrafts.get(t.id) ?? (l.text || '')
  const fetchAgain = h('button.small', { type: 'button', onclick: async (e: Event) => {
    lyricDrafts.delete(t.id)
    try { await withBusy(e.currentTarget as HTMLButtonElement, () => api.post(`/api/links/tracks/${t.id}/lyrics/fetch`), 'Fetching…') }
    catch (err) { toast(String((err as Error).message), 'error') }
  } }, 'Fetch again')
  const save = h('button.small.primary', { type: 'button', onclick: async (e: Event) => {
    try { await withBusy(e.currentTarget as HTMLButtonElement, () => api.put(`/api/links/tracks/${t.id}/lyrics`, { text: text.value }), 'Saving…'); lyricDrafts.delete(t.id); toast('Lyrics saved; new takes use them.') }
    catch (err) { toast(String((err as Error).message), 'error') }
  } }, 'Save')
  const where = l.source ? `from ${LYRIC_SOURCES[l.source] || l.source}${l.synced ? ' (synced)' : ''}${l.language ? ` · ${l.language}` : ''}${l.edited ? ' · edited' : ''}` : ''
  const summary = l.status === 'found' ? `Lyrics ${where}`
    : l.status === 'fetching' || l.status === 'pending' ? 'Lyrics: looking them up…'
    : l.status === 'instrumental' ? 'Lyrics: instrumental (no words)'
    : l.status === 'error' ? `Lyrics: couldn’t look them up (${l.error || 'error'})`
    : 'Lyrics: none found — paste them here if you have them'
  return h('details.source-lyrics', { open: lyricOpen.has(t.id) || lyricDrafts.has(t.id),
    ontoggle: (e: Event) => { if ((e.currentTarget as HTMLDetailsElement).open) lyricOpen.add(t.id); else lyricOpen.delete(t.id) } },
    h('summary.small', { class: l.status === 'found' ? '' : 'muted' }, summary),
    l.detail?.trackName ? h('div.small.muted', `Matched “${l.detail.trackName}” by ${l.detail.artistName}${l.detail.albumName ? ` (${l.detail.albumName})` : ''}`) : null,
    text, h('div.actions', save, fetchAgain))
}

const pct = (x: number) => `${Math.round((x || 0) * 100)}%`

function mashupView(s: any, latest: any): HTMLElement[] {
  const out: HTMLElement[] = []
  const p = latest.brief
  const heardCount = s.tracks.filter((t: any) => t.analysis?.model).length
  if (heardCount < s.tracks.length) out.push(h('p.small.warn', `The music model heard ${heardCount} of ${s.tracks.length} tracks; the rest were planned from their titles and pages only (see the errors above).`))
  out.push(h('section.brief',
    h('div.brief-head', h('h2', `Mashup plan v${latest.version}`), latest.instruction ? h('span.muted.small', `“${latest.instruction}”`) : null),
    p.summary ? h('p.lead', p.summary) : null,
    p.heard ? h('p.small.muted', p.heard) : null,
    p.per_track?.length ? [h('h4', 'What each track brings'), h('ul', p.per_track.map((x: any) => h('li', h('b', `#${x.number} `), x.contributes)))] : null,
    p.artist?.name ? h('p.small', 'Performed by ', h('b', p.artist.name), p.artist.vocal_tags ? ` — ${p.artist.vocal_tags}` : '') : null,
    (p.songs || []).map((song: any) => h('div.mashup-song',
      h('h3', song.title),
      h('div.small', [song.bpm ? `${song.bpm} bpm` : '', song.key, song.language, song.seconds ? `${Math.round(song.seconds / 60 * 10) / 10} min` : ''].filter(Boolean).join(' · ')),
      h('p.mono.small', song.caption),
      song.structure ? h('p.small', h('b', 'Structure: '), song.structure) : null,
      song.sources?.length ? h('div.shares', song.sources.map((x: any) => h('span.pill', { title: x.takes || '' }, `#${x.number} ${pct(x.share)}`))) : null,
      song.vocal_tags ? h('p.small.muted', 'Vocals: ' + song.vocal_tags) : null,
      song.lyrics_credits?.length ? h('div.small.credits', h('b', 'Lines from: '), song.lyrics_credits.map((c: any, i: number) =>
        h('span', i ? ' · ' : '', `${c.section} ← #${(c.sources || []).join(', #')}`, c.note ? h('span.muted', ` (${c.note})`) : null))) : null,
      song.instrumental || /^\s*instrumental\s*$/i.test(song.vocal_tags || '') || /^\s*\[instrumental\]\s*$/i.test(song.lyrics || '')
        ? h('p.small.muted', 'Instrumental (no vocals)')
        : h('details', h('summary', 'Lyrics'), h('pre.lyrics-pre', song.lyrics || ''))))))

  const instruction = h('input', { type: 'text', placeholder: 'Change it: “more like #1”, “faster”, “female vocals”, “less trap, more strings”…' }) as HTMLInputElement
  const again = async () => {
    if (!instruction.value.trim()) return
    try {
      await api.post(`/api/links/sets/${s.id}/refine`, { instruction: instruction.value.trim() })
      toast('Planning a new take…'); instruction.value = ''
    } catch (e) { toast(String((e as Error).message), 'error') }
  }
  instruction.addEventListener('keydown', (e) => { if (e.key === 'Enter') again() })
  out.push(h('div.refine', instruction, h('button.primary', { onclick: again }, icon('sparkle', 14), ' New take')))
  out.push(jobList((j) => j.data?.setId === s.id))

  const takes = s.takes || []
  if (takes.length) {
    out.push(h('h2', `Takes (${takes.length})`))
    out.push(h('div.takes', takes.map((t: any, i: number) => h('div.take',
      h('button.icon-btn', { title: 'Play', disabled: t.status !== 'recorded', onclick: () => player.playQueue(takes.filter((x: any) => x.status === 'recorded'), Math.max(0, takes.filter((x: any) => x.status === 'recorded').indexOf(t)), 'Mashup takes') }, icon('play', 14)),
      h('div.take-main', h('div.track-title', { title: t.feedback?.length ? feedbackTip(t.feedback) : '' }, `${t.title} `, h('span.muted.small', `v${t.mashup?.version || '?'}`)),
        h('div.small.muted', t.mashup?.instruction ? `“${t.mashup.instruction}”` : (i === takes.length - 1 ? 'first take' : '')),
        t.recordNote ? h('div.small.muted', t.recordNote) : null,
        queueStatus({ trackId: t.id }, { labels: false, max: 2, compact: true })),
      h('span.pill', t.status === 'failed' && t.error ? `failed: ${t.error.slice(0, 60)}` : t.status)))))
  }
  if (s.albums.length) out.push(shelf('The mashup single', s.albums.map(albumCard)))
  if (s.briefs.length > 1) out.push(h('details.history', h('summary', `Earlier plans (${s.briefs.length - 1})`),
    s.briefs.slice(1).map((v: any) => h('div.history-item', h('b', `v${v.version}: ${v.brief.title}`), v.instruction ? h('span.muted', ` — “${v.instruction}”`) : null, h('p.small', v.brief.summary || '')))))
  return out
}
