// Create: describe new music, discover new artists, write a custom song; plus the job list.

import { api, on } from './api'
import { clear, h, icon, toast } from './dom'
import { spinner } from './waiting'
import { queueStatus } from './queue'

function countsLine(c: any) {
  if (!c) return ''
  const parts: string[] = []
  if (c.artistsTotal) parts.push(`${c.artistsPlanned || 0} of ${c.artistsTotal} artists planned`)
  if (c.artistsTotal && (c.artistsDone || c.artistsWritten)) parts.push(`${c.artistsDone || 0} finished`)
  if (c.releasesPlanned) parts.push(`${c.albumsWritten || 0} of ${c.releasesPlanned} releases written`)
  if (c.songsPlanned) parts.push(`${c.songsPlanned} songs planned`)
  if (c.artistsFailed) parts.push(`${c.artistsFailed} failed`)
  return parts.join(' · ')
}

const openTails = new Set<string>()

// Retry an engine install: stop the current attempt (if any), wait for it to end, start again with
// the same choices. Finished downloads are reused by the installer.
export async function retryInstall(j: any) {
  if (j.status === 'running' || j.status === 'queued') {
    await api.post(`/api/jobs/${j.id}/cancel`)
    for (let i = 0; i < 60; i++) {
      const cur = (await api.get('/api/jobs')).find((x: any) => x.id === j.id)
      if (!cur || !['running', 'queued'].includes(cur.status)) break
      await new Promise((r) => setTimeout(r, 500))
    }
  }
  const d = j.data || {}
  if (d.tool === 'ffmpeg') { await api.post('/api/runtime/ensure', {}); if (j.status !== 'running') await api.del(`/api/jobs/${j.id}`).catch(() => {}); return }
  if (d.repair) { await api.post('/api/engine/repair', {}); if (j.status !== 'running') await api.del(`/api/jobs/${j.id}`).catch(() => {}); return }
  await api.post('/api/engine/install', { profile: d.profile, rocm: d.rocm, arch: d.arch, downloadModels: d.downloadModels })
  if (j.status !== 'running') await api.del(`/api/jobs/${j.id}`).catch(() => {})
}

export function jobList(filter?: (j: any) => boolean) {
  const box = h('div.jobs')
  const render = async () => {
    if (!box.isConnected && box.dataset.mounted) return off()
    box.dataset.mounted = '1'
    const jobs = (await api.get('/api/jobs')).filter(filter || (() => true)).slice(0, 12)
    clear(box)
    if (!jobs.length) { box.append(h('p.muted.small', 'Nothing running.')); return }
    for (const j of jobs) {
      const res = j.result || {}
      const links: HTMLElement[] = []
      for (const id of res.albums || []) links.push(h('a', { href: '#/album/' + id }, 'album'))
      for (const id of res.artists || []) links.push(h('a', { href: '#/artist/' + id }, 'artist'))
      if (res.albumId) links.push(h('a', { href: '#/album/' + res.albumId }, 'album'))
      if (res.artistId) links.push(h('a', { href: '#/artist/' + res.artistId }, 'artist'))
      if (res.setId) links.push(h('a', { href: '#/links/' + res.setId }, 'open'))
      const live = j.status === 'running' || j.status === 'queued'
      const isInstall = j.kind === 'install' || j.kind === 'runtime'
      const stalled = live && j.flags?.stalled
      const tail = (j.tail || []) as string[]
      const details = tail.length ? h('details.job-tail', { open: openTails.has(j.id), ontoggle: (e: Event) => {
        if ((e.currentTarget as HTMLDetailsElement).open) openTails.add(j.id); else openTails.delete(j.id) } },
        h('summary.small.muted', 'Output'), h('pre.small', tail.slice(-20).join('\n'))) : null
      box.append(h('div.job', { class: j.status },
        h('div.job-head', h('span.job-title', j.title), h('span.job-status', j.status),
          !live ? h('button.icon-btn.small', { title: 'Dismiss', onclick: async () => { await api.del(`/api/jobs/${j.id}`); render() } }, '×') : null),
        // Installs report real byte progress; AI jobs show what they're doing and their counts, not a made-up bar.
        live && isInstall ? h('div.progress', h('div', { style: { width: `${Math.round((j.progress || 0) * 100)}%` } })) : null,
        live && !isInstall ? h('div.job-msg.wait-line', spinner(), j.message || 'Working…') : h('div.job-msg', j.error || j.message || ''),
        j.flags?.counts ? h('div.small.muted', countsLine(j.flags.counts)) : null,
        live ? queueStatus({ jobId: j.id }, { max: 4 }) : null,
        stalled ? h('div.warn.small', `No progress for ${j.flags.stalled} s. The step is retried automatically after 10 minutes; Retry restarts it now and keeps finished downloads.`) : null,
        links.length ? h('div.job-links', ...links.slice(0, 8).flatMap((l, i) => (i ? [' · ', l] : [l]))) : null,
        details,
        h('div.actions',
          live ? h('button.small', { onclick: () => api.post(`/api/jobs/${j.id}/cancel`) }, 'Cancel') : null,
          isInstall && (stalled || ['failed', 'interrupted', 'cancelled'].includes(j.status))
            ? h('button.small.primary', { onclick: () => retryInstall(j) }, 'Retry')
            : j.kind === 'build' && ['failed', 'interrupted', 'cancelled'].includes(j.status)
              ? h('button.small.primary', { onclick: async () => { await api.post('/api/create/build', j.data || {}); await api.del(`/api/jobs/${j.id}`).catch(() => {}) } }, 'Retry') : null)))
    }
  }
  let timer = 0
  const off = on('job', () => { clearTimeout(timer); timer = window.setTimeout(render, 150) })
  render()
  return box
}

