// Settings: AI provider (any OpenAI-compatible server), engine, playback, library, links,
// the editable rules the AI follows, and data/sharing.

import { api, desktop, on, runJob } from './api'
import { clear, h, icon, toast } from './dom'
import { jobList } from './create'
import { logBox } from './studio'
import { busy, waitPanel, withBusy } from './waiting'
import { openShareFile } from './views'

export const PRESETS: { id: string; name: string; baseUrl: string; text: string; fast: string; images: boolean; imageModel?: string; imageSize?: string; unloadImages?: boolean; keyNeeded: boolean; note: string }[] = [
  { id: 'openai', name: 'OpenAI', baseUrl: 'https://api.openai.com/v1', text: 'gpt-4o', fast: 'gpt-4o-mini', images: true, imageModel: 'gpt-image-1', imageSize: '1024x1024', keyNeeded: true, note: 'Needs an API key from platform.openai.com.' },
  { id: 'openrouter', name: 'OpenRouter', baseUrl: 'https://openrouter.ai/api/v1', text: 'openai/gpt-4o', fast: 'openai/gpt-4o-mini', images: false, keyNeeded: true, note: 'One key for many models. Image generation is off by default (placeholder covers).' },
  { id: 'lemonade', name: 'Lemonade (AMD, local chat + images)', baseUrl: 'http://localhost:13305/v1', text: '', fast: '', images: true, imageModel: 'SDXL-Turbo', imageSize: '', unloadImages: false, keyNeeded: false, note: 'Install Lemonade, pull a chat model and an image model (e.g. `lemonade pull SDXL-Turbo`), then press Test connection to fill the model lists. No key unless you set LEMONADE_API_KEY.' },
  { id: 'lmstudio', name: 'LM Studio', baseUrl: 'http://localhost:1234/v1', text: '', fast: '', images: false, keyNeeded: false, note: 'Start the local server in LM Studio (Developer › Start server) and load a model. No key needed.' },
  { id: 'ollama', name: 'Ollama', baseUrl: 'http://localhost:11434/v1', text: 'qwen2.5:14b', fast: 'qwen2.5:7b', images: false, keyNeeded: false, note: 'Run `ollama serve` and `ollama pull <model>`. No key needed.' },
  { id: 'llamacpp', name: 'llama.cpp server', baseUrl: 'http://localhost:8080/v1', text: 'local', fast: 'local', images: false, keyNeeded: false, note: 'Start llama-server with a model. No key needed.' },
  { id: 'custom', name: 'Other (OpenAI-compatible)', baseUrl: '', text: '', fast: '', images: false, keyNeeded: false, note: 'Any server that speaks the OpenAI chat completions API.' },
]

function num(v: string, fallback: number) { const n = Number(v); return v.trim() !== '' && Number.isFinite(n) ? n : fallback }
function field(label: string, el: HTMLElement, help?: string) { return h('label', label, el, help ? h('span.help', help) : null) }
function input(value: any, attrs: any = {}) { return h('input', { type: 'text', value: value ?? '', ...attrs }) as HTMLInputElement }
function check(value: boolean) { return h('input', { type: 'checkbox', checked: !!value }) as HTMLInputElement }
function select(value: string, options: [string, string][]) { return h('select', options.map(([v, l]) => h('option', { value: v, selected: v === value }, l))) as HTMLSelectElement }

/** A chat-model picker filled from the server's list. The saved name always stays selectable,
 *  marked when the server doesn't have it; nothing is ever switched without the user picking. */
function modelPicker(saved: string, blankLabel?: string) {
  const sel = h('select') as HTMLSelectElement
  const other = input('', { placeholder: 'exact model name' })
  const warn = h('div.warn.small.model-warn')
  let list: string[] = [], loaded: string[] = [], listed = false, value = saved || ''
  const OTHER = '\u0000other'
  const paint = () => {
    const names = [...list]
    if (value && !names.includes(value)) names.unshift(value)
    const opts: HTMLElement[] = []
    if (blankLabel !== undefined) opts.push(h('option', { value: '' }, blankLabel))
    else if (!value) opts.push(h('option', { value: '' }, list.length ? 'Pick a model' : 'No models listed yet'))
    for (const n of names) {
      const absent = listed && !list.includes(n)
      opts.push(h('option', { value: n }, n + (loaded.includes(n) ? '  (loaded)' : '') + (absent ? "  — isn't on the server" : '')))
    }
    opts.push(h('option', { value: OTHER }, 'Other (type a name)…'))
    clear(sel).append(...opts)
    sel.value = value
    other.style.display = 'none'
    warn.textContent = listed && value && !list.includes(value)
      ? `${value} isn't on the server${list.length ? '. Pick one of its models above' : ''}; requests to it will fail until it's back.` : ''
  }
  sel.addEventListener('change', () => {
    if (sel.value === OTHER) { other.style.display = ''; other.value = value; other.focus(); return }
    value = sel.value; paint()
  })
  other.addEventListener('change', () => { value = other.value.trim(); paint() })
  paint()
  return {
    el: h('div', sel, other, warn),
    get value() { return value },
    set(v: string) { value = v || ''; paint() },
    fill(models: string[], loadedNow: string[], ok: boolean) { list = models || []; loaded = loadedNow || []; listed = ok; paint() },
  }
}

