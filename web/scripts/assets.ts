import { createHash } from 'node:crypto'
import { mkdir, readFile, readdir, rename, writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'
import iris from '../iris-source.json'
import type { Sources } from './sources'

const root = fileURLToPath(new URL('..', import.meta.url))
const digest = (raw: string | Uint8Array, encoding: 'hex' | 'base64' = 'hex') => createHash('sha256').update(raw).digest(encoding)
const sri = (raw: Uint8Array) => `sha256-${digest(raw, 'base64')}`

async function stylesheetHash(file: string): Promise<string> {
  const source = await readFile(`${root}/.iris/packages/${file}`, 'utf8')
  if (source.length > 32768) throw Error('Iris stylesheet source exceeded review budget')
  const ast = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true)
  const matches: string[] = []
  for (const statement of ast.statements) {
    if (!ts.isVariableStatement(statement)) continue
    for (const declaration of statement.declarationList.declarations) {
      if (declaration.name.getText(ast) !== 'CSS' || !declaration.initializer) continue
      if (!ts.isNoSubstitutionTemplateLiteral(declaration.initializer)) throw Error('Iris CSS is not a static literal')
      matches.push(`sha256-${digest(declaration.initializer.text, 'base64')}`)
    }
  }
  if (matches.length !== 1) throw Error('Iris CSS source contract changed')
  return matches[0]
}

async function content(): Promise<Record<string, string | Uint8Array>> {
  const names = (await readdir(`${root}/dist`)).sort()
  if (JSON.stringify(names) !== JSON.stringify(['app.css', 'app.js', 'index.html'])) throw Error('Unexpected build assets; no implicit file serving')
  const js = new Uint8Array(await readFile(`${root}/dist/app.js`))
  const css = new Uint8Array(await readFile(`${root}/dist/app.css`))
  let html = await readFile(`${root}/dist/index.html`, 'utf8')
  for (const [name, integrity] of [['app.js', sri(js)], ['app.css', sri(css)]]) {
    const attribute = name.endsWith('.js') ? 'src' : 'href'
    const needle = `${attribute}="/web/assets/${name}"`
    if (html.split(needle).length !== 2) throw Error('Unexpected HTML asset reference')
    html = html.replace(needle, `${needle} integrity="${integrity}"`)
  }
  const licenses = await Promise.all([
    ['svelte', 'LICENSE.md'], ['@floating-ui/dom', 'LICENSE'],
    ['@floating-ui/core', 'LICENSE'], ['@floating-ui/utils', 'LICENSE'],
  ].map(async ([name, file]) => `## ${name}\n\n${await readFile(`${root}/node_modules/${name}/${file}`, 'utf8')}`))
  return {
    'app.js': js, 'app.css': css, 'index.html': html, 'iris.LICENSE': await readFile(`${root}/.iris/LICENSE`, 'utf8'),
    'third-party.LICENSE': licenses.join('\n\n'),
  }
}

export async function buildAssets(sources: Sources): Promise<Record<string, string | Uint8Array>> {
  const raw = await content()
  const limits: Record<string, number> = { 'app.js': 1048576, 'app.css': 262144, 'index.html': 65536, 'iris.LICENSE': 8192, 'third-party.LICENSE': 32768 }
  const files: Sources = {}
  for (const [name, value] of Object.entries(raw)) {
    const size = typeof value === 'string' ? Buffer.byteLength(value) : value.byteLength
    if (!size || size > limits[name]) throw Error(`Asset exceeds budget: ${name}`)
    files[name] = { bytes: size, sha256: digest(value) }
  }
  const style_hashes = await Promise.all([
    stylesheetHash('svelte/src/primitives/button/styles.ts'), stylesheetHash('theme/src/globalStyles.ts'),
  ])
  raw['assets.json'] = JSON.stringify({ schema_version: 2, files, style_hashes, iris, build: { bun: Bun.version, sources } }, null, 2) + '\n'
  if (Buffer.byteLength(raw['assets.json']) > 32768) throw Error('Frontend manifest exceeded budget')
  return raw
}

export async function publishAssets(raw: Record<string, string | Uint8Array>, destination: string, checkOnly: boolean): Promise<void> {
  if (checkOnly) {
    const names = (await readdir(destination)).sort()
    if (JSON.stringify(names) !== JSON.stringify(Object.keys(raw).sort())) throw Error('Shipping frontend asset set differs')
    for (const [name, value] of Object.entries(raw)) {
      if (!(await readFile(`${destination}/${name}`)).equals(Buffer.from(value))) throw Error(`Shipping frontend differs from rebuild: ${name}`)
    }
    return // no repair of stale assets during validation
  }
  await mkdir(destination, { recursive: true })
  // Manifest commits last. An interrupted replacement fails digest validation.
  // This is a source build, never an update of a live installation.
  for (const [name, value] of Object.entries(raw)) {
    const temporary = `${destination}/.${name}.${process.pid}.tmp`
    await writeFile(temporary, value, { flag: 'wx', mode: 0o644 })
    await rename(temporary, `${destination}/${name}`)
  }
}
