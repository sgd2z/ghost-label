// First run: connect an AI provider, set up the engine, react to real artists, answer a few
// AI-chosen questions, then build the first artists.

import { api, on } from './api'
import { activeCalls, busy, onActivity, spinner, waitPanel, waitingText } from './waiting'
import { clear, h, icon, toast } from './dom'
import { engineForm, providerForm } from './settings'
import { app } from './app'

type Reaction = { name: string; reaction: 'love' | 'like' | 'no'; genre?: string; deezerId?: number; picture?: string }

const state = {
  step: 0,
  genres: [] as { id: number; name: string }[],
  reactions: new Map<string, Reaction>(),
  answers: [] as { question: string; answer: string[] }[],
  notes: '',
  explicit: false,
  question: null as any,
}

const STEPS = ['Welcome', 'AI provider', 'Music engine', 'Genres', 'Artists', 'Questions', 'Finish']

export async function viewOnboarding() {
  const page = h('div.onboarding')
  const render = async () => {
    const body = h('div.ob-body')
    clear(page).append(h('div.ob-steps', STEPS.map((s, i) => h('span', { class: i === state.step ? 'active' : i < state.step ? 'done' : '' }, s))), body)
    try { body.append(await STEP_VIEWS[state.step](go)) } catch (e) { body.append(h('p.warn', String((e as Error).message)), nav(go, { next: true })) }
  }
  const go = (d: number) => { state.step = Math.max(0, Math.min(STEPS.length - 1, state.step + d)); render() }
  await render()
  return page
}

function nav(go: (d: number) => void, o: { back?: boolean; next?: boolean; nextLabel?: string; onNext?: () => Promise<boolean | void> | boolean | void } = {}) {
  return h('div.ob-nav', o.back !== false ? h('button', { onclick: () => go(-1) }, 'Back') : h('span'),
    o.next !== false ? h('button.primary', { onclick: async () => { if ((await o.onNext?.()) !== false) go(1) } }, o.nextLabel || 'Continue') : null)
}