export async function viewCreate(tab = 'discover') {
  const page = h('div.page')
  const tabs = h('div.tabs')
  const body = h('div.tab-body')
  const show = (t: string) => {
    tabs.querySelectorAll('button').forEach((b) => b.classList.toggle('active', b.dataset.tab === t))
    clear(body).append(t === 'custom' ? customForm() : discoverForm())
  }
  for (const [id, label] of [['discover', 'New artists & albums'], ['custom', 'Write one song']]) {
    tabs.append(h('button', { 'data-tab': id, onclick: () => show(id) }, label))
  }
  page.append(h('header.page-head', h('h1', 'Create')), tabs, h('div.create-layout', body, h('aside.create-side', h('h3', 'Activity'), jobList())))
  show(tab || 'discover')
  return page
}

function discoverForm() {
  const brief = h('textarea.big', { rows: 4, placeholder: 'Describe what you want to hear — or leave it empty to discover from your taste.\n\ne.g. “Late-night Urdu ghazals with modern R&B production”, “a 90s Bristol trip-hop band that never existed”' }) as HTMLTextAreaElement
  const count = h('input', { type: 'number', min: '1', max: '50', value: '3' }) as HTMLInputElement
  const output = h('select', h('option', { value: 'artists' }, 'Artists with discographies'), h('option', { value: 'album' }, 'One album each'), h('option', { value: 'songs' }, 'A few songs each')) as HTMLSelectElement
  const songs = h('input', { type: 'number', min: '1', max: '30', placeholder: 'auto' }) as HTMLInputElement
  const go = h('button.primary.large', { onclick: async () => {
    try {
      await api.post('/api/create/build', { brief: brief.value.trim() || undefined, count: Number(count.value) || 1, output: output.value, songCount: songs.value ? Number(songs.value) : undefined })
      toast('Writing new music… you can keep listening meanwhile.')
    } catch (e) { toast(String((e as Error).message), 'error') }
  } }, icon('sparkle', 16), ' Create')
  const chips = ['Something new from my taste', 'A Hindi indie-pop duo', 'Instrumental focus music', 'A Punjabi folk-electronic project', 'Japanese city pop revival'].map((c) =>
    h('button.chip', { onclick: () => { brief.value = c === 'Something new from my taste' ? '' : c; brief.focus() } }, c))
  return h('div.form',
    h('label', 'What should it be?', brief), h('div.chips', chips),
    h('div.form-row', h('label', 'How many artists', count), h('label', 'What to make', output), h('label', 'Songs per release', songs)),
    h('p.small.muted', 'Text and artwork are written by your AI provider; songs are recorded by the local engine when you play them (or ahead of time from the Studio).'),
    go)
}

function customForm() {
  const title = h('input', { type: 'text', placeholder: 'Title (optional)' }) as HTMLInputElement
  const style = h('textarea', { rows: 3, placeholder: 'Style: genre, instruments, tempo, mood, vocal type… (e.g. “slow qawwali-inspired soul, harmonium, tabla, warm male vocal, 72 bpm”)' }) as HTMLTextAreaElement
  const lyrics = h('textarea', { rows: 8, placeholder: 'Lyrics (optional — leave empty and the AI writes them; use [Verse] [Chorus] tags if you like)' }) as HTMLTextAreaElement
  const instrumental = h('input', { type: 'checkbox' }) as HTMLInputElement
  const language = h('input', { type: 'text', placeholder: 'auto (e.g. hi, ur, en)' }) as HTMLInputElement
  const artist = h('select', h('option', { value: '' }, 'A new one-off artist')) as HTMLSelectElement
  api.get('/api/artists').then((list) => list.forEach((a: any) => artist.append(h('option', { value: a.id }, a.name))))
  return h('div.form',
    h('label', 'Title', title), h('label', 'Style', style), h('label', 'Lyrics', lyrics),
    h('div.form-row', h('label', 'Artist', artist), h('label', 'Vocal language', language), h('label.check', instrumental, ' Instrumental')),
    h('button.primary.large', { onclick: async () => {
      if (!style.value.trim() && !lyrics.value.trim()) return toast('Give it a style or some lyrics', 'error')
      try {
        await api.post('/api/create/custom', { title: title.value, style: style.value, lyrics: lyrics.value, instrumental: instrumental.checked,
          language: language.value.trim() || undefined, artistId: artist.value || undefined })
        toast('Writing your song… it will be recorded next.')
      } catch (e) { toast(String((e as Error).message), 'error') }
    } }, icon('sparkle', 16), ' Make this song'))
}
