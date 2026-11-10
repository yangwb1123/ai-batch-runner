import { afterEach, expect, test } from 'bun:test'
import { mkdtemp, mkdir, readFile, rm, symlink, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { publishAssets } from '../scripts/assets'
import { sourceFiles } from '../scripts/sources'
import { build } from 'vite'
import config from '../vite.config'

const roots: string[] = []
afterEach(async () => { for (const root of roots.splice(0)) await rm(root, { recursive: true, force: true }) })

async function fixture(): Promise<string> {
  const root = await mkdtemp(join(tmpdir(), 'abr-build-test-'))
  roots.push(root)
  const files = ['package.json', 'bun.lock', 'iris-source.json', 'index.html', 'tsconfig.json', 'svelte.config.js', 'vite.config.ts']
  await Promise.all(files.map((name) => writeFile(join(root, name), '{}')))
  await Promise.all(['src', 'scripts', 'tests'].map((name) => mkdir(join(root, name))))
  await writeFile(join(root, 'src/main.ts'), 'export {}')
  return root
}

test('compiled whitespace literals retain their values without trailing source whitespace', async () => {
  const root = await mkdtemp(join(tmpdir(), 'abr-build-whitespace-'))
  roots.push(root)
  const entry = join(root, 'entry.js')
  await writeFile(join(root, 'package.json'), '{"type":"module"}')
  await writeFile(entry, [
    'export const whitespace = [..." \\t\\n\\r\\f\\u00a0\\u000b\\ufeff"].map(c => c.codePointAt(0));',
    'const tag = (parts) => ({cooked: [...parts], raw: [...parts.raw]});',
    'export const css = `selector { \t\n color: red; \t\n}`;',
    'export const tagged = tag`first \t\nsecond\\n`;',
  ].join('\n'))
  const result = await build({
    ...config, root, configFile: false, cacheDir: join(root, 'cache'), logLevel: 'silent',
    build: { ...config.build, write: false, lib: { entry, formats: ['es'] } },
  })
  const output = Array.isArray(result) ? result[0] : result
  if (!('output' in output)) throw Error('Expected an in-memory build')
  const chunk = output.output.find((item) => item.type === 'chunk')
  if (!chunk || chunk.type !== 'chunk') throw Error('Expected a JavaScript chunk')
  const loaded = await import(`data:text/javascript;base64,${Buffer.from(chunk.code).toString('base64')}`)
  expect(loaded.whitespace).toEqual([32, 9, 10, 13, 12, 160, 11, 65279])
  expect(loaded.css).toBe('selector { \t\n color: red; \t\n}')
  expect(loaded.tagged).toEqual({ cooked: ['first \t\nsecond\n'], raw: ['first \t\nsecond\\n'] })
  expect(chunk.code).not.toMatch(/[\t ]+$/m)
})

test('build inputs detect source drift and omit machine-specific/installed paths', async () => {
  const root = await fixture()
  const before = await sourceFiles(root)
  await writeFile(join(root, 'README.md'), 'documentation')
  expect(await sourceFiles(root)).toEqual(before)
  await writeFile(join(root, 'src/main.ts'), 'export const changed = true')
  expect(await sourceFiles(root)).not.toEqual(before)
  expect(before['src/main.ts'].sha256).toHaveLength(64)
})

test.each(['file', 'directory'])('build rejects %s links, not implicit source expansion', async (kind) => {
  const root = await fixture()
  await symlink(join(root, kind === 'file' ? 'src/main.ts' : 'src'), join(root, 'src/alias'))
  await expect(sourceFiles(root)).rejects.toThrow('link')
})

test('rebuild comparison is read-only on success and mismatch', async () => {
  const root = await fixture()
  const target = join(root, 'output')
  const raw = { 'app.js': 'reviewed bytes', 'assets.json': '{}' }
  await publishAssets(raw, target, false)
  await publishAssets(raw, target, true)
  await expect(publishAssets({ ...raw, 'app.js': 'different' }, target, true)).rejects.toThrow('differs')
  expect(await readFile(join(target, 'app.js'), 'utf8')).toBe('reviewed bytes')
  await writeFile(join(target, 'unexpected.js'), 'extra')
  await expect(publishAssets(raw, target, true)).rejects.toThrow('asset set')
})

test('oversize source and broad directory sets are rejected before compiling', async () => {
  const root = await fixture()
  await writeFile(join(root, 'src/big.ts'), 'x'.repeat(262145))
  await expect(sourceFiles(root)).rejects.toThrow('size')
  await rm(join(root, 'src/big.ts'))
  await Promise.all(Array.from({ length: 257 }, (_, index) => mkdir(join(root, `src/dir${index}`))))
  await expect(sourceFiles(root)).rejects.toThrow('entries')
})