export async function providerForm(onSaved?: () => void) {
  const s = await api.get('/api/settings')
  const st = await api.get('/api/state')
  const p = s.provider
  const guess = PRESETS.find((x) => x.baseUrl && p.baseUrl.startsWith(x.baseUrl.replace('/v1', ''))) || PRESETS[PRESETS.length - 1]
  const preset = select(guess.id, PRESETS.map((x) => [x.id, x.name]))
  const note = h('p.small.muted', guess.note)
  const baseUrl = input(p.baseUrl, { placeholder: 'https://…/v1' })
  const key = h('input', { type: 'password', placeholder: st.hasApiKey ? '•••••••• (saved — type to replace)' : 'API key (leave empty for local servers)', autocomplete: 'off' }) as HTMLInputElement
  const textModel = modelPicker(p.textModel)
  // Blank Fast model = the Main model (the core falls back to it).
  const fastModel = modelPicker(p.fastModel && p.fastModel !== p.textModel ? p.fastModel : '', 'Same as main model')
  const modelsNote = h('div.small.muted')
  // Fresh every time Settings opens (and after Test connection): what the server has right now.
  const refreshModels = async () => {
    modelsNote.textContent = 'Reading the model list…'
    try {
      const r = await api.get('/api/provider/models')
      const ok = !r.error
      textModel.fill(r.models, r.loaded, ok); fastModel.fill(r.models, r.loaded, ok)
      modelsNote.textContent = ok
        ? `${r.models.length} chat model${r.models.length === 1 ? '' : 's'} on ${r.baseUrl}${r.lemonade ? ' (Lemonade: downloaded or loaded)' : ''}` + (r.loaded?.length ? ` · loaded now: ${r.loaded.join(', ')}` : '')
        : `Couldn't read the model list: ${r.error}`
    } catch (err) { modelsNote.textContent = `Couldn't read the model list: ${(err as Error).message}` }
  }
  refreshModels()
  const jsonMode = select(p.jsonMode, [['auto', 'Auto (try strict schema, then JSON mode, then plain)'], ['json_schema', 'Strict JSON schema'], ['json_object', 'JSON mode'], ['none', 'Plain text (parsed)']])
  const temp = input(p.temperature, { type: 'number', step: '0.1', min: '0', max: '2' })
  const parallel = input(p.maxParallel, { type: 'number', min: '1', max: '16', placeholder: 'auto' })
  const images = check(p.imagesEnabled)
  const imagesBase = input(p.imagesBaseUrl, { placeholder: 'empty = the chat server above' })
  const keyHint = () => st.hasImagesKey ? '•••••••• (saved — type to replace)' : 'needed when images use another server'
  const imageKey = h('input', { type: 'password', placeholder: keyHint(), autocomplete: 'off' }) as HTMLInputElement
  const imageResult = h('div.test-result')
  const imageModel = input(p.imageModel)
  const imageSize = input(p.imageSize, { placeholder: 'model’s own' })
  const imageSteps = input(p.imageSteps, { placeholder: 'model’s own', type: 'number', min: '1', max: '100' })
  const imageCfg = input(p.imageCfgScale, { placeholder: 'model’s own', type: 'number', step: '0.1', min: '0', max: '30' })
  const negative = h('textarea', { rows: 2, value: p.imageNegativePrompt || '' }) as HTMLTextAreaElement
  const imageModels = h('datalist', { id: 'image-models-list' })
  imageModel.setAttribute('list', 'image-models-list')
  const unloadImages = check(p.unloadImageModelBeforeRecording)
  const result = h('div.test-result')
  preset.addEventListener('change', () => {
    const x = PRESETS.find((y) => y.id === preset.value)!
    note.textContent = x.note
    if (x.id !== 'custom') {
      baseUrl.value = x.baseUrl; textModel.set(x.text); fastModel.set(x.fast && x.fast !== x.text ? x.fast : '')
      if (x.images && x.imageModel) {
        // A preset that makes its own images (OpenAI, Lemonade): images on, from the same server.
        images.checked = true; imagesBase.value = ''; imageModel.value = x.imageModel
        imageSize.value = x.imageSize || ''   // empty: the model's own size (Lemonade's recipe options)
        unloadImages.checked = !!x.unloadImages
      } else if (!imagesBase.value.trim()) {
        // Images on their own server (e.g. OpenAI next to LM Studio) keep their settings.
        images.checked = x.images
      }
    }
  })
  const status = h('div.save-status.small')
  // Resolves true only once the core has written settings.json (and the key), read them back, and
  // they match what was typed; otherwise shows the error and resolves false.
  const save = async (quiet = false): Promise<boolean> => {
    const sent = { baseUrl: baseUrl.value.trim(), textModel: textModel.value.trim(), fastModel: fastModel.value.trim(),
      jsonMode: jsonMode.value, temperature: num(temp.value, p.temperature), maxParallel: parallel.value.trim() === '' ? '' : num(parallel.value, 1), imagesEnabled: images.checked,
      imagesBaseUrl: imagesBase.value.trim(), imageModel: imageModel.value.trim(), imageSize: imageSize.value.trim(),
      imageSteps: imageSteps.value.trim(), imageCfgScale: imageCfg.value.trim(), imageNegativePrompt: negative.value.trim(),
      unloadImageModelBeforeRecording: unloadImages.checked }
    clear(status).append(h('span.muted', 'Saving…'))
    try {
      const r = await api.put('/api/provider', { provider: sent, ...(key.value ? { apiKey: key.value } : {}), ...(imageKey.value ? { imagesApiKey: imageKey.value } : {}) })
      const back = await api.get('/api/settings')
      const wrong = Object.entries(sent).filter(([k, v]) => back.provider?.[k] !== v).map(([k]) => k)
      if (!r?.ok || wrong.length) throw new Error(wrong.length ? `These fields didn't stick: ${wrong.join(', ')}` : 'The core did not confirm the save')
      if (key.value) { key.value = ''; key.placeholder = '•••••••• (saved — type to replace)' }
      if (imageKey.value) { imageKey.value = ''; st.hasImagesKey = true; imageKey.placeholder = keyHint() }
      clear(status).append(h('span.ok', `✓ Saved to ${r.settingsPath} · key in ${r.keyStore}`))
      for (const w of r.warnings || []) { status.append(h('div.warn', w)); toast(w, 'error') }
      if (!quiet) toast('Saved')
      onSaved?.()
      return true
    } catch (e) {
      const msg = 'Not saved: ' + ((e as Error).message || e)
      clear(status).append(h('span.warn', msg))
      toast(msg, 'error')
      return false
    }
  }
  const test = async (e?: Event) => {
    if (!(await save(true))) return
    const p = waitPanel('Testing the connection…')
    clear(result).append(p.el)
    const done = e?.currentTarget ? busy(e.currentTarget as HTMLButtonElement, 'Testing…') : () => {}
    let r: any
    try { r = await api.post('/api/provider/test') } catch (err) { clear(result).append(h('div.warn', `✗ ${(err as Error).message}`)); return } finally { p.stop(); done() }
    clear(result).append(
      h('div', { class: r.ok ? 'ok' : 'warn' }, r.ok ? `✓ Connected — ${r.chat.model} answered in ${r.chat.seconds}s (JSON: ${r.jsonMode})` : `✗ ${r.usedModel ? r.usedModel + ': ' : ''}${r.chatError || 'Failed'}`),
      ...(r.modelWarning ? [h('div.warn.small', r.modelWarning)] : []),
      r.lemonade ? h('div.small.muted', `Lemonade: ${r.chatModels.length} chat · ${r.imageModels.length} image · ${r.musicModels.length} music models (downloaded or loaded)` + (r.loadedModels?.length ? ` · loaded now: ${r.loadedModels.join(', ')}` : '')) :
      r.models ? h('div.small.muted', `${r.models.length} models available`) : h('div.small.muted', r.modelsError ? 'Model list unavailable: ' + r.modelsError : ''),
      h('div.small.muted', `Images: ${r.images} · key stored in: ${r.keyStore}`))
    if (r.imageModels?.length) clear(imageModels).append(...r.imageModels.map((m: string) => h('option', { value: m })))
    await refreshModels()   // never picks one for the user; a missing saved model stays, marked
  }
  const testImages = async (e?: Event) => {
    if (!(await save(true))) return
    const done = e?.currentTarget ? busy(e.currentTarget as HTMLButtonElement, 'Testing…') : () => {}
    try {
      const r = await api.post('/api/provider/test-images')
      if (r.imageModels?.length) clear(imageModels).append(...r.imageModels.map((m: string) => h('option', { value: m })))
      clear(imageResult).append(
        h('div', { class: r.ok ? 'ok' : 'warn' }, r.ok ? `✓ ${r.baseUrl} answered with this key${r.loaded ? ` and has ${r.model} loaded` : r.models ? ` and lists ${r.model}` : ''}` : `✗ ${r.error || 'Failed'}`),
        r.imageModels?.length ? h('div.small.muted', `Image models on this server: ${r.imageModels.join(', ')}`)
          : r.lemonade ? h('div.small.muted', 'No image models found on this server. For example: lemonade pull SDXL-Turbo') : h('span'),
        h('div.small.muted', `Server: ${r.baseUrl || '—'}${r.separate ? ' (separate from chat)' : ' (same as chat)'} · key used: ${r.key} · model: ${r.model || '—'} · no image was generated`),
        ...(r.modelDefaults && Object.keys(r.modelDefaults).length ? [h('div.small.muted',
          `${r.model} defaults on this server: ` + [r.modelDefaults.width && r.modelDefaults.height ? `${r.modelDefaults.width}×${r.modelDefaults.height}` : '',
            r.modelDefaults.steps ? `${r.modelDefaults.steps} steps` : '', r.modelDefaults.cfg_scale != null ? `cfg ${r.modelDefaults.cfg_scale}` : '']
            .filter(Boolean).join(', ') + (r.modelSizeGb ? ` · ${r.modelSizeGb} GB` : '') + ' (used when Size/Steps/CFG are empty)')] : []),
        ...(r.warning ? [h('div.warn.small', r.warning)] : []))
    } catch (err) { clear(imageResult).append(h('div.warn', `✗ ${(err as Error).message}`)) } finally { done() }
  }
  const clearImageKey = async () => {
    await api.put('/api/secret', { name: 'imagesApiKey', value: '' })
    st.hasImagesKey = false; imageKey.placeholder = keyHint(); toast('Image key removed')
  }
  const notices = [st.settingsError, st.keyStoreWarning, st.migratedFrom ? `Settings were copied from the previous version's folder (${st.migratedFrom}).` : null]
    .filter(Boolean).map((t: string) => h('p.warn.small', t))
  if (st.keyStoreNote) notices.push(h('p.small.muted', st.keyStoreNote))
  return h('div.form', ...notices,
    field('Provider', preset), note,
    field('Base URL', baseUrl), field('API key', key, `Stored in ${st.keyStore}${st.keyStoreInsecure && !/not encrypted/.test(st.keyStore) ? ' (not encrypted)' : ''}. Optional for local servers.`),
    h('div.form-row', field('Main model', textModel.el, 'artists, albums, lyrics'), field('Fast model', fastModel.el, 'questions and small tasks; blank = same as main')),
    h('div.actions', modelsNote, h('button.small', { onclick: () => refreshModels() }, 'Refresh list')),
    h('details', h('summary', 'Advanced'),
      h('div.form-row', field('JSON output', jsonMode), field('Temperature', temp), field('Parallel requests', parallel, 'background AI requests at once; empty = auto (1 for a server on this computer or network, 3 for a cloud service)'))),
    h('div.actions', h('button', { onclick: () => save() }, 'Save'), h('button.primary', { onclick: test }, 'Test connection')),
    status, result,
    h('fieldset.images',
      h('legend', 'Images (album covers and artist photos)'),
      h('p.small.muted', 'Images can use a different service from the chat AI, e.g. LM Studio for chat and OpenAI (gpt-image-1) for images: set the Images base URL and its own key. The chat key is never sent to a different images server. Without images you get generated placeholder art.'),
      h('label.check', images, ' Generate covers and photos with an images API'),
      h('div.form-row', field('Images base URL', imagesBase, 'e.g. https://api.openai.com/v1'), field('Image model', imageModel), field('Size', imageSize)),
      field('Images API key', imageKey, 'Stored like the main key.'), imageModels,
      h('div.form-row', field('Steps', imageSteps, 'Lemonade only'), field('CFG scale', imageCfg, 'Lemonade only')),
      field('Negative prompt', negative, 'Things to keep out of every image. Sent to Lemonade, which passes it on where the model uses it (turbo models at CFG 1 mostly ignore it); other servers don’t take one.'),
      h('p.small.muted', 'Lemonade tip: Z-Image-Turbo (', h('code', 'lemonade pull Z-Image-Turbo'), ', about 20.7 GB, 1024×1024, 9 steps) gives much better people and hands than SDXL-Turbo (512×512, 4 steps). Leave Size, Steps and CFG empty to use each model’s own settings. With a big image model you can turn on “Unload the image model before each recording” to free GPU memory for the music engine; artwork jobs load it again when they need it (“Loading <model>…”).'),
      h('label.check', unloadImages, ' Unload the image model before each recording (frees GPU memory for the music engine; Lemonade)'),
      h('div.actions', h('button', { onclick: testImages }, 'Test images'), h('button', { onclick: clearImageKey }, 'Remove image key')),
      imageResult))
}