const STEP_VIEWS: ((go: (d: number) => void) => Promise<HTMLElement>)[] = [
  async (go) => h('div.ob-welcome',
    h('img.brand-mark.big', { src: '/icon.png', alt: '' }), h('h1', 'Ghost Label'),
    h('p.lead', 'An endless library of artists who don’t exist — with bios, discographies, liner notes, lyrics and covers — recorded on your own computer as you listen.'),
    h('p.muted', 'You’ll connect any OpenAI-compatible AI (cloud or local, like LM Studio or Ollama) for the writing, and a local music model for the recording.'),
    nav(go, { back: false, nextLabel: 'Get started' })),

  async (go) => {
    let ok = false
    const st = await api.get('/api/state')
    ok = st.providerConfigured
    const form = await providerForm(() => { ok = true })
    return h('div', h('h2', 'Connect your AI'), h('p.muted', 'Pick a provider, paste a key if it needs one, and test it. Local servers are free and private but slower.'), form,
      nav(go, { onNext: async () => {
        const s = await api.get('/api/state')
        if (!s.providerConfigured && !ok) { toast('Save a provider first (a base URL and a model).', 'error'); return false }
      } }))
  },

  async (go) => h('div', h('h2', 'Set up the music engine'),
    h('p.muted', 'Songs are recorded locally. You can install now and keep going — it downloads in the background — or skip and do it later in Settings.'),
    await engineForm(), nav(go, { nextLabel: 'Continue' })),

  async (go) => {
    const list = await api.get('/api/catalog/genres')
    const box = h('div.chips.genres')
    for (const g of list.filter((x: any) => x.id !== 0)) {
      const on = state.genres.some((x) => x.id === g.id)
      box.append(h('button.chip', { class: on ? 'on' : '', onclick: (e: Event) => {
        const el = e.currentTarget as HTMLElement
        const i = state.genres.findIndex((x) => x.id === g.id)
        if (i >= 0) state.genres.splice(i, 1); else state.genres.push({ id: g.id, name: g.name })
        el.classList.toggle('on', i < 0)
      } }, g.name))
    }
    return h('div', h('h2', 'What do you listen to?'), h('p.muted', 'Pick a few genres. Next you’ll react to real artists from them.'),
      list.length ? box : h('p.warn', 'The music catalog (Deezer) didn’t answer; you can still search for artists on the next step.'),
      nav(go))
  },

  async (go) => {
    const grid = h('div.react-grid')          // genre picks, reacted and related artists
    const results = h('div.react-grid')       // search results only (shown instead of the grid)
    const counter = h('span.muted', `${state.reactions.size} reacted`)
    // Cards are keyed by Deezer id: different artists can share a name (Deezer has several
    // "Imagine Dragons" entries), so a name can't identify a card.
    const keyOf = (a: any) => (a.id ? 'dz:' + a.id : 'name:' + a.name)
    const card = (a: any, genre?: string) => {
      const key = keyOf(a)
      const r = state.reactions.get(key)
      const el = h('div.react-card', { 'data-key': key, class: r ? r.reaction : '' },
        a.picture ? h('img', { src: a.picture, loading: 'lazy' }) : h('div.blank'), h('div.card-title', a.name),
        h('div.react-btns', (['love', 'like', 'no'] as const).map((k) => h('button', { class: r?.reaction === k ? 'on' : '', title: k, onclick: () => {
          const cur = state.reactions.get(key)
          if (cur?.reaction === k) state.reactions.delete(key)
          else state.reactions.set(key, { name: a.name, reaction: k, genre, deezerId: a.id, picture: a.picture })
          const now = state.reactions.get(key)?.reaction || ''
          document.querySelectorAll(`.react-card[data-key="${CSS.escape(key)}"]`).forEach((c) => {
            c.className = 'react-card ' + now
            c.querySelectorAll('.react-btns button').forEach((b) => b.classList.toggle('on', (b as HTMLElement).title === now))
          })
          counter.textContent = `${state.reactions.size} reacted`
          if (k === 'love' && cur?.reaction !== k && a.id) api.get('/api/catalog/related/' + a.id).then((rel) => rel.slice(0, 4).forEach((x: any) => add(x, genre)))
        } }, k === 'love' ? '♥' : k === 'like' ? '👍' : '✕'))))
      return el
    }
    const add = (a: any, genre?: string) => {
      if (grid.querySelector(`[data-key="${CSS.escape(keyOf(a))}"]`)) return
      grid.append(card(a, genre))
    }
    // Search: debounced, and only the newest request may draw (older responses are dropped).
    let seq = 0
    let timer = 0
    const runSearch = async (q: string) => {
      const mine = ++seq
      q = q.trim()
      if (q.length < 2) { results.style.display = 'none'; grid.style.display = ''; clear(results); return }
      grid.style.display = 'none'; results.style.display = ''
      clear(results).append(h('p.muted', 'Searching…'))
      try {
        const hits = await api.get('/api/catalog/search?q=' + encodeURIComponent(q))
        if (mine !== seq) return
        clear(results)
        if (!hits.length) results.append(h('p.muted', 'No artists found.'))
        for (const x of hits) results.append(card(x))
      } catch (e) {
        if (mine === seq) clear(results).append(h('p.warn', String((e as Error).message)))
      }
    }
    results.style.display = 'none'
    const search = h('input', { type: 'search', placeholder: 'Search for an artist you love…',
      oninput: (e: Event) => { clearTimeout(timer); const v = (e.target as HTMLInputElement).value; timer = window.setTimeout(() => runSearch(v), 300) },
      onkeydown: (e: KeyboardEvent) => { if (e.key === 'Enter') { clearTimeout(timer); runSearch((e.target as HTMLInputElement).value) } } })
    for (const r of state.reactions.values()) add({ name: r.name, id: r.deezerId, picture: r.picture }, r.genre)
    for (const g of state.genres.slice(0, 8)) {
      try { (await api.get(`/api/catalog/genre/${g.id}?limit=12`)).forEach((a: any) => add(a, g.name)) } catch { /* skip */ }
    }
    return h('div', h('h2', 'React to some artists'), h('p.muted', 'Love, like, or not for me. Loving one brings in related artists. React to as many as you like — 8 or more helps.'),
      h('div.form-row', search, counter), results, grid,
      nav(go, { onNext: () => { if (!state.reactions.size) { toast('React to at least one artist (or search for one).', 'error'); return false } state.question = null } }))
  },

  async (go) => {
    const wrap = h('div')
    const answered = h('span', String(state.answers.length))
    let asking = 0
    let ctrl: AbortController | null = null
    const fetchQuestion = (signal: AbortSignal) => fetch('/api/onboarding/next', { method: 'POST', signal, headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tag: 'onboarding', reactions: [...state.reactions.values()].map(({ picture, ...r }) => r), answers: state.answers, max: 4 }) })
      .then(async (r) => { const d = await r.json().catch(() => ({ error: `HTTP ${r.status}` })); if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`); return d })
    const backBtn = () => h('button', { onclick: () => { ctrl?.abort(); asking++; go(-1) } }, 'Back')
    const skipBtn = () => h('button', { onclick: () => { ctrl?.abort(); asking++; state.question = { done: true }; show(state.question) } }, 'Skip questions')

    // Ask for the next question. Without a button: a waiting panel replaces the step. With the
    // Next button: the answered question stays on screen and the button shows the wait.
    const load = async (nextBtn?: HTMLButtonElement, statusEl?: HTMLElement) => {
      const mine = ++asking
      ctrl = new AbortController()
      const started = Date.now()
      let stop: () => void
      if (nextBtn) stop = busy(nextBtn, 'Thinking of the next question…', 'onboarding', statusEl)
      else {
        const p = waitPanel('Thinking of a question…', 'onboarding')
        const slow = h('div.small.muted')
        const t = window.setTimeout(() => slow.append('Local models can take a minute or two, especially the first request while the model loads.'), 45000)
        clear(wrap).append(p.el, slow, h('div.ob-nav', backBtn(), skipBtn()))
        stop = () => { p.stop(); window.clearTimeout(t) }
      }
      try {
        const q = await fetchQuestion(ctrl.signal)
        stop()
        if (mine !== asking) return
        state.question = q
        show(q)
      } catch (e) {
        stop()
        if (mine !== asking || (e as Error).name === 'AbortError') return
        clear(wrap).append(h('p.warn', String((e as Error).message || e)),
          h('p.small.muted', `After ${Math.round((Date.now() - started) / 1000)} s. Details are in Settings › Music engine › App log.`),
          h('div.ob-nav', backBtn(), h('div', skipBtn(), ' ', h('button.primary', { onclick: () => load() }, 'Retry'))))
      }
    }

    const show = (q: any) => {
      answered.textContent = String(state.answers.length)
      clear(wrap)
      if (q.done) { wrap.append(h('p.lead', 'That’s enough to go on.'), nav(go, { onNext: () => {} })); return }
      const chosen = new Set<string>()
      const opts = h('div.chips')
      const choices: { label: string; detail?: string; either?: boolean }[] = (q.options || []).map((o: any) => (typeof o === 'string' ? { label: o } : o))
      if (q.either) choices.push({ label: q.either, either: true })
      const paint = () => opts.querySelectorAll('.chip').forEach((c) => c.classList.toggle('on', chosen.has((c as HTMLElement).dataset.label!)))
      for (const c of choices) {
        opts.append(h('button.chip', { class: c.either ? 'either' : '', 'data-label': c.label, onclick: () => {
          const was = chosen.has(c.label)
          // Single answer, or the "no preference" answer: picking it replaces the others.
          if (!q.multiple || c.either || choices.some((x) => x.either && chosen.has(x.label))) chosen.clear()
          was ? chosen.delete(c.label) : chosen.add(c.label)
          paint()
        } }, c.label, c.detail ? h('span.chip-detail', c.detail) : null))
      }
      const other = h('input', { type: 'text', placeholder: 'Or say it in your own words' }) as HTMLInputElement
      const status = h('div.small.muted.wait-status')
      const next = h('button.primary', { onclick: () => {
        const answer = [...chosen, ...(other.value.trim() ? [other.value.trim()] : [])]
        state.answers.push({ question: q.question, answer })
        answered.textContent = String(state.answers.length)
        state.question = null
        opts.querySelectorAll('button').forEach((b) => ((b as HTMLButtonElement).disabled = true))
        other.disabled = true
        load(next, status)
      } }, 'Next') as HTMLButtonElement
      wrap.append(h('h3', q.question), ...(q.multiple ? [h('p.small.muted', 'Pick any that fit.')] : []), opts, other,
        h('div.ob-nav', backBtn(), h('div', skipBtn(), ' ', next)), status)
    }

    if (state.question) show(state.question)
    else load()
    return h('div', h('h2', 'A few questions'), h('p.muted', 'Your AI picks these from your reactions (', answered, ' answered).'), wrap)
  },

  async (go) => {
    const notes = h('textarea', { rows: 3, placeholder: 'Anything else? Languages you love (Hindi, Urdu, Punjabi…), eras, moods, what to avoid…' }) as HTMLTextAreaElement
    notes.value = state.notes
    const explicit = h('input', { type: 'checkbox', checked: state.explicit }) as HTMLInputElement
    const count = h('input', { type: 'number', min: '1', max: '30', value: '4' }) as HTMLInputElement
    const form = h('div.form', h('label', 'Notes for the AI', notes), h('label.check', explicit, ' Explicit lyrics are fine'),
      h('label', 'How many artists to start with', count))
    const progress = h('div')
    const buildBtn = h('button.primary.large', { onclick: () => start() }, icon('sparkle', 16), ' Build my library') as HTMLButtonElement
    const navRow = h('div.ob-nav', h('button', { onclick: () => go(-1) }, 'Back'), buildBtn)
    let profileDone = false
    const start = () => {
      state.notes = notes.value
      state.explicit = explicit.checked
      form.style.display = 'none'
      navRow.style.display = 'none'
      runBuild(progress, { profileDone, total: Math.max(1, Math.min(30, Number(count.value) || 4)),
        writeProfile: () => api.post('/api/onboarding/finish', { reactions: [...state.reactions.values()].map(({ picture, ...r }) => r), answers: state.answers, notes: notes.value, allowExplicit: explicit.checked }),
        onProfile: () => { profileDone = true },
        onBack: () => { clear(progress); form.style.display = ''; navRow.style.display = '' } })
    }
    return h('div', h('h2', 'Almost there'), form, progress, navRow)
  },

]

// "Build my library" progress: the steps the core really runs, which one is active, and the
// counts it reports (job flags `stage` and `counts`). No percentages.
function runBuild(box: HTMLElement, o: { profileDone: boolean; total: number; writeProfile: () => Promise<any>; onProfile: () => void; onBack: () => void }) {
  const started = Date.now()
  const elapsed = h('span.muted')
  const summary = h('div')
  const actions = h('div.ob-nav')
  const steps = [
    { key: 'profile', title: 'Writing your taste profile' },
    { key: 'roster', title: `Planning ${o.total} artist${o.total === 1 ? '' : 's'}` },
    { key: 'artists', title: 'Writing each artist, their releases and tracklists' },
    { key: 'done', title: 'Ready to listen' },
  ]
  const items = steps.map((s) => ({ ...s, mark: h('span.mark', '○'), sub: h('span.sub'), li: h('li') }))
  items.forEach((i) => i.li.append(i.mark, h('div', i.title, i.sub)))
  const wait = h('div.small.muted.wait-status')
  clear(box).append(h('div.wait-line', h('strong', 'Building your library'), h('span', { style: { marginLeft: '10px' } }, elapsed)),
    h('ol.stages', items.map((i) => i.li)), wait, summary, actions)
  let stage = o.profileDone ? 'roster' : 'profile'
  let failed = false
  let finished = false
  const setStage = (key: string, isFailed = false) => {
    stage = key; failed = isFailed; failedAt = isFailed ? Date.now() : 0
    const idx = steps.findIndex((s) => s.key === key)
    items.forEach((it, i) => {
      const state = i < idx || (key === 'done' && i === idx) ? 'done' : i === idx ? (isFailed ? 'failed' : 'active') : ''
      it.li.className = state
      clear(it.mark).append(state === 'done' ? '✓' : state === 'failed' ? '✕' : state === 'active' ? spinner() : '○')
    })
  }
  const tick = window.setInterval(() => paintWait(), 1000)
  const offAct = onActivity(() => paintWait())
  let failedAt = 0
  function paintWait() {
    const s = Math.round(((failed && failedAt) || Date.now()) / 1000 - started / 1000)
    elapsed.textContent = finished ? `took ${s} s` : failed ? `stopped after ${s} s` : `${s} s`
    wait.textContent = !finished && !failed && activeCalls().length ? waitingText(activeCalls()[0].startedAt) : ''
  }
  const stopAll = () => { window.clearInterval(tick); offAct(); offJob() }
  let offJob = () => {}
  const fail = (msg: string, retry: () => void) => {
    setStage(stage, true)
    clear(summary).append(h('p.warn', msg), h('p.small.muted', 'Details are in Settings › Music engine › App log.'))
    clear(actions).append(h('button', { onclick: () => { stopAll(); o.onBack() } }, 'Back'), h('button.primary', { onclick: retry }, 'Retry'))
    paintWait()
  }

  const counts = (c: any = {}) => {
    const total = c.artistsTotal || o.total
    items[1].sub.textContent = c.artistsPlanned ? `Planned ${c.artistsPlanned} of ${total}` : ''
    const parts: string[] = []
    if (c.artistsWritten || c.artistsDone) parts.push(`${c.artistsDone || 0} of ${total} artists finished`)
    if (c.releasesPlanned) parts.push(`${c.albumsWritten || 0} of ${c.releasesPlanned} planned releases written`)
    if (c.songsPlanned) parts.push(`${c.songsPlanned} songs planned`)
    if (c.artistsFailed) parts.push(`${c.artistsFailed} failed`)
    items[2].sub.textContent = parts.join(' · ')
  }

  const build = async () => {
    setStage('roster'); clear(summary); clear(actions); paintWait()
    let job: any
    try { job = await api.post('/api/create/build', { count: o.total, output: 'artists' }) } catch (e) { return fail(String((e as Error).message), build) }
    offJob()
    const cancel = h('button', { onclick: async () => { await api.post(`/api/jobs/${job.id}/cancel`).catch(() => {}) } }, 'Cancel')
    clear(actions).append(h('span'), cancel)
    offJob = on('job', (e: any) => {
      const j = e.job
      if (!j || j.id !== job.id) return
      if (j.flags?.counts) counts(j.flags.counts)
      if (j.status === 'running' && j.flags?.stage && j.flags.stage !== 'done') setStage(j.flags.stage)
      if (j.status === 'finished') {
        finished = true; setStage('done'); stopAll(); paintWait()
        const errs = j.result?.errors || []
        clear(summary).append(...(errs.length ? [h('p.warn.small', `${errs.length} artist${errs.length === 1 ? '' : 's'} couldn't be written: ${errs[0]}`)] : []),
          h('p.small.muted', 'Songs are recorded by the music engine when you play them (or from the Studio).'))
        clear(actions).append(h('span'), h('button.primary', { onclick: () => { app.state = null; location.hash = '#/create' } }, 'Open my library'))
      } else if (j.status === 'failed') fail(j.error || 'The build failed.', build)
      else if (j.status === 'cancelled') fail('Cancelled.', build)
    })
  }

  const profile = async () => {
    setStage('profile'); clear(summary); paintWait()
    const ctrl = { cancelled: false }
    clear(actions).append(h('span'), h('button', { onclick: () => { ctrl.cancelled = true; stopAll(); o.onBack() } }, 'Cancel'))
    try {
      const p = await o.writeProfile()
      if (ctrl.cancelled) return
      o.onProfile()
      items[0].sub.textContent = p.summary || ''
      build()
    } catch (e) { if (!ctrl.cancelled) fail(String((e as Error).message), profile) }
  }
  o.profileDone ? build() : profile()
}
