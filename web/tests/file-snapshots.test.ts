import { expect, test } from 'bun:test'
import { fileSet, fileBytes, previewText, FILE_SCHEMA, type FileTarget } from '../src/lib/file-snapshots'
import { createFileReader, fileTarget, type FileView } from '../src/lib/file-reader'
import { accessRecord, FILE_READ_SCOPE } from '../src/lib/task-models'
import { initial } from '../src/lib/task-state'
import { access, detail, turn, json, pause } from './task-fixture'
import type { Fetcher } from '../src/lib/api'

const target: FileTarget = { key: 'context-one', context: access.context, instance: 'alpha', session: 'session-a', turn: 'turn-a' }
const fileAccess = { ...access, file_snapshots: true, scopes: [...access.scopes, FILE_READ_SCOPE] }
const accessFetcher = (fetcher: Fetcher): Fetcher => (path, init) => String(path) === '/web/api/access' ? Promise.resolve(json({ data: fileAccess })) : fetcher(path, init)
const identity = { schema: FILE_SCHEMA, verification: 'content_integrity_only', instance_id: target.instance,
  session_id: target.session, turn_id: target.turn, snapshot_set_id: 'a'.repeat(32), created_at: 1700000000 }
async function fixture(raw = '<script>alert(1)</script>\nhello') {
  const bytes = new TextEncoder().encode(raw)
  const sha256 = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), (byte) => byte.toString(16).padStart(2, '0')).join('')
  const metadata = { snapshot_id: 'b'.repeat(32), path: 'reports/result.html', size_bytes: bytes.length, sha256, content_kind: 'text' }
  return { bytes, list: { data: { ...identity, state: 'ready', files: [metadata], unavailable: [] } },
    content: { data: { ...identity, ...metadata, content_b64: btoa(String.fromCharCode(...bytes)) } } }
}

test('additional read capability requires an explicit coherent access flag', () => {
  expect(accessRecord(access)).toEqual({ ...access, mode: 'control' })
  expect(accessRecord({ ...access, file_snapshots: false }).file_snapshots).toBe(false)
  expect(accessRecord({ ...access, scopes: [...access.scopes, FILE_READ_SCOPE], file_snapshots: true }).file_snapshots).toBe(true)
  for (const data of [{ ...access, file_snapshots: true }, { ...access, scopes: [...access.scopes, FILE_READ_SCOPE] },
    { ...access, file_snapshots: 'true' }]) expect(() => accessRecord(data)).toThrow()
})

test('only a currently bound terminal task permits file reads', () => {
  const state = { ...initial(), access: accessRecord({ ...access, file_snapshots: true, scopes: [...access.scopes, FILE_READ_SCOPE] }),
    detail, currentTurn: turn, selectedSession: detail.session_id, selectedTurn: turn.turn_id, readAt: 1700000001000 }
  expect(fileTarget(state)?.turn).toBe(turn.turn_id)
  for (const patch of [{ readAt: null }, { syncStopped: true }, { selectedTurn: 'other' }, { selectedSession: 'other' },
    { currentTurn: { ...turn, state: 'running' } }, { access: accessRecord(access) }, { access: null }]) expect(fileTarget({ ...state, ...patch })).toBeNull()
})

test('empty, binary and text bytes have explicit semantics and exact hashes', async () => {
  for (const raw of ['', '中文\n<script>never execute</script>', '\u0000binary']) {
    const data = await fixture(raw)
    if (raw.includes('\0')) { data.list.data.files[0]!.content_kind = 'binary'; data.content.data.content_kind = 'binary' }
    const set = fileSet(data.list, target), file = set.files[0]!
    expect(await fileBytes(data.content, target, set, file)).toEqual(data.bytes)
    expect(previewText(data.bytes, file)).toBe(raw.includes('\0') ? null : raw)
  }
})

test('rejects swapped identity, invalid base64, size, metadata and digest before displaying', async () => {
  const data = await fixture(), set = fileSet(data.list, target), file = set.files[0]!
  for (const patch of [{ session_id: 'other' }, { instance_id: 'other' }, { turn_id: 'other' }, { snapshot_set_id: 'c'.repeat(32) },
    { snapshot_id: 'c'.repeat(32) }, { sha256: 'f'.repeat(64) }, { path: '../escape' }, { size_bytes: 0 },
    { content_b64: '====' }, { content_b64: btoa('changed') }, { created_at: 5 }, { content_kind: 'binary' }]) {
    await expect(fileBytes({ data: { ...data.content.data, ...patch } }, target, set, file)).rejects.toThrow()
  }
  const forged = { ...data.content.data, sha256: 'f'.repeat(64) }
  await expect(fileBytes({ data: forged }, target, set, { ...file, sha256: forged.sha256 })).rejects.toThrow()
})

