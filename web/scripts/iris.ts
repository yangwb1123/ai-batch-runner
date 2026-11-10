import { lstat, readFile, realpath, symlink } from 'node:fs/promises'
import { homedir } from 'node:os'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import provenance from '../iris-source.json'

const root = fileURLToPath(new URL('..', import.meta.url))
const source = resolve(process.env.IRIS_UI_DIR || `${homedir()}/iris-ui`)

function git(...args: string[]): string {
  const result = Bun.spawnSync(['git', '-C', source, ...args], { timeout: 5000 })
  if (result.exitCode !== 0 || result.stdout.length > 65536) throw Error('Cannot verify Iris source')
  return result.stdout.toString().trim()
}

if (git('rev-parse', 'HEAD') !== provenance.revision) throw Error('Iris revision differs from iris-source.json')
if (git('status', '--porcelain=v1', '--untracked-files=all', '--', 'packages', 'LICENSE')) {
  throw Error('Iris source is dirty; review before building')
}
await readFile(`${source}/packages/svelte/src/index.ts`, 'utf8')
const link = `${root}/.iris`
try {
  const info = await lstat(link)
  if (!info.isSymbolicLink() || await realpath(link) !== source) {
    throw Error('Existing .iris does not point to the reviewed source; review it explicitly')
  }
} catch (error) {
  if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error
  await symlink(source, link, 'dir')
}
process.stdout.write(`Iris UI verified: ${provenance.revision}; source is read-only to this build\n`)
