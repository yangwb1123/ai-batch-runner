import { expect, test } from 'bun:test'
import { createDrafts, MAX_DRAFT_BYTES } from '../src/lib/drafts'
import { appendDraft, draftAvailable, draftBackupAvailable, draftMatches, draftTarget, exportDraft } from '../src/lib/draft-file'
import { readDraftText } from '../src/lib/draft-file-reader'
import { prepareAction } from '../src/lib/task-api'
import { initial, type Tasks } from '../src/lib/task-state'
import { access, detail } from './task-fixture'

const encoder = new TextEncoder()
const signal = () => new AbortController().signal
function snapshot(): Tasks {
  return { ...initial(), initialized: true, access: { ...access, mode: 'control', scopes: [...access.scopes] },
    detail: { ...detail }, selectedSession: detail.session_id, readAt: 1700000001000 }
}
function reportedFile(size: number, stream: () => ReadableStream<Uint8Array>, name = 'draft.txt'): File {
  return { name, size, stream } as File
}
function bytesFile(bytes: Uint8Array, size = bytes.length): File {
  return reportedFile(size, () => new ReadableStream({ start(controller) { controller.enqueue(bytes); controller.close() } }))
}

test('draft file roundtrip preserves UTF-8 BOM, CRLF, CR, tabs and original text exactly', async () => {
  const state = snapshot(), text = '\ufeff目标：保持原文\r\n范围：已有模块\r验收：\t测试通过\n'
  const saved = exportDraft(state, text)
  const file = new File([saved.text], saved.filename, { type: 'text/plain' })
  expect(saved.filename).toBe('abr-draft-session-a.txt')
  expect(saved.text).toBe(text)
  const loaded = await readDraftText(file, signal())
  expect(encoder.encode(loaded)).toEqual(encoder.encode(text))
  expect(appendDraft(state, draftTarget(state)!, '', loaded)).toBe(text)
})

test('export contains only editor text and does not append identity or pending metadata', () => {
  const state = snapshot()
  Object.assign(state, { token: 'private-token', draft: 'another-draft', idempotency_key: 'private-key' })
  const text = 'User-entered password=do-not-silently-redact-this-text'
  expect(exportDraft(state, text).text).toBe(text)
  for (const value of [state.access!.context, 'private-token', 'another-draft', 'private-key', 'instance_id']) {
    expect(exportDraft(state, text).text).not.toContain(value)
  }
  state.detail!.session_id = state.selectedSession = '../not-a-filename'
  expect(exportDraft(state, text).filename).toBe('abr-draft-selected.txt')
})

test('64KiB backup and recovery retain the existing independent 16KiB send gate', async () => {
  const state = snapshot(), text = '字'.repeat(21845) + 'x'
  expect(encoder.encode(text).length).toBe(MAX_DRAFT_BYTES)
  const saved = exportDraft(state, text)
  const restored = await readDraftText(new File([saved.text], saved.filename), signal())
  const drafts = createDrafts(); drafts.context(state.contextEpoch)
  drafts.set(state.selectedSession, appendDraft(state, draftTarget(state)!, '', restored))
  expect(drafts.get(state.selectedSession)).toBe(text)
  expect(() => prepareAction('send', state.selectedSession, text, state.access!)).toThrow()
  expect(prepareAction('send', state.selectedSession, 'x'.repeat(16384), state.access!).body).toContain('prompt')
  expect(() => exportDraft(state, text + 'x')).toThrow('65536')
  expect(() => drafts.set(state.selectedSession, text + 'x')).toThrow()
  expect(drafts.get(state.selectedSession)).toBe(text)
})

test('append reads the latest draft, preserves both texts and refuses overflow without clipping', () => {
  const state = snapshot(), target = draftTarget(state)!, imported = 'saved scope\r\n'
  const original = 'original draft', latest = original + '\nnew manual requirement'
  expect(appendDraft(state, target, latest, imported)).toBe(latest + '\n\n' + imported)
  expect(appendDraft(state, target, 'x'.repeat(MAX_DRAFT_BYTES - 3), 'y')).toHaveLength(MAX_DRAFT_BYTES)
  const full = 'x'.repeat(MAX_DRAFT_BYTES - 2)
  expect(() => appendDraft(state, target, full, 'y')).toThrow('65536')
  expect(full).toHaveLength(MAX_DRAFT_BYTES - 2)
  expect(imported).toBe('saved scope\r\n')
})

