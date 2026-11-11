// Downloads the uv binary for the target platform into resources/bin (bundled with the app so it
// can create its own Python 3.12 environments). Usage: node scripts/fetch-uv.mjs win|linux|mac
import { execFileSync } from 'node:child_process'
import { mkdirSync, rmSync, existsSync, readdirSync, copyFileSync, chmodSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'

const VERSION = process.env.UV_VERSION || '0.13.7'
const target = process.argv[2] || 'win'
const asset = { win: 'uv-x86_64-pc-windows-msvc.zip', linux: 'uv-x86_64-unknown-linux-gnu.tar.gz', mac: 'uv-aarch64-apple-darwin.tar.gz' }[target]
if (!asset) throw new Error('target must be win, linux or mac')
const url = `https://github.com/astral-sh/uv/releases/download/${VERSION}/${asset}`
const bin = join(import.meta.dirname, '..', 'resources', 'bin')
rmSync(bin, { recursive: true, force: true })
mkdirSync(bin, { recursive: true })
const work = join(tmpdir(), 'uv-fetch-' + Date.now())
mkdirSync(work, { recursive: true })
const file = join(work, asset)
console.log('downloading', url)
execFileSync('curl', ['-fsSL', '-o', file, url], { stdio: 'inherit' })
if (asset.endsWith('.zip')) execFileSync('unzip', ['-q', file, '-d', work])
else execFileSync('tar', ['-xzf', file, '-C', work])
const find = (dir) => readdirSync(dir, { withFileTypes: true }).flatMap((e) => e.isDirectory() ? find(join(dir, e.name)) : [join(dir, e.name)])
const name = target === 'win' ? 'uv.exe' : 'uv'
const found = find(work).find((p) => p.endsWith('/' + name) || p.endsWith('\\' + name))
if (!found || !existsSync(found)) throw new Error('uv binary not found in ' + asset)
copyFileSync(found, join(bin, name))
if (target !== 'win') chmodSync(join(bin, name), 0o755)
console.log('uv', VERSION, '->', join(bin, name))