export async function engineForm() {
  const [s, d, st] = await Promise.all([api.get('/api/settings'), api.get('/api/engine/detect'), api.get('/api/state')])
  const e = s.engine
  const profile = select(e.profile || d.suggested, d.profiles.map((p: string) => [p, ({ 'rocm-windows': 'AMD GPU on Windows (ROCm)', cuda: 'NVIDIA GPU (CUDA)', mps: 'Apple Silicon (MPS)', cpu: 'CPU only (slow)', lemonade: 'Lemonade Server only (small install, no local model)', mock: 'Test tones (no AI model)' } as any)[p] || p]))
  const rocm = select('10.0.0', d.rocmVersions.map((v: string) => [v, 'ROCm ' + v]))
  const arch = input('gfx1151')
  const models = check(true)
  const hfToken = h('input', { type: 'password', autocomplete: 'off', placeholder: st.hasHfToken ? '•••••••• (saved — type to replace)' : 'hf_… (optional)' }) as HTMLInputElement
  const saveHf = async () => {
    if (!hfToken.value) return true
    try {
      const r = await api.put('/api/secret', { name: 'hfToken', value: hfToken.value.trim() })
      hfToken.value = ''; hfToken.placeholder = '•••••••• (saved — type to replace)'
      if (r.warning) toast(r.warning, 'error')
      return true
    } catch (e) { toast('Hugging Face token not saved: ' + (e as Error).message, 'error'); return false }
  }
  const kind = select(e.kind, [['builtin', 'Built-in engine host'], ['custom', 'Custom engine command']])
  const backend = select(e.backend, [['acestep', 'ACE-Step 1.5 (built-in)'], ['lemonade', 'Lemonade Server (ACE-Step-Music)'], ['mock', 'Mock (test tones)']])
  // Lemonade: its URL defaults to the AI provider's base URL; the core resolves it the same way.
  const lemonUrl = input(e.lemonadeUrl, { placeholder: s.provider.baseUrl ? `${s.provider.baseUrl} (AI provider base URL)` : 'http://localhost:13305/v1' })
  const lemonModel = input(e.lemonadeModel, { placeholder: 'ACE-Step-Music' })
  const unloadChat = check(e.unloadChatModelBeforeRecording)
  const lemonResult = h('div.small')
  const testLemon = h('button', { onclick: async () => {
    testLemon.disabled = true; lemonResult.textContent = 'Checking…'
    try {
      await save(true)
      const r = await api.post('/api/engine/test-lemonade', {})
      const where = `${r.url} (${r.urlSource})`
      lemonResult.replaceChildren(r.ok
        ? h('span', `✓ Lemonade ${r.version || ''} at ${where} has ${r.model}` + (r.loaded ? ' (loaded now)' : ' (it loads on the first song)') + '.')
        : h('span.error', r.error || 'Lemonade isn’t ready.'),
        ...(!r.ok && r.running ? [h('div', 'In a terminal: ', h('code', r.pull))] : []),
        ...(r.warning ? [h('div.muted', r.warning)] : []))
    } catch (err) { lemonResult.textContent = String((err as Error).message) }
    testLemon.disabled = false
  } }, 'Test music engine') as HTMLButtonElement
  const lemonBox = h('fieldset.images',
    h('legend', 'Lemonade Server'),
    h('p.small.muted', 'Lemonade runs ACE-Step 1.5 (the XL model, about 10.5 GB) on its own. Set it up once in a terminal: ',
      h('code', 'lemonade pull ACE-Step-Music'), '. Lemonade reports no progress, so songs show a timer instead of a percentage. ',
      'It takes no reference audio: mashups and voice references are recorded by the built-in engine when it is installed ',
      st.builtinInstalled ? '(it is).' : '(it isn’t, so those songs are recorded without their reference).',
      ' Listening to linked audio also needs the built-in engine.'),
    h('div.form-row', field('Lemonade URL', lemonUrl, 'Empty: the AI provider’s base URL.'), field('Music model', lemonModel)),
    h('label.check', unloadChat, ' Unload the chat model in Lemonade before each song (frees its memory; it reloads for the next AI request)'),
    h('div.actions', testLemon), lemonResult)
  const showLemon = () => { lemonBox.style.display = backend.value === 'lemonade' && kind.value !== 'custom' ? '' : 'none' }
  backend.addEventListener('change', showLemon); kind.addEventListener('change', showLemon); showLemon()
  const python = input(e.python, { placeholder: st.enginePython ? `using ${st.enginePython}` : 'filled in by the installer' })
  const cmd = input(e.customCommand, { placeholder: 'program that speaks the engine protocol' })
  const args = input(e.customArgs)
  const maxMin = input(e.maxSongMinutes, { type: 'number', min: '0.5', max: '10', step: '0.5' })
  const idle = input(e.idleUnloadMinutes, { type: 'number', min: '0', max: '120' })
  const envText = h('textarea', { rows: 3, value: Object.entries(e.env || {}).map(([k, v]) => `${k}=${v}`).join('\n') }) as HTMLTextAreaElement
  const optsText = h('textarea', { rows: 3, value: Object.entries(e.options || {}).map(([k, v]) => `${k}=${v}`).join('\n') }) as HTMLTextAreaElement
  const parse = (t: string) => Object.fromEntries(t.split('\n').map((l) => l.trim()).filter((l) => l.includes('=')).map((l) => [l.slice(0, l.indexOf('=')).trim(), l.slice(l.indexOf('=') + 1).trim()]))
  // Send only what the user changed here. This form can be older than the last install (e.g. it
  // was open while setup ran); sending every field used to overwrite the installer's python and env.
  const lemon = () => ({ lemonadeUrl: lemonUrl.value.trim(), lemonadeModel: lemonModel.value.trim(), unloadChatModelBeforeRecording: unloadChat.checked })
  const initial = { kind: kind.value, backend: backend.value, python: python.value.trim(), customCommand: cmd.value.trim(), customArgs: args.value.trim(), ...lemon(),
    maxSongMinutes: Number(maxMin.value), idleUnloadMinutes: Number(idle.value), env: JSON.stringify(parse(envText.value)), options: JSON.stringify(parse(optsText.value)) }
  const save = async (quiet = false) => {
    await saveHf()
    const now: Record<string, any> = { kind: kind.value, backend: backend.value, python: python.value.trim(), customCommand: cmd.value.trim(), customArgs: args.value.trim(), ...lemon(),
      maxSongMinutes: Number(maxMin.value), idleUnloadMinutes: Number(idle.value), env: JSON.stringify(parse(envText.value)), options: JSON.stringify(parse(optsText.value)) }
    const patch: Record<string, any> = {}
    for (const k of Object.keys(now)) if (now[k] !== (initial as any)[k]) patch[k] = k === 'env' || k === 'options' ? JSON.parse(now[k]) : now[k]
    if (!Object.keys(patch).length) { if (!quiet) toast('Nothing changed'); return }
    await api.put('/api/settings', { engine: patch })
    Object.assign(initial, now)
    await api.post('/api/engine/restart'); toast('Saved; the engine restarts when next needed')
  }
  // One install at a time: the core attaches extra requests to the running job; the button says so.
  const idleLabel = st.engineInstalled ? 'Reinstall' : 'Install engine'
  const installBtn = h('button.primary', { onclick: async () => {
    installBtn.disabled = true
    await saveHf()
    try {
      const r = await api.post('/api/engine/install', { profile: profile.value, rocm: rocm.value, arch: arch.value, downloadModels: models.checked })
      toast(r.attached ? 'The engine is already being set up; showing its progress below.' : 'Setting up the engine…')
    } catch (e) { toast(String((e as Error).message), 'error') }
    syncInstall()
  } }, idleLabel) as HTMLButtonElement
  const syncInstall = async () => {
    const running = (await api.get('/api/jobs')).some((j: any) => j.kind === 'install' && (j.status === 'running' || j.status === 'queued'))
    installBtn.disabled = running
    installBtn.textContent = running ? 'Installing… (progress below)' : idleLabel
  }
  syncInstall()
  const offJobs = on('job', (ev: any) => { if (!installBtn.isConnected && installBtn.dataset.seen) return offJobs(); installBtn.dataset.seen = '1'; if (ev.job?.kind === 'install' || ev.job?.status === 'dismissed') syncInstall() })
  const gpuLine = d.gpus.length ? d.gpus.map((g: any) => g.name).join(', ') : 'no GPU detected'
  return h('div.form',
    h('div.notice', h('b', 'This computer: '), `${d.system} ${d.machine} · ${gpuLine}`, h('div.small', 'Suggested setup: ' + d.suggested)),
    h('h3', 'Set up the music engine'),
    h('p.small.muted', 'Downloads Python 3.12, PyTorch for your hardware, ACE-Step 1.5 (MIT) and its model weights (~10 GB), ffmpeg and yt-dlp into the app’s data folder. Takes a while; you can keep using the app. Using only AMD Lemonade Server for music? Pick “Lemonade Server only”: a small install (Python, numpy, ffmpeg, yt-dlp) that selects Lemonade.'),
    h('div.form-row', field('Hardware', profile), field('ROCm wheels', rocm), field('GPU arch', arch)),
    h('label.check', models, ' Download model weights now'),
    field('Hugging Face token', hfToken, 'Optional. Used for the model download (as HF_TOKEN) if anonymous downloads are slow or refused. Read-only tokens are enough: huggingface.co › Settings › Access Tokens.'),
    h('div.actions', installBtn, st.engineInstalled ? h('button', { onclick: async (ev: Event) => {
      const b = ev.currentTarget as HTMLButtonElement
      b.disabled = true
      try { const r = await api.post('/api/engine/repair', {}); toast(r.attached ? 'The engine is already being set up; showing its progress below.' : 'Repairing the engine: only missing pieces are redone; model weights are kept.') }
      catch (err) { toast(String((err as Error).message), 'error') }
      b.disabled = false; syncInstall()
    } }, 'Repair engine') : null),
    st.engineInstalled ? h('p.small.muted', `Engine Python: ${st.enginePython || e.python || 'not found'} · ACE-Step: ${st.engineAceRoot || 'not found'}`) : null,
    jobList((j) => j.kind === 'install'),
    h('h3', 'Engine'),
    h('div.form-row', field('Engine', kind), field('Backend', backend)),
    lemonBox,
    field('Engine Python', python), h('div.form-row', field('Custom command', cmd), field('Arguments', args)),
    h('div.form-row', field('Longest song (minutes)', maxMin), field('Unload after idle (minutes)', idle)),
    h('div.form-row', field('Backend options (key=value per line)', optsText), field('Environment (key=value per line)', envText)),
    h('div.actions', h('button', { onclick: () => save() }, 'Save engine settings'), h('button', { onclick: () => api.post('/api/engine/load') }, 'Load now')),
    h('details.log', h('summary', 'Engine log'), logBox('engine')), h('details.log', h('summary', 'App log'), logBox('core')))
}

