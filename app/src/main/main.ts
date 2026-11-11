// Electron main process: finds or prepares a Python for the core sidecar, starts it, and shows
// the UI it serves on 127.0.0.1 (per-launch token). Also owns native bits: dialogs, file
// associations (.glalbum), single instance, opening links.

import { app, BrowserWindow, dialog, ipcMain, shell, Menu } from 'electron'
import { spawn, spawnSync, ChildProcess } from 'node:child_process'
import { existsSync, mkdirSync, appendFileSync, writeFileSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { StringDecoder } from 'node:string_decoder'

const isWin = process.platform === 'win32'
let core: ChildProcess | null = null
let mainWindow: BrowserWindow | null = null
let splash: BrowserWindow | null = null
let coreUrl = ''
const pendingOpen: string[] = []

function payloadDir(): string {
  // Packaged: resources/payload/{core,engine,bin}. Dev: the repo.
  const packaged = join(process.resourcesPath || '', 'payload')
  if (app.isPackaged && existsSync(packaged)) return packaged
  return join(__dirname, '..', '..', '..')
}

function corePaths() {
  const root = payloadDir()
  const packaged = app.isPackaged && existsSync(join(root, 'core'))
  return {
    coreDir: join(root, 'core'),
    engineDir: join(root, 'engine'),
    // The core (Python) serves the UI, so it must be real files, not inside app.asar.
    uiDir: packaged ? join(root, 'ui') : join(__dirname, '..', 'renderer'),
    uv: findUv(packaged ? join(root, 'bin') : join(root, 'app', 'resources', 'bin')),
  }
}

function findUv(binDir: string): string | null {
  const name = isWin ? 'uv.exe' : 'uv'
  const local = join(binDir, name)
  if (existsSync(local)) return local
  const which = spawnSync(isWin ? 'where' : 'which', ['uv'], { encoding: 'utf8' })
  const found = which.status === 0 ? which.stdout.split(/\r?\n/)[0].trim() : ''
  return found || null
}

function logLine(text: string) {
  try {
    const dir = join(app.getPath('userData'), 'logs')
    mkdirSync(dir, { recursive: true })
    appendFileSync(join(dir, 'main.log'), `${new Date().toISOString()} ${text}\n`)
  } catch { /* ignore */ }
  console.log(text)
}

function showSplash(message: string) {
  const html = `<html><body style="margin:0;background:#141416;color:#eee;font:14px system-ui;display:flex;flex-direction:column;align-items:center;justify-content:center;height:100vh">
    <div style="font-size:22px;font-weight:700;margin-bottom:10px">Ghost Label</div><div id="m" style="opacity:.7">${message}</div></body></html>`
  if (!splash) {
    splash = new BrowserWindow({ width: 420, height: 220, frame: false, resizable: false, backgroundColor: '#141416', show: true })
  }
  splash.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(html))
}

function venvPython(venv: string) {
  return isWin ? join(venv, 'Scripts', 'python.exe') : join(venv, 'bin', 'python')
}

function run(cmd: string, args: string[]): boolean {
  logLine(`$ ${cmd} ${args.join(' ')}`)
  const r = spawnSync(cmd, args, { encoding: 'utf8', windowsHide: true })
  if (r.stdout) logLine(r.stdout.slice(-2000))
  if (r.stderr) logLine(r.stderr.slice(-2000))
  return r.status === 0
}

// The core's runtime extras (core/requirements.txt: keyring for the OS keychain, certifi) are
// checked by importing them at every start and installed when missing, so a venv that lost them,
// or a system Python, gets them too. The core still runs without them.
function ensureExtras(py: string, uv: string | null, coreDir: string, system: boolean) {
  const ok = spawnSync(py, ['-c', 'import keyring, certifi'], { windowsHide: true }).status === 0
  if (ok) return
  const req = join(coreDir, 'requirements.txt')
  const pkgs = existsSync(req) ? ['-r', req] : ['keyring', 'certifi']
  if (uv) run(uv, ['pip', 'install', '--python', py, ...(system ? ['--system'] : []), ...pkgs])
  else run(py, ['-m', 'pip', 'install', ...(system ? ['--user'] : []), ...pkgs])
}

