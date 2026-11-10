import { mkdir, rmdir } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { buildAssets, publishAssets } from './assets'
import { sourceFiles } from './sources'
import pkg from '../package.json'

const root = fileURLToPath(new URL('..', import.meta.url))
const destination = fileURLToPath(new URL('../../pbatch/hub_web_assets/', import.meta.url))
const args = process.argv.slice(2)
if (args.length > 1 || (args.length === 1 && args[0] !== '--check')) throw Error('Only --check is supported')
if (`bun@${Bun.version}` !== pkg.packageManager) throw Error('Use the Bun version pinned by packageManager')
const deadline = performance.now() + 180000

function run(script: string, ...arguments_: string[]): void {
  const remaining = deadline - performance.now()
  if (remaining <= 0) throw Error('Frontend build deadline exceeded')
  const result = Bun.spawnSync([process.execPath, script, ...arguments_], {
    cwd: root, timeout: Math.ceil(remaining), stdout: 'inherit', stderr: 'inherit',
  })
  if (result.exitCode !== 0 || performance.now() >= deadline) throw Error(`Frontend build step failed: ${script}`)
}

// Refuse concurrent builders. A crash retains the lock for explicit operator review.
const lock = `${root}/.build-lock`
await mkdir(lock, { mode: 0o700 })
try {
  const before = await sourceFiles(root)
  run('scripts/iris.ts')
  run('node_modules/vite/bin/vite.js', 'build')
  const assets = await buildAssets(before)
  run('scripts/iris.ts')
  if (JSON.stringify(before) !== JSON.stringify(await sourceFiles(root))) throw Error('Frontend sources changed during build')
  if (performance.now() >= deadline) throw Error('Frontend build deadline exceeded')
  await publishAssets(assets, destination, args[0] === '--check')
  const summary = args[0] === '--check' ? 'Frontend rebuild matches shipping bytes; no assets changed' : 'Frontend assets and source manifest built'
  process.stdout.write(`${summary}\n`)
} finally {
  await rmdir(lock)
}