async function playbackForm() {
  const s = await api.get('/api/settings')
  const normalize = check(s.player.normalize), autoplay = check(s.player.autoplay), synced = check(s.player.syncedLyrics)
  const look = input(s.studio.lookahead, { type: 'number', min: '0', max: '10' }), ahead = input(s.studio.lyricsAhead, { type: 'number', min: '0', max: '10' })
  const prefetch = check(s.studio.prefetch), voiceRef = check(s.studio.artistVoiceReference)
  const quality = select(String(s.engine.quality), [['8', 'Demo (fast)'], ['16', 'Balanced'], ['32', 'Studio (slow)']])
  const save = () => api.put('/api/settings', { player: { normalize: normalize.checked, autoplay: autoplay.checked, syncedLyrics: synced.checked },
    studio: { lookahead: Number(look.value), lyricsAhead: Number(ahead.value), prefetch: prefetch.checked, artistVoiceReference: voiceRef.checked },
    engine: { quality: Number(quality.value) } }).then(() => toast('Saved'))
  const f = h('div.form',
    h('label.check', normalize, ' Even out loudness between songs'), h('label.check', autoplay, ' Keep playing similar music when the queue ends'),
    h('label.check', synced, ' Follow along in lyrics'),
    h('div.form-row', field('Songs recorded ahead', look), field('Lyrics written ahead', ahead), field('Quality', quality)),
    h('label.check', prefetch, ' Record unrecorded songs while idle'),
    h('label.check', voiceRef, ' Keep each artist’s voice consistent (uses their first recording as reference audio)'))
  f.addEventListener('change', save)
  return f
}

