// The player: a queue that may contain songs that aren't recorded yet. When playback reaches
// one, the player asks the studio for it (and the next few) and starts as soon as it lands.

import { recMerge, type RecState } from './recstatus'
import { api, on } from './api'

export interface Track {
  id: string; number: number; title: string; albumId: string; albumTitle?: string; artistId: string; artistName?: string
  cover?: string | null; status: string; audio?: string | null; duration?: number | null; targetSeconds?: number | null
  loudness?: number | null; lyrics?: string | null; style?: string | null; instrumental?: boolean; favorite?: boolean; feedback?: string[]
  explicit?: boolean; language?: string | null; error?: string | null; theme?: string | null
  engine?: string | null; recordNote?: string | null
}

type Listener = () => void

class Player {
  audio = new Audio()
  ctx: AudioContext | null = null
  gain: GainNode | null = null
  analyser: AnalyserNode | null = null
  queue: Track[] = []
  index = -1
  source = ''
  waiting = false
  recording: RecState | null = null
  engine: { state?: string; error?: string | null } = {}
  settings = { normalize: true, volume: 0.9, autoplay: true, lookahead: 2 }
  listeners = new Set<Listener>()
  playedCurrent = false

  constructor() {
    this.audio.preload = 'auto'
    this.audio.addEventListener('timeupdate', () => this.emit())
    this.audio.addEventListener('play', () => { this.emit(); this.mediaState() })
    this.audio.addEventListener('pause', () => { this.emit(); this.mediaState() })
    this.audio.addEventListener('ended', () => this.ended())
    this.audio.addEventListener('loadedmetadata', () => this.emit())
    on('track', (e) => this.updateTrack(e.track))
    on('recorded', (e) => this.recorded(e.trackId))
    on('recording', (e) => {
      this.recording = recMerge(this.recording, e.trackId, e.progress || {})
      this.emit()
    })
    on('studio', (e) => {
      const cur = e.studio?.current
      this.recording = cur ? recMerge(this.recording, cur.trackId, cur) : null
      if (e.studio?.engine) this.engine = e.studio.engine
      this.emit()
    })
    on('engine', (e) => { this.engine = { state: e.state, error: e.error }; this.emit() })
    on('deleted', (e) => this.forget(new Set<string>(e.trackIds || [])))
    const ms = (navigator as any).mediaSession
    if (ms) {
      ms.setActionHandler('play', () => this.play())
      ms.setActionHandler('pause', () => this.pause())
      ms.setActionHandler('nexttrack', () => this.next())
      ms.setActionHandler('previoustrack', () => this.prev())
      ms.setActionHandler('seekto', (d: any) => this.seek(d.seekTime))
    }
    this.restore()
  }

  subscribe(fn: Listener) { this.listeners.add(fn); return () => this.listeners.delete(fn) }
  emit() { for (const fn of this.listeners) fn() }
  get current(): Track | null { return this.queue[this.index] || null }
  get playing() { return !this.audio.paused }

  setupGraph() {
    if (this.ctx) return
    try {
      this.ctx = new AudioContext()
      const src = this.ctx.createMediaElementSource(this.audio)
      this.gain = this.ctx.createGain()
      this.analyser = this.ctx.createAnalyser()
      this.analyser.fftSize = 256
      src.connect(this.gain).connect(this.analyser).connect(this.ctx.destination)
    } catch { this.ctx = null }
  }

  applyGain() {
    const t = this.current
    let g = this.settings.volume
    if (this.settings.normalize && t?.loudness != null) {
      // Bring each song to the same gated RMS loudness (measured by the engine), capped at +6 dB.
      const target = -16
      g *= Math.min(2, Math.pow(10, (target - t.loudness) / 20))
    }
    if (this.gain) this.gain.gain.value = g
    else this.audio.volume = Math.min(1, g)
  }

  playQueue(tracks: Track[], start = 0, source = '') {
    this.queue = tracks.slice()
    this.source = source
    this.index = -1
    this.go(start)
  }