test('set states and path/size budgets cannot become empty success', async () => {
  const { list } = await fixture()
  for (const patch of [{ state: 'absent' }, { files: [] }, { unavailable: [{ path: 'other', code: 'invented' }] },
    { files: [list.data.files[0], list.data.files[0]] }, { files: [{ ...list.data.files[0], size_bytes: 65537 }] },
    { files: [{ ...list.data.files[0], path: 'evil\u202efile' }] }]) expect(() => fileSet({ data: { ...list.data, ...patch } }, target)).toThrow()
  expect(fileSet({ data: { ...identity, snapshot_set_id: null, created_at: null, state: 'absent', files: [], unavailable: [] } }, target).state).toBe('absent')
})

test('UTF8 BOM remains visible and is never mislabeled as a zero-byte file', async () => {
  for (const raw of ['\ufeff', '\ufeffhello']) {
    const data = await fixture(raw), set = fileSet(data.list, target), file = set.files[0]!
    const bytes = await fileBytes(data.content, target, set, file)
    expect(bytes).toEqual(data.bytes)
    expect(previewText(bytes, file)).toBe(raw.replace('\ufeff', '\\u{feff}'))
    expect(file.size_bytes).toBeGreaterThan(0)
  }
})

test('permission change or task switch aborts reads and discards late content', async () => {
  const data = await fixture()
  let current: FileTarget | null = target, state: FileView, finish!: (response: Response) => void, signal: AbortSignal | undefined, saves = 0
  const reader = createFileReader(() => current, (value) => { state = value }, () => { saves++ }, () => {}, accessFetcher(async (path, init) => {
    if (String(path).endsWith('/file-snapshots')) return json(data.list)
    signal = init?.signal as AbortSignal
    return new Promise((resolve) => { finish = resolve })
  }))
  await reader.read()
  const pending = reader.read(state!.list!.files[0], true)
  await pause()
  current = null; reader.bind()
  expect(signal!.aborted).toBe(true)
  finish(json(data.content)); await pending
  expect(state!.preview).toBeNull(); expect(state!.list).toBeNull(); expect(saves).toBe(0)
  current = { ...target, key: 'new-login' }; reader.bind(); expect(state!.list).toBeNull()
})

test('each download reauthenticates over GET and revoked content is cleared', async () => {
  const data = await fixture()
  let state: FileView, deny = false, saves = 0, reads = 0
  const reader = createFileReader(() => target, (value) => { state = value }, (_file, bytes) => {
    saves++; expect(bytes).toEqual(data.bytes)
  }, () => {}, accessFetcher(async (path, init) => {
    expect(init?.credentials).toBe('same-origin'); expect(init?.redirect).toBe('error'); expect(init?.cache).toBe('no-store')
    expect(init?.body).toBeUndefined()
    if (String(path).endsWith('/file-snapshots')) return json(data.list)
    reads++; return deny ? json({}, 403) : json(data.content)
  }))
  await reader.read(); const file = state!.list!.files[0]!
  // Svelte wraps props in proxies; metadata object identity is not an authorization boundary.
  await reader.read({ ...file }); expect(state!.preview?.text).toContain('<script>')
  await reader.read(file, true); expect(reads).toBe(2); expect(saves).toBe(1)
  deny = true; await reader.read(file, true)
  expect(saves).toBe(1); expect(state!.preview).toBeNull(); expect(state!.list).toBeNull(); expect(state!.problem).toContain('权限')
  reader.clear()
})

test('unmounted/page-hidden reader cannot publish or download a late response', async () => {
  const data = await fixture()
  let state: FileView, finish!: (response: Response) => void
  const reader = createFileReader(() => target, (value) => { state = value }, () => { throw new Error('late download') }, () => {}, accessFetcher(async () => new Promise((resolve) => { finish = resolve })))
  const pending = reader.read(); await pause(); reader.clear(); finish(json(data.list)); await pending
  expect(state!.list).toBeNull(); expect(state!.busy).toBe(false)
})

test('another tab changing the Cookie context cannot publish under unchanged local task state', async () => {
  const data = await fixture()
  let state: FileView, context = target.context, swapAfterFile = false, saves = 0
  const reader = createFileReader(() => target, (value) => { state = value }, () => { saves++ }, () => {}, async (path) => {
    if (String(path) === '/web/api/access') return json({ data: { ...fileAccess, context } })
    if (String(path).endsWith('/file-snapshots')) return json(data.list)
    if (swapAfterFile) context = 'c'.repeat(64)
    return json(data.content)
  })
  await reader.read(); swapAfterFile = true
  await reader.read(state!.list!.files[0], true)
  expect(state!.list).toBeNull(); expect(state!.preview).toBeNull(); expect(saves).toBe(0)
  expect(state!.problem).toContain('登录')
  await reader.read(); expect(state!.list).toBeNull(); expect(state!.problem).toContain('登录')
})