async function libraryForm() {
  const s = await api.get('/api/settings')
  const maxAlbums = input(s.library.maxAlbumsPerArtist, { type: 'number', min: '1', max: '12' })
  const hint = input(s.library.tracksPerAlbumHint, { placeholder: 'e.g. 8–12 (a hint for the AI)' })
  const explicit = check(s.library.allowExplicit)
  const every = input(s.library.tasteRefreshEvery, { type: 'number', min: '0', max: '200' })
  const save = () => api.put('/api/settings', { library: { maxAlbumsPerArtist: Number(maxAlbums.value), tracksPerAlbumHint: hint.value, allowExplicit: explicit.checked, tasteRefreshEvery: Number(every.value) } }).then(() => toast('Saved'))
  const taste = await api.get('/api/taste')
  const prof = taste?.profile || {}
  const f = h('div.form',
    h('div.form-row', field('Albums per new artist (max)', maxAlbums), field('Songs per album', hint)),
    h('label.check', explicit, ' Allow explicit lyrics'),
    field('Update my taste profile after this many favorites/skips (0 = never)', every),
    h('h3', 'Your taste profile'),
    prof.summary ? h('div.taste', h('p.lead', prof.summary), prof.loves ? h('p', h('b', 'Loves: '), prof.loves) : null, prof.avoids ? h('p', h('b', 'Avoids: '), prof.avoids) : null,
      prof.genres?.length ? h('p.small.muted', prof.genres.join(' · ')) : null) : h('p.muted', 'Not written yet.'),
    h('div.actions', h('button', { onclick: async (e: Event) => { await withBusy(e.currentTarget as HTMLButtonElement, () => runJob('/api/taste/refresh'), 'Updating…'); toast('Taste profile updated'); location.reload() } }, 'Update from my listening'),
      h('button', { onclick: async () => { await api.put('/api/settings', { onboarded: false }); location.hash = '#/onboarding'; location.reload() } }, 'Redo onboarding')))
  f.addEventListener('change', (e) => { if ((e.target as HTMLElement).tagName !== 'BUTTON') save() })
  return f
}

