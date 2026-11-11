import { build } from 'esbuild'
import { cpSync, mkdirSync } from 'node:fs'

mkdirSync('dist/renderer', { recursive: true })
await build({ entryPoints: ['src/main/main.ts'], bundle: true, platform: 'node', target: 'node20', outfile: 'dist/main/main.js', external: ['electron'], format: 'cjs', sourcemap: true })
await build({ entryPoints: ['src/main/preload.ts'], bundle: true, platform: 'node', target: 'node20', outfile: 'dist/main/preload.js', external: ['electron'], format: 'cjs' })
await build({ entryPoints: ['src/renderer/app.ts'], bundle: true, platform: 'browser', target: 'chrome120', outfile: 'dist/renderer/app.js', format: 'esm', sourcemap: true })
cpSync('src/renderer/index.html', 'dist/renderer/index.html')
cpSync('src/renderer/styles.css', 'dist/renderer/styles.css')
cpSync('resources/icon.png', 'dist/renderer/icon.png')
console.log('built')
