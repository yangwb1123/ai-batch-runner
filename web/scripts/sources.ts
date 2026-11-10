import { createHash } from 'node:crypto'
import { constants } from 'node:fs'
import { lstat, open, opendir } from 'node:fs/promises'
import { join, relative } from 'node:path'

export type Sources = Record<string, { bytes: number; sha256: string }>
const inputs = ['package.json', 'bun.lock', 'iris-source.json', 'index.html', 'tsconfig.json', 'svelte.config.js', 'vite.config.ts']

async function record(root: string, path: string, result: Sources): Promise<void> {
  if (Object.keys(result).length >= 128) throw Error('Frontend input count exceeded')
  if ((await lstat(path)).isSymbolicLink()) throw Error('Frontend input cannot be a link')
  const handle = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK)
  try {
    const info = await handle.stat()
    if (!info.isFile() || info.size > 262144) throw Error('Frontend input type or size exceeded')
    const buffer = Buffer.alloc(info.size + 1)
    const { bytesRead } = await handle.read(buffer, 0, buffer.length, 0)
    const after = await handle.stat()
    const named = await lstat(path)
    if (bytesRead !== info.size || after.size !== info.size || after.mtimeMs !== info.mtimeMs || after.ctimeMs !== info.ctimeMs || named.ino !== info.ino || named.dev !== info.dev || named.isSymbolicLink()) {
      throw Error('Frontend input changed while reading')
    }
    const name = relative(root, path).split('\\').join('/')
    result[name] = { bytes: bytesRead, sha256: createHash('sha256').update(buffer.subarray(0, bytesRead)).digest('hex') }
    if (Object.values(result).reduce((sum, row) => sum + row.bytes, 0) > 2097152) throw Error('Frontend total input bytes exceeded')
  } finally {
    await handle.close()
  }
}

async function collect(root: string, directory: string, result: Sources, budget: { visited: number }, depth = 0): Promise<void> {
  if ((await lstat(directory)).isSymbolicLink() || depth > 8) throw Error('Frontend directory link or depth exceeded')
  for await (const entry of await opendir(directory)) {
    if (++budget.visited > 256) throw Error('Frontend directory entries exceeded')
    const path = join(directory, entry.name)
    if (entry.isSymbolicLink()) throw Error('Frontend input cannot be a link')
    if (entry.isDirectory()) await collect(root, path, result, budget, depth + 1)
    else await record(root, path, result)
  }
}

export async function sourceFiles(root: string): Promise<Sources> {
  if ((await lstat(root)).isSymbolicLink()) throw Error('Frontend source root cannot be a link')
  const result: Sources = {}
  for (const name of inputs) await record(root, join(root, name), result)
  const budget = { visited: 0 }
  for (const name of ['src', 'scripts', 'tests']) await collect(root, join(root, name), result, budget)
  return Object.fromEntries(Object.entries(result).sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0))
}