async function linksForm() {
  const s = await api.get('/api/settings')
  const st = await api.get('/api/state')
  const mode = select(s.links.mode, [['metadata', 'Metadata only (default)'], ['audio', 'Listen to the audio'], ['voice', 'Audio + voice reference']])
  const src = select(s.links.audioSource, [['youtube', 'The linked audio (yt-dlp)'], ['preview', '30-second catalog previews (Deezer)']])
  const max = input(s.links.maxTracks, { type: 'number', min: '1', max: '200' }), secs = input(s.links.analysisSeconds, { type: 'number', min: '10', max: '240' })
  const keep = check(s.links.keepClips), ytdlp = input(s.links.ytdlp, { placeholder: 'auto (module or yt-dlp on PATH)' })
  const save = () => api.put('/api/settings', { links: { mode: mode.value, audioSource: src.value, maxTracks: Number(max.value), analysisSeconds: Number(secs.value), keepClips: keep.checked, ytdlp: ytdlp.value.trim() } }).then(() => toast('Saved'))
  const f = h('div.form',
    h('div.form-row', field('Default mode', mode), field('Audio from', src)),
    h('div.form-row', field('Most tracks per set', max), field('Seconds analysed per track', secs)),
    h('label.check', keep, ' Keep downloaded clips'), field('yt-dlp path', ytdlp, `Found: module ${st.ytdlp.module ? 'yes' : 'no'}, binary ${st.ytdlp.binary || 'no'}`),
    h('p.small.muted', 'Downloading audio from YouTube may be against its terms; the default reads metadata only. Audio modes are for links you have the right to download.'))
  f.addEventListener('change', save)
  return f
}