test('stable polling and selected-turn changes retain session draft identity while reads lock actions', () => {
  const state = snapshot(), target = draftTarget(state)!
  state.refreshing = true; state.busy = true; state.readAt! += 1000; state.selectedTurn = 'another-turn'
  expect(draftMatches(state, target)).toBe(true)
  expect(draftAvailable(state)).toBe(false)
  expect(() => appendDraft(state, target, 'keep', 'import')).toThrow()
  state.refreshing = false; state.busy = false
  expect(draftAvailable(state)).toBe(true)
  expect(appendDraft(state, target, 'keep', 'import')).toBe('keep\n\nimport')
})

test('changed identity and instance invalidate the captured target without invalidating a newly authorized view', () => {
  const target = draftTarget(snapshot())!
  for (const change of [(s: Tasks) => { s.contextEpoch++ }, (s: Tasks) => { s.access!.context = 'b'.repeat(64) },
    (s: Tasks) => { s.detail!.instance_id = 'other' }]) {
    const state = snapshot(); change(state)
    expect(draftMatches(state, target)).toBe(false)
    expect(draftAvailable(state)).toBe(true)
    expect(() => appendDraft(state, target, 'keep', 'import')).toThrow()
  }
})

test('invalid selected session and missing read authority block backup and append', () => {
  const target = draftTarget(snapshot())!
  const changes = [(s: Tasks) => { s.selectedSession = 'other' },
    (s: Tasks) => { s.access = null }, (s: Tasks) => { s.access!.task_access = false },
    (s: Tasks) => { s.access!.scopes = ['agent.sessions:write'] },
    (s: Tasks) => { s.access!.scopes = ['agent.instances:read', 'agent.sessions:read'] },
    (s: Tasks) => { s.readAt = null },
    (s: Tasks) => { s.syncStopped = true }, (s: Tasks) => { s.detail = null }]
  for (const change of changes) {
    const state = snapshot(); change(state)
    expect(draftMatches(state, target)).toBe(false)
    expect(draftAvailable(state)).toBe(false)
    expect(() => exportDraft(state, 'private draft')).toThrow()
    expect(() => appendDraft(state, target, 'keep', 'import')).toThrow()
  }
})

test('closing a session permits backup of its existing draft but blocks appending new text', () => {
  const state = snapshot(), target = draftTarget(state)!
  state.detail!.status = 'closed'
  expect(draftMatches(state, target)).toBe(true)
  expect(draftBackupAvailable(state)).toBe(true)
  expect(draftAvailable(state)).toBe(false)
  expect(exportDraft(state, 'retain my existing draft').text).toBe('retain my existing draft')
  expect(() => appendDraft(state, target, 'retain my existing draft', 'new')).toThrow()
  state.detail!.status = 'idle'; state.detail!.online = false; state.detail!.frozen = true
  state.detail!.controllable = false; state.detail!.active_turn_id = 'running'
  expect(draftAvailable(state)).toBe(true)
  expect(appendDraft(state, target, 'keep', 'new')).toBe('keep\n\nnew')
})

test('unknown or pending writes block backup and append without changing the original request', () => {
  const state = snapshot(), target = draftTarget(state)!
  const pending = prepareAction('send', state.selectedSession, 'original submission', state.access!)
  state.pending = pending
  expect(draftAvailable(state)).toBe(false)
  expect(() => exportDraft(state, 'keep')).toThrow()
  expect(() => appendDraft(state, target, 'keep', 'import')).toThrow()
  state.pending = null; state.outcome = 'unknown'
  expect(draftAvailable(state)).toBe(false)
  expect(() => appendDraft(state, target, 'keep', 'import')).toThrow()
  expect(JSON.parse(pending.body)).toEqual({ prompt: 'original submission' })
  expect(pending.key).toBeTruthy()
})