  enqueue(tracks: Track[], next = false) {
    if (!this.queue.length) return this.playQueue(tracks, 0)
    if (next) this.queue.splice(this.index + 1, 0, ...tracks)
    else this.queue.push(...tracks)
    this.persist(); this.requestNeeds(); this.emit()
  }

  go(i: number) {
    if (i < 0 || i >= this.queue.length) return
    if (this.current && this.playedCurrent && this.audio.duration && this.audio.currentTime / this.audio.duration < 0.8) {
      api.post('/api/play-event', { trackId: this.current.id, kind: 'skip', position: this.audio.currentTime }).catch(() => {})
    }
    this.index = i
    this.playedCurrent = false
    this.load()
  }

  load() {
    const t = this.current
    if (!t) return
    this.requestNeeds()
    this.mediaMeta()
    // A song being recorded again still has its previous take: play that meanwhile.
    if (t.audio && (t.status === 'recorded' || t.status === 'queued' || t.status === 'recording')) {
      this.waiting = false
      this.setupGraph()
      this.audio.src = t.audio
      this.applyGain()
      this.ctx?.resume()
      this.audio.play().catch(() => {})
      this.playedCurrent = true
      api.post('/api/play-event', { trackId: t.id, kind: 'play' }).catch(() => {})
    } else {
      this.audio.pause()
      this.audio.removeAttribute('src')
      this.waiting = true
    }
    this.persist(); this.emit()
  }

  /** Deleted songs leave the queue; if the playing one went, stop and move on. */
  forget(ids: Set<string>) {
    if (!ids.size || !this.queue.some((t) => ids.has(t.id))) return
    const current = this.current
    const wasCurrent = !!current && ids.has(current.id)
    const before = this.queue.slice(0, Math.max(0, this.index)).filter((t) => !ids.has(t.id)).length
    this.queue = this.queue.filter((t) => !ids.has(t.id))
    if (wasCurrent) {
      this.audio.pause(); this.audio.removeAttribute('src')
      this.waiting = false
      this.index = -1
      if (before < this.queue.length) this.go(before)
    } else this.index = current ? this.queue.findIndex((t) => t.id === current.id) : -1
    this.persist(); this.requestNeeds(); this.emit()
  }

  /** Record a failed song again (also unblocks the studio after an engine failure). */
  retry(trackId: string) {
    api.post('/api/studio/request', { trackIds: [trackId] }).then(() => {
      this.queue = this.queue.map((q) => q.id === trackId ? { ...q, status: 'queued', error: null } as Track : q)
      if (this.current?.id === trackId) this.waiting = true
      this.requestNeeds(); this.emit()
    }).catch(() => {})
  }

  requestNeeds() {
    const ids = this.queue.slice(Math.max(0, this.index), this.index + 1 + this.settings.lookahead).map((t) => t.id)
    api.post('/api/studio/need', { trackIds: ids }).catch(() => {})
  }

  play() {
    if (!this.current) return
    if (!this.audio.src && this.current.audio) return this.load()
    this.ctx?.resume(); this.audio.play().catch(() => {})
  }
  pause() { this.audio.pause() }
  toggle() { this.playing ? this.pause() : this.play() }
  next() { if (this.index < this.queue.length - 1) this.go(this.index + 1) }
  prev() { if (this.audio.currentTime > 3) this.seek(0); else this.go(this.index - 1) }
  seek(s: number) { if (isFinite(s)) this.audio.currentTime = s }
  setVolume(v: number) { this.settings.volume = v; this.applyGain(); api.put('/api/settings', { player: { volume: v } }).catch(() => {}); this.emit() }

  ended() {
    const t = this.current
    if (t) api.post('/api/play-event', { trackId: t.id, kind: 'complete', position: this.audio.duration }).catch(() => {})
    this.playedCurrent = false
    if (this.index < this.queue.length - 1) this.go(this.index + 1)
    else if (this.settings.autoplay) this.autoplay()
    else this.emit()
  }