async function rulesForm() {
  const rules = await api.get('/api/rules')
  const list = h('div.rules-list')
  const editor = h('textarea.rules-editor', { rows: 24, spellcheck: false }) as HTMLTextAreaElement
  const title = h('h3')
  let current = rules[0]?.name
  const open = (r: any) => { current = r.name; title.textContent = r.name + '.md' + (r.edited ? ' (edited)' : ''); editor.value = r.text
    list.querySelectorAll('button').forEach((b) => b.classList.toggle('active', b.dataset.name === r.name)) }
  for (const r of rules) list.append(h('button', { 'data-name': r.name, onclick: () => open(r) }, r.name, r.edited ? ' •' : ''))
  if (rules[0]) open(rules[0])
  return h('div',
    h('p.muted', 'These plain-text rules are the app’s judgment: they’re sent to your AI with each request. Edit them to change how artists, albums, lyrics, covers and link briefs are written.'),
    h('div.rules', list, h('div.rules-main', title, editor, h('div.actions',
      h('button.primary', { onclick: async () => { await api.put('/api/rules/' + current, { text: editor.value }); const r = rules.find((x: any) => x.name === current); r.text = editor.value; r.edited = true; open(r); toast('Saved') } }, 'Save'),
      h('button', { onclick: async () => { const r = await api.post(`/api/rules/${current}/reset`); const i = rules.findIndex((x: any) => x.name === current); rules[i] = r; open(r); toast('Reset to the bundled version') } }, 'Reset')))))
}