test('new file entry rejects empty, control, bidi and invalid Unicode without changing text', async () => {
  const state = snapshot(), target = draftTarget(state)!
  for (const value of ['', ' \t\r\n', '\u0000bad', 'bad\u000b', 'bad\u007f', 'bad\u0085', 'bad\u202e', 'bad\u2068', '\ud800']) {
    expect(() => exportDraft(state, value)).toThrow()
    expect(() => appendDraft(state, target, 'preserve me', value)).toThrow()
  }
  for (const value of ['bad\u0000', 'bad\u202e', ' \r\n']) {
    await expect(readDraftText(new File([value], 'draft.txt'), signal())).rejects.toThrow()
  }
  await expect(readDraftText(bytesFile(new Uint8Array([0xc3, 0x28])), signal())).rejects.toThrow('UTF-8')
  expect(appendDraft(state, target, 'old\r\n', 'new\tline')).toBe('old\r\n\n\nnew\tline')
})

test('file size and text extension are rejected before stream allocation', async () => {
  let reads = 0
  const stream = () => { reads++; throw Error('must not read') }
  for (const size of [0, MAX_DRAFT_BYTES + 1, -1, 1.5, NaN, Infinity]) {
    await expect(readDraftText(reportedFile(size, stream), signal())).rejects.toThrow()
  }
  for (const name of ['draft.json', 'draft.txt.exe', 'draft', 'x'.repeat(257) + '.txt']) {
    await expect(readDraftText(reportedFile(1, stream, name), signal())).rejects.toThrow()
  }
  expect(reads).toBe(0)
  expect(await readDraftText(new File(['ok'], 'DRAFT.TXT'), signal())).toBe('ok')
})

test('reported size and actual stream byte size must match, including truncation and overflow', async () => {
  const bytes = encoder.encode('原文')
  await expect(readDraftText(bytesFile(bytes, 1), signal())).rejects.toThrow()
  await expect(readDraftText(bytesFile(bytes, 10), signal())).rejects.toThrow()
  await expect(readDraftText(bytesFile(new Uint8Array(MAX_DRAFT_BYTES + 1), 1), signal())).rejects.toThrow()
  let cancelled = false
  const endless = reportedFile(1, () => new ReadableStream({ pull(c) { c.enqueue(new Uint8Array()) }, cancel() { cancelled = true } }))
  await expect(readDraftText(endless, signal())).rejects.toThrow()
  expect(cancelled).toBe(true)
})

test('aborted input is never opened and cancellation releases a pending owned read', async () => {
  const before = new AbortController(); before.abort()
  let reads = 0
  await expect(readDraftText(reportedFile(1, () => { reads++; throw Error('must not open') }), before.signal)).rejects.toThrow()
  expect(reads).toBe(0)
  const during = new AbortController(); let cancelled = false
  const blocked = reportedFile(1, () => new ReadableStream({ cancel() { cancelled = true; return new Promise(() => {}) } }))
  const result = readDraftText(blocked, during.signal)
  during.abort()
  await expect(result).rejects.toThrow()
  expect(cancelled).toBe(true)
})

test('opening a file cannot postpone the complete read deadline', async () => {
  const original = Object.getOwnPropertyDescriptor(performance, 'now')
  let clock = 1000, cancelled = false
  Object.defineProperty(performance, 'now', { configurable: true, value: () => clock })
  try {
    const slowOpen = reportedFile(1, () => {
      clock += 5001
      return new ReadableStream({ start(c) { c.enqueue(new Uint8Array([65])); c.close() }, cancel() { cancelled = true } })
    })
    await expect(readDraftText(slowOpen, signal())).rejects.toThrow('5 秒')
    expect(cancelled).toBe(true)
  } finally {
    if (original) Object.defineProperty(performance, 'now', original)
    else delete (performance as Partial<Performance>).now
  }
})

test('continuing chunks cannot renew the five-second complete read deadline', async () => {
  let timer: ReturnType<typeof setInterval>, chunks = 0, cancelled = false
  const continuing = reportedFile(100, () => new ReadableStream({
    start(controller) { timer = setInterval(() => { chunks++; controller.enqueue(new Uint8Array([65])) }, 80) },
    cancel() { cancelled = true; clearInterval(timer) },
  }))
  const started = performance.now()
  try { await expect(readDraftText(continuing, signal())).rejects.toThrow('5 秒') }
  finally { clearInterval(timer!) }
  expect(cancelled).toBe(true)
  expect(chunks).toBeGreaterThan(1); expect(chunks).toBeLessThan(100)
  expect(performance.now() - started).toBeLessThan(6500)
}, 10000)