  async autoplay() {
    // Keep music flowing: continue with recorded songs from other albums, favorites first.
    try {
      const songs: Track[] = await api.get('/api/songs')
      const fav = await api.get('/api/favorites')
      const favAlbums = new Set((fav.albums || []).map((a: any) => a.id))
      const played = new Set(this.queue.map((t) => t.id))
      const pool = songs.filter((s) => s.status === 'recorded' && !played.has(s.id))
      pool.sort((a, b) => Number(favAlbums.has(b.albumId)) - Number(favAlbums.has(a.albumId)))
      if (pool.length) { this.queue.push(...pool.slice(0, 10)); this.go(this.index + 1) }
    } catch { /* nothing to add */ }
  }

  updateTrack(t: Track) {
    if (!t) return
    let changed = false
    this.queue = this.queue.map((q) => { if (q.id === t.id) { changed = true; return { ...q, ...t } } return q })
    if (changed) this.emit()
  }

  async recorded(trackId: string) {
    if (this.current?.id === trackId) {
      const fresh = await api.get('/api/tracks/' + trackId)
      this.updateTrack(fresh)
      if (this.waiting) this.load()
    }
  }

  mediaMeta() {
    const ms = (navigator as any).mediaSession
    const t = this.current
    if (!ms || !t || !(window as any).MediaMetadata) return
    ms.metadata = new (window as any).MediaMetadata({ title: t.title, artist: t.artistName, album: t.albumTitle,
      artwork: t.cover ? [{ src: t.cover, sizes: '512x512' }] : [] })
  }
  mediaState() {
    const ms = (navigator as any).mediaSession
    if (ms) ms.playbackState = this.playing ? 'playing' : 'paused'
  }

  persist() {
    const state = { ids: this.queue.map((t) => t.id), index: this.index, source: this.source }
    api.put('/api/kv/player', { value: state }).catch(() => {})
  }

  async restore() {
    try {
      const s = await api.get('/api/settings')
      Object.assign(this.settings, { normalize: s.player?.normalize ?? true, volume: s.player?.volume ?? 0.9,
        autoplay: s.player?.autoplay ?? true, lookahead: s.studio?.lookahead ?? 2 })
      const saved = (await api.get('/api/kv/player')).value
      if (saved?.ids?.length) {
        const tracks: Track[] = []
        for (const id of saved.ids.slice(0, 200)) { try { tracks.push(await api.get('/api/tracks/' + id)) } catch { /* gone */ } }
        this.queue = tracks
        this.index = Math.min(saved.index, tracks.length - 1)
        this.source = saved.source || ''
        const t = this.current
        if (t?.status === 'recorded' && t.audio) { this.audio.src = t.audio; this.applyGain() }
        this.emit()
      }
    } catch { /* first run */ }
  }

  level(): number {
    if (!this.analyser) return 0
    const data = new Uint8Array(this.analyser.frequencyBinCount)
    this.analyser.getByteFrequencyData(data)
    let sum = 0
    for (const v of data) sum += v
    return sum / data.length / 255
  }
}

export const player = new Player()

// Lyrics without timing data: each line gets a share of the song in proportion to its length,
// section tags and blank lines get none; instrumental sections (a tag with no lines) get a
// fixed share so solos and intros take time too. Purely mechanical.
export interface LyricLine { text: string; section?: boolean; weight: number }

export function parseLyrics(lyrics: string): LyricLine[] {
  const lines = (lyrics || '').split(/\r?\n/)
  const out: LyricLine[] = []
  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i].trim()
    if (!raw) continue
    if (/^\[.*\]$/.test(raw)) {
      const nextIsTag = i + 1 >= lines.length || /^\[.*\]$/.test((lines[i + 1] || '').trim()) || !(lines[i + 1] || '').trim()
      out.push({ text: raw.slice(1, -1), section: true, weight: nextIsTag ? 6 : 0 })
    } else {
      out.push({ text: raw, weight: Math.max(4, raw.length) })
    }
  }
  return out
}

export function activeLine(lines: LyricLine[], fraction: number): number {
  const total = lines.reduce((s, l) => s + l.weight, 0)
  if (!total) return -1
  let acc = 0
  const target = fraction * total
  for (let i = 0; i < lines.length; i++) {
    acc += lines[i].weight
    if (acc >= target && lines[i].weight > 0) return i
  }
  return lines.length - 1
}