async function dataForm() {
  const st = await api.get('/api/state')
  const info = await desktop.info()
  return h('div.form',
    h('p', h('b', 'Library: '), `${st.totals.artists} artists, ${st.totals.albums} albums, ${st.totals.recorded}/${st.totals.tracks} songs recorded`),
    h('p', h('b', 'Data folder: '), h('span.mono', st.home)),
    h('div.actions', h('button', { onclick: () => desktop.openPath(st.home) }, 'Open data folder'), h('button', { onclick: () => openShareFile() }, icon('share', 14), ' Open a shared .glalbum…')),
    h('p.small.muted', `App ${info.version} · ${info.platform}`))
}

const SECTIONS: [string, string, () => Promise<HTMLElement>][] = [
  ['provider', 'AI provider', () => providerForm()], ['engine', 'Music engine', engineForm], ['playback', 'Playback & studio', playbackForm],
  ['library', 'Library & taste', libraryForm], ['links', 'Links', linksForm], ['rules', 'Rules', rulesForm], ['data', 'Data & sharing', dataForm],
]

export async function viewSettings(section = 'provider') {
  const sec = SECTIONS.find((s) => s[0] === section) || SECTIONS[0]
  const body = await sec[2]()
  return h('div.page', h('header.page-head', h('h1', 'Settings')),
    h('div.settings', h('div.settings-nav', SECTIONS.map(([id, label]) => h('a', { href: '#/settings/' + id, class: id === sec[0] ? 'active' : '' }, label))),
      h('div.settings-body', h('h2', sec[1]), body)))
}

on('taste', () => toast('Your taste profile was updated from your listening'))
