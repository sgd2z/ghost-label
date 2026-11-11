// Tiny DOM helpers: h('div.class', {attrs}, children...).

type Child = Node | string | number | null | undefined | false | Child[]
export type Attrs = Record<string, any>

export function h(tag: string, attrs?: Attrs | Child, ...children: Child[]): HTMLElement {
  const [name, ...classes] = tag.split('.')
  const el = document.createElement(name || 'div')
  if (classes.length) el.className = classes.join(' ')
  if (attrs && (typeof attrs !== 'object' || attrs instanceof Node || Array.isArray(attrs))) {
    children.unshift(attrs as Child)
    attrs = undefined
  }
  for (const [k, v] of Object.entries((attrs as Attrs) || {})) {
    if (v === undefined || v === null || v === false) continue
    if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v)
    else if (k === 'class') el.className = [el.className, v].filter(Boolean).join(' ')
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v)
    else if (k === 'html') el.innerHTML = v
    else if (k in el && k !== 'list' && k !== 'form') (el as any)[k] = v
    else el.setAttribute(k, v === true ? '' : String(v))
  }
  append(el, children)
  return el
}

export function append(el: Node, children: Child[]) {
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue
    if (Array.isArray(c)) append(el, c)
    else el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)))
  }
}

export function clear(el: HTMLElement) {
  while (el.firstChild) el.removeChild(el.firstChild)
  return el
}

const PATHS: Record<string, string> = {
  home: 'M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z',
  artists: 'M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zm-8 9a8 8 0 0 1 16 0z',
  albums: 'M4 4h16v16H4zM12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zm0 3a1 1 0 1 1 0 2 1 1 0 0 1 0-2z',
  songs: 'M9 18V5l12-2v13M9 18a3 3 0 1 1-6 0 3 3 0 0 1 6 0zm12-2a3 3 0 1 1-6 0 3 3 0 0 1 6 0z',
  heart: 'M12 21s-7.5-4.6-9.3-9.2C1.4 8.4 3.6 5 7 5c2 0 3.4 1.1 5 3 1.6-1.9 3-3 5-3 3.4 0 5.6 3.4 4.3 6.8C19.5 16.4 12 21 12 21z',
  sparkle: 'M12 2l2.2 6.3L20 10l-5.8 1.7L12 18l-2.2-6.3L4 10l5.8-1.7zM19 15l1 2.5 2.5 1-2.5 1L19 22l-1-2.5-2.5-1 2.5-1z',
  link: 'M10 14a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1M14 10a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1',
  studio: 'M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3zM5 11a7 7 0 0 0 14 0M12 18v3',
  settings: 'M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z',
  play: 'M7 4v16l13-8z',
  pause: 'M6 4h4v16H6zm8 0h4v16h-4z',
  next: 'M5 4l10 8-10 8zM17 4h2v16h-2z',
  prev: 'M19 20 9 12l10-8zM5 4h2v16H5z',
  shuffle: 'M16 3h5v5M4 20 21 3M21 16v5h-5M15 15l6 6M4 4l5 5',
  repeat: 'M17 1l4 4-4 4M3 11V9a4 4 0 0 1 4-4h14M7 23l-4-4 4-4M21 13v2a4 4 0 0 1-4 4H3',
  queue: 'M3 6h12M3 12h12M3 18h8M17 14v7l5-3.5z',
  lyrics: 'M4 5h16v11H8l-4 4z',
  search: 'M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM21 21l-5-5',
  volume: 'M4 9v6h4l5 4V5L8 9zM16 8.5a5 5 0 0 1 0 7M18.5 6a8.5 8.5 0 0 1 0 12',
  share: 'M4 12v7a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-7M16 6l-4-4-4 4M12 2v13',
  more: 'M5 12h.01M12 12h.01M19 12h.01',
  x: 'M6 6l12 12M18 6 6 18',
  record: 'M12 7a5 5 0 1 0 0 10 5 5 0 0 0 0-10z',
  check: 'M5 13l4 4L19 7',
  expand: 'M4 14v6h6M20 10V4h-6M14 10l6-6M4 20l6-6',
}

export function icon(name: string, size = 18, cls = 'icon'): SVGElement {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg')
  svg.setAttribute('viewBox', '0 0 24 24')
  svg.setAttribute('width', String(size))
  svg.setAttribute('height', String(size))
  svg.setAttribute('class', cls)
  const p = document.createElementNS('http://www.w3.org/2000/svg', 'path')
  p.setAttribute('d', PATHS[name] || '')
  const filled = ['play', 'pause', 'next', 'prev', 'record'].includes(name)
  p.setAttribute('fill', filled ? 'currentColor' : 'none')
  p.setAttribute('stroke', filled ? 'none' : 'currentColor')
  p.setAttribute('stroke-width', '1.8')
  p.setAttribute('stroke-linecap', 'round')
  p.setAttribute('stroke-linejoin', 'round')
  svg.appendChild(p)
  return svg
}

export function fmtTime(s?: number | null) {
  if (!s || !isFinite(s)) return '–:––'
  const m = Math.floor(s / 60)
  return `${m}:${String(Math.floor(s % 60)).padStart(2, '0')}`
}

export function toast(text: string, kind: 'info' | 'error' = 'info') {
  let host = document.getElementById('toasts')
  if (!host) { host = h('div', { id: 'toasts' }); document.body.appendChild(host) }
  const t = h('div.toast', { class: kind }, text)
  host.appendChild(t)
  setTimeout(() => t.classList.add('out'), 4200)
  setTimeout(() => t.remove(), 4700)
}

/** "What should change? (optional)": resolves with the text ('' = none), or null when cancelled. */
export function feedbackDialog(title: string, action: string, history: string[] = []): Promise<string | null> {
  return new Promise((resolve) => {
    const text = h('textarea', { rows: 4, placeholder: 'e.g. “slower, softer drums, keep the chorus”', maxLength: 500 }) as HTMLTextAreaElement
    let done = false
    const finish = (v: string | null) => { if (!done) { done = true; resolve(v) } }
    const body = h('div.form', h('label', 'What should change? (optional)', text),
      h('p.small.muted', 'Leave it empty for a fresh take with the same settings.'),
      history.length ? h('details.small', h('summary.muted', `Earlier feedback (${history.length})`), h('ul', history.map((f) => h('li', f)))) : null)
    const close = modal(title, body, [
      { label: 'Cancel', onClick: () => finish(null) },
      { label: action, primary: true, onClick: () => finish(text.value.trim()) },
    ])
    // Closing by clicking outside counts as Cancel.
    const watch = new MutationObserver(() => { if (!body.isConnected) { watch.disconnect(); finish(null) } })
    watch.observe(document.body, { childList: true })
    text.addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { finish(text.value.trim()); close() } })
    setTimeout(() => text.focus())
  })
}

export function modal(title: string, body: Node, actions: { label: string; primary?: boolean; onClick?: () => any }[]) {
  const backdrop = h('div.modal-backdrop')
  const close = () => backdrop.remove()
  const box = h('div.modal', h('h2', title), body,
    h('div.modal-actions', actions.map((a) => h('button', { class: a.primary ? 'primary' : '', onclick: async () => { const r = await a.onClick?.(); if (r !== false) close() } }, a.label))))
  backdrop.appendChild(box)
  backdrop.addEventListener('click', (e) => { if (e.target === backdrop) close() })
  document.body.appendChild(backdrop)
  return close
}