function corePython(uv: string | null, coreDir: string): string {
  if (process.env.SM_CORE_PYTHON) return process.env.SM_CORE_PYTHON
  const venv = join(app.getPath('userData'), 'core-venv')
  const py = venvPython(venv)
  if (existsSync(py)) { ensureExtras(py, uv, coreDir, false); return py }
  if (uv) {
    showSplash('Setting up for the first time (Python 3.12)…')
    if (run(uv, ['venv', '--python', '3.12', venv]) && existsSync(py)) {
      ensureExtras(py, uv, coreDir, false)
      return py
    }
  }
  for (const candidate of isWin ? [['py', '-3.12'], ['py', '-3'], ['python']] : [['python3.12'], ['python3']]) {
    const r = spawnSync(candidate[0], [...candidate.slice(1), '-c', 'import sys; print(sys.executable)'], { encoding: 'utf8', windowsHide: true })
    if (r.status === 0 && r.stdout.trim()) { const found = r.stdout.trim(); ensureExtras(found, null, coreDir, true); return found }
  }
  throw new Error('No Python found. Install Python 3.12 (python.org) or keep the bundled uv next to the app.')
}

function startCore(): Promise<string> {
  // Development: attach to a core that is already running (e.g. core/tests/demo.py).
  if (process.env.SM_CORE_URL) return Promise.resolve(process.env.SM_CORE_URL)
  const { coreDir, engineDir, uiDir, uv } = corePaths()
  const python = corePython(uv, coreDir)
  const home = process.env.SM_HOME || undefined
  const args = ['-m', 'smcore', '--port', '0', '--ui', uiDir, '--engine-dir', engineDir]
  if (uv) args.push('--uv', uv)
  if (home) args.push('--home', home)
  logLine(`starting core: ${python} ${args.join(' ')} (cwd ${coreDir})`)
  return new Promise((resolve, reject) => {
    core = spawn(python, args, { cwd: coreDir, env: { ...process.env, PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' }, windowsHide: true })
    let buffer = ''
    // UTF-8 that may be split across chunks (Hindi/Urdu lyrics in log lines): decode statefully.
    const outDecoder = new StringDecoder('utf8')
    const errDecoder = new StringDecoder('utf8')
    let errLine = ''
    const timer = setTimeout(() => reject(new Error('The core did not start in 60 s. See logs/main.log.')), 60000)
    core.stdout?.on('data', (chunk: Buffer) => {
      buffer += outDecoder.write(chunk)
      const m = buffer.match(/SMCORE READY (\{.*\})/)
      if (m) {
        clearTimeout(timer)
        const info = JSON.parse(m[1])
        resolve(`http://127.0.0.1:${info.port}/?token=${encodeURIComponent(info.token)}`)
      }
    })
    core.stderr?.on('data', (chunk: Buffer) => {
      errLine += errDecoder.write(chunk)
      const lines = errLine.split(/\r?\n/)
      errLine = lines.pop() || ''
      for (const line of lines) if (line.trim()) logLine('[core] ' + line)
    })
    core.on('exit', (code) => {
      logLine(`core exited ${code}`)
      core = null
      if (!app.isReady() || !mainWindow) reject(new Error(`The core exited (${code}). See logs/main.log.`))
    })
  })
}

function createWindow(url: string) {
  mainWindow = new BrowserWindow({
    width: 1320, height: 860, minWidth: 980, minHeight: 640, backgroundColor: '#141416',
    title: 'Ghost Label', show: false, icon: join(__dirname, '..', 'renderer', 'icon.png'),
    titleBarStyle: process.platform === 'darwin' ? 'hiddenInset' : 'default',
    webPreferences: { preload: join(__dirname, 'preload.js'), contextIsolation: true, sandbox: false },
  })
  mainWindow.setMenuBarVisibility(false)
  mainWindow.webContents.setWindowOpenHandler(({ url: target }) => {
    if (/^https?:/.test(target)) shell.openExternal(target)
    return { action: 'deny' }
  })
  mainWindow.once('ready-to-show', () => {
    splash?.close(); splash = null
    mainWindow?.show()
    for (const p of pendingOpen.splice(0)) mainWindow?.webContents.send('open-share', p)
  })
  mainWindow.loadURL(url)
  if (process.env.SM_SCREENSHOTS) captureScreens(process.env.SM_SCREENSHOTS)
}

// Test aid: SM_SCREENSHOTS=dir SM_SCREEN_ROUTES="home=#/home ;; album=#/album/x|<js>|<js>" saves one PNG per
// route (after running each optional snippet), then quits.
async function captureScreens(dir: string) {
  const { mkdirSync, writeFileSync } = await import('node:fs')
  mkdirSync(dir, { recursive: true })
  const routes = (process.env.SM_SCREEN_ROUTES || 'home=#/home').split(';;').map((r) => r.trim()).filter(Boolean)
    .map((r) => [r.slice(0, r.indexOf('=')), r.slice(r.indexOf('=') + 1)] as [string, string])
  const wc = mainWindow!.webContents
  await new Promise<void>((r) => wc.once('did-finish-load', () => r()))
  for (const [name, hash] of routes) try {
    const [route, ...actions] = hash.split('|')
    await wc.executeJavaScript(`location.hash = ${JSON.stringify(route)}`)
    await new Promise((r) => setTimeout(r, Number(process.env.SM_SCREEN_WAIT || 2500)))
    for (const js of actions) { await wc.executeJavaScript(js).catch((e) => logLine('screen js: ' + e)); await new Promise((r) => setTimeout(r, 1800)) }
    const img = await wc.capturePage()
    writeFileSync(join(dir, name + '.png'), img.toPNG())
    logLine('screenshot ' + name)
  } catch (e) { logLine(`screenshot ${name} failed: ${e}`) }
  app.quit()
}

ipcMain.handle('open-file', async (_e, filters) => {
  const r = await dialog.showOpenDialog(mainWindow!, { properties: ['openFile'], filters: filters || [{ name: 'Ghost Label album', extensions: ['glalbum'] }] })
  return r.canceled ? null : r.filePaths[0]
})
ipcMain.handle('save-file', async (_e, name: string) => {
  const r = await dialog.showSaveDialog(mainWindow!, { defaultPath: name, filters: [{ name: 'Ghost Label album', extensions: ['glalbum'] }] })
  return r.canceled ? null : r.filePath
})
ipcMain.handle('choose-folder', async () => {
  const r = await dialog.showOpenDialog(mainWindow!, { properties: ['openDirectory', 'createDirectory'] })
  return r.canceled ? null : r.filePaths[0]
})
ipcMain.handle('reveal', (_e, path: string) => shell.showItemInFolder(path))
ipcMain.handle('open-path', (_e, path: string) => shell.openPath(path))
ipcMain.handle('info', () => ({ version: app.getVersion(), platform: process.platform, userData: app.getPath('userData') }))

function handleArgv(argv: string[]) {
  const file = argv.find((a) => a.toLowerCase().endsWith('.glalbum'))
  if (!file) return
  if (mainWindow) mainWindow.webContents.send('open-share', file)
  else pendingOpen.push(file)
}

if (!app.requestSingleInstanceLock()) {
  app.quit()
} else {
  app.on('second-instance', (_e, argv) => {
    handleArgv(argv)
    if (mainWindow) { if (mainWindow.isMinimized()) mainWindow.restore(); mainWindow.focus() }
  })
  app.on('open-file', (e, path) => { e.preventDefault(); handleArgv([path]) })
  app.whenReady().then(async () => {
    Menu.setApplicationMenu(process.platform === 'darwin' ? Menu.buildFromTemplate([{ role: 'appMenu' }, { role: 'editMenu' }, { role: 'windowMenu' }]) : null)
    handleArgv(process.argv)
    try {
      showSplash('Starting…')
      coreUrl = await startCore()
      createWindow(coreUrl)
    } catch (error) {
      logLine(String(error))
      splash?.close()
      dialog.showErrorBox('Ghost Label could not start', String((error as Error).message || error))
      app.quit()
    }
  })
  app.on('window-all-closed', () => app.quit())
  // Stop the core and everything it started (engine, pip/uv). On Windows, killing the venv's
  // python.exe launcher alone can leave the real interpreter and its children running.
  app.on('before-quit', () => {
    if (!core || core.exitCode !== null || !core.pid) return
    if (isWin) spawnSync('taskkill', ['/PID', String(core.pid), '/T', '/F'], { windowsHide: true })
    else core.kill()
  })
}

export { dirname }
