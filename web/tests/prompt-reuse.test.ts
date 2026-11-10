import { expect, spyOn, test } from 'bun:test'
import { appendPromptReuse, preparePromptReuse, promptReuseAvailable, promptReuseMatches } from '../src/lib/prompt-reuse'
import { MAX_DRAFT_BYTES, createDrafts } from '../src/lib/drafts'
import { prepareAction } from '../src/lib/task-api'
import { turnRecord } from '../src/lib/task-history'
import { initial, type Tasks } from '../src/lib/task-state'
import { access, detail, turn } from './task-fixture'

function snapshot(prompt = '已读取的指令 [REDACTED]\n保留范围与验收。'): Tasks {
  return { ...initial(3), initialized: true, access: { ...access, mode: 'control', scopes: [...access.scopes] },
    detail: { ...detail }, currentTurn: { ...turn, prompt }, selectedSession: detail.session_id,
    selectedTurn: turn.turn_id, readAt: 1700000001000 }
}

test('prepares the exact visible current instruction and freezes its source identity', () => {
  const text = '\ufeff  目标 [REDACTED]\n\t<script>literal()</script>\n尾行  \n', state = snapshot(text)
  const prepared = preparePromptReuse(state)
  expect(prepared.text).toBe(text)
  expect(prepared.reference).toEqual({ target: { epoch: 3, context: access.context,
    session: detail.session_id, instance: detail.instance_id }, turn: turn.turn_id, prompt: text })
  expect(Object.isFrozen(prepared.reference)).toBe(true)
  expect(Object.isFrozen(prepared.reference.target)).toBe(true)
  expect(promptReuseAvailable(state)).toBe(true)
  expect(promptReuseMatches(state, prepared.reference)).toBe(true)
  expect(appendPromptReuse(state, prepared.reference, '', prepared.text)).toBe(text)
})

test('reuses the parser-visible text without reconstructing submitted secrets or control characters', () => {
  const state = snapshot()
  state.currentTurn = turnRecord({ ...turn, prompt: 'visible [REDACTED]\r\nline\u202e' }, detail.session_id)
  const prepared = preparePromptReuse(state)
  expect(prepared.text).toBe('visible [REDACTED] \nline ')
  expect(appendPromptReuse(state, prepared.reference, '', prepared.text)).toBe(state.currentTurn.prompt)
})

test('only the opened turn is a source, independent of live and listed history turns', () => {
  const state = snapshot('opened historical instruction')
  state.turnMode = 'fixed'
  state.liveTurn = { ...turn, turn_id: 'live-turn', prompt: 'do not copy live' }
  state.turns = { data: [{ ...turn, turn_id: 'list-turn', prompt: 'do not copy list' }], next_cursor: '' }
  expect(preparePromptReuse(state).text).toBe('opened historical instruction')
  state.currentTurn = null
  expect(promptReuseAvailable(state)).toBe(false)
  expect(() => preparePromptReuse(state)).toThrow()
})

test('source binding rejects missing or mismatched selected turn, session and instance', () => {
  const prepared = preparePromptReuse(snapshot())
  const mutations = [(s: Tasks) => { s.currentTurn = null }, (s: Tasks) => { s.selectedTurn = '' },
    (s: Tasks) => { s.selectedTurn = 'other-turn' }, (s: Tasks) => { s.currentTurn!.turn_id = 'other-turn' },
    (s: Tasks) => { s.currentTurn!.session_id = 'other-session' }, (s: Tasks) => { s.selectedSession = 'other-session' },
    (s: Tasks) => { s.currentTurn!.instance_id = 'other-instance' }, (s: Tasks) => { s.detail!.session_id = 'other-session' },
    (s: Tasks) => { s.detail = null }]
  for (const mutate of mutations) {
    const state = snapshot(); mutate(state)
    expect(promptReuseAvailable(state)).toBe(false)
    expect(promptReuseMatches(state, prepared.reference)).toBe(false)
    expect(() => preparePromptReuse(state)).toThrow()
    expect(() => appendPromptReuse(state, prepared.reference, 'keep', 'addition')).toThrow()
  }
})

test('full identity changes invalidate an old reference even when the next view is independently authorized', () => {
  const reference = preparePromptReuse(snapshot()).reference
  const mutations = [(s: Tasks) => { s.contextEpoch++ }, (s: Tasks) => { s.access!.context = 'b'.repeat(64) },
    (s: Tasks) => { s.detail!.instance_id = s.currentTurn!.instance_id = 'other-instance' },
    (s: Tasks) => { s.detail!.session_id = s.currentTurn!.session_id = s.selectedSession = 'other-session' },
    (s: Tasks) => { s.currentTurn!.turn_id = s.selectedTurn = 'other-turn' }]
  for (const mutate of mutations) {
    const state = snapshot(); mutate(state)
    expect(promptReuseAvailable(state)).toBe(true)
    expect(promptReuseMatches(state, reference)).toBe(false)
    expect(() => appendPromptReuse(state, reference, 'keep', 'addition')).toThrow()
  }
})

test('missing current authority or facts invalidates the reference and blocks operations', () => {
  const reference = preparePromptReuse(snapshot()).reference
  const mutations = [(s: Tasks) => { s.access = null }, (s: Tasks) => { s.access!.task_access = false },
    (s: Tasks) => { s.access!.mode = 'read_only' }, (s: Tasks) => { s.access!.context = '' },
    (s: Tasks) => { s.readAt = null }, (s: Tasks) => { s.syncStopped = true },
    ...['agent.instances:read', 'agent.sessions:read', 'agent.sessions:write'].map((scope) =>
      (s: Tasks) => { s.access!.scopes = s.access!.scopes.filter((value) => value !== scope) })]
  for (const mutate of mutations) {
    const state = snapshot(); mutate(state)
    expect(promptReuseAvailable(state)).toBe(false)
    expect(promptReuseMatches(state, reference)).toBe(false)
    expect(() => preparePromptReuse(state)).toThrow()
    expect(() => appendPromptReuse(state, reference, 'keep', 'addition')).toThrow()
  }
})

test('ordinary read clocks, active turn state and unrelated events preserve the exact instruction reference', () => {
  const state = snapshot(), prepared = preparePromptReuse(state)
  for (const status of ['queued', 'running', 'cancel_requested', 'cancelled', 'failed', 'completed', 'interrupted', 'lost']) {
    state.currentTurn!.state = status; state.currentTurn!.updated_at++; state.readAt!++
    state.events = { data: [], next_cursor: 5 }
    expect(promptReuseMatches(state, prepared.reference)).toBe(true)
    expect(appendPromptReuse(state, prepared.reference, '', prepared.text)).toBe(prepared.text)
  }
})

test('busy and refresh lock actions without expiring a same-facts preview', () => {
  const state = snapshot(), prepared = preparePromptReuse(state)
  for (const flag of ['busy', 'refreshing'] as const) {
    state[flag] = true
    expect(promptReuseMatches(state, prepared.reference)).toBe(true)
    expect(promptReuseAvailable(state)).toBe(false)
    expect(() => preparePromptReuse(state)).toThrow()
    expect(() => appendPromptReuse(state, prepared.reference, 'latest', prepared.text)).toThrow()
    state[flag] = false
  }
  expect(promptReuseAvailable(state)).toBe(true)
  expect(appendPromptReuse(state, prepared.reference, 'latest', prepared.text)).toBe('latest\n\n' + prepared.text)
})

test('pending, unknown and closed prevent reuse without changing the original request or draft', () => {
  const prepared = preparePromptReuse(snapshot())
  const pending = prepareAction('send', detail.session_id, 'original submitted instruction', snapshot().access!)
  for (const mutate of [(s: Tasks) => { s.pending = pending }, (s: Tasks) => { s.outcome = 'unknown' },
    (s: Tasks) => { s.detail!.status = 'closed' }]) {
    const state = snapshot(); mutate(state)
    const before = JSON.stringify(state)
    expect(promptReuseAvailable(state)).toBe(false)
    expect(() => preparePromptReuse(state)).toThrow()
    expect(() => appendPromptReuse(state, prepared.reference, 'keep new draft', 'addition')).toThrow()
    expect(JSON.stringify(state)).toBe(before)
  }
  expect(JSON.parse(pending.body)).toEqual({ prompt: 'original submitted instruction' })
})

test('offline, frozen, uncontrollable and running sessions can still arrange a local draft', () => {
  const state = snapshot(); Object.assign(state.detail!, { online: false, frozen: true, controllable: false, active_turn_id: 'active' })
  const prepared = preparePromptReuse(state)
  expect(promptReuseAvailable(state)).toBe(true)
  expect(appendPromptReuse(state, prepared.reference, 'draft', prepared.text)).toBe('draft\n\n' + prepared.text)
})

test('changed text beyond the list excerpt or changed truncation invalidates the captured source', () => {
  const text = 'x'.repeat(16000) + ' original', state = snapshot(text), prepared = preparePromptReuse(state)
  state.currentTurn!.prompt = 'x'.repeat(16000) + ' changed'
  expect(promptReuseMatches(state, prepared.reference)).toBe(false)
  expect(() => appendPromptReuse(state, prepared.reference, 'keep', prepared.text)).toThrow()
  state.currentTurn!.prompt = text; state.currentTurn!.prompt_truncated = true
  expect(promptReuseMatches(state, prepared.reference)).toBe(false)
  expect(promptReuseAvailable(state)).toBe(false)
  expect(() => preparePromptReuse(state)).toThrow()
})

test('empty, invalid Unicode, control and oversized source prompts fail closed without sanitizing', () => {
  for (const text of ['', ' \t\r\n', '\u0000bad', 'bad\u000b', 'bad\u007f', 'bad\u0085', 'bad\u202e',
    'bad\u2068', '\ud800', '\udfff', 'x'.repeat(16385), '字'.repeat(5462)]) {
    const state = snapshot(text), before = state.currentTurn!.prompt
    expect(promptReuseAvailable(state)).toBe(false)
    expect(() => preparePromptReuse(state)).toThrow()
    expect(state.currentTurn!.prompt).toBe(before)
  }
})

test('source budget is 16384 UTF-8 bytes and is independent of the larger draft limit', () => {
  for (const text of ['x'.repeat(16384), '字'.repeat(5461) + 'x', '🧪'.repeat(4096)]) {
    const state = snapshot(text), prepared = preparePromptReuse(state)
    expect(new TextEncoder().encode(prepared.text)).toHaveLength(16384)
    expect(appendPromptReuse(state, prepared.reference, '', prepared.text)).toBe(text)
  }
  expect(promptReuseAvailable(snapshot('字'.repeat(5461) + 'xx'))).toBe(false)
})

test('edited preview appends to the latest draft and leaves source and earlier draft unchanged', () => {
  const state = snapshot(), prepared = preparePromptReuse(state), original = 'original\n', latest = original + 'new manual requirement'
  const edited = '  edited\r\n\tinstruction\n'
  expect(appendPromptReuse(state, prepared.reference, latest, edited)).toBe(latest + '\n\n' + edited)
  expect(appendPromptReuse(state, prepared.reference, '', edited)).toBe(edited)
  expect(prepared.text).toBe(state.currentTurn!.prompt)
  expect(original).toBe('original\n')
})

test('64KiB assembled draft is retained while the existing 16KiB send gate remains unchanged', () => {
  const state = snapshot(), reference = preparePromptReuse(state).reference
  const text = appendPromptReuse(state, reference, 'x'.repeat(MAX_DRAFT_BYTES - 3), 'y')
  expect(new TextEncoder().encode(text)).toHaveLength(MAX_DRAFT_BYTES)
  const drafts = createDrafts(); drafts.context(state.contextEpoch); drafts.set(state.selectedSession, text)
  expect(drafts.get(state.selectedSession)).toBe(text)
  expect(() => prepareAction('send', state.selectedSession, text, state.access!)).toThrow()
  expect(() => appendPromptReuse(state, reference, 'x'.repeat(MAX_DRAFT_BYTES - 2), 'y')).toThrow('65536')
  expect(drafts.get(state.selectedSession)).toBe(text)
})

test('invalid edited or existing text rejects without clipping either value', () => {
  const state = snapshot(), reference = preparePromptReuse(state).reference
  for (const edited of ['', ' \t\n', '\u0000', '\u202e', '\ud800', 'x'.repeat(MAX_DRAFT_BYTES + 1)]) {
    expect(() => appendPromptReuse(state, reference, 'existing', edited)).toThrow()
  }
  for (const existing of ['\u0000', '\ud800', 'x'.repeat(MAX_DRAFT_BYTES + 1)]) {
    expect(() => appendPromptReuse(state, reference, existing, 'addition')).toThrow()
  }
})

test('consumption of a submitted draft does not clear a newer manually edited draft', () => {
  const state = snapshot(), prepared = preparePromptReuse(state), drafts = createDrafts()
  drafts.context(state.contextEpoch)
  const submitted = appendPromptReuse(state, prepared.reference, '', prepared.text)
  drafts.set(state.selectedSession, submitted + '\nnew requirement')
  expect(drafts.consume(state.selectedSession, submitted)).toBe(false)
  expect(drafts.get(state.selectedSession)).toBe(submitted + '\nnew requirement')
})

test('helpers are pure and never fetch, send or import pending, receipt and arbitrary snapshot fields', () => {
  const state = snapshot(), extra = { draft: 'private-draft', token: 'private-token' }
  Object.assign(state, extra)
  state.receipt = { id: 'private-receipt', session_id: state.selectedSession, status: 'queued' }
  const before = JSON.stringify(state), request = spyOn(globalThis, 'fetch').mockRejectedValue(Error('unexpected network'))
  try {
    const prepared = preparePromptReuse(state)
    expect(promptReuseMatches(state, prepared.reference)).toBe(true)
    expect(appendPromptReuse(state, prepared.reference, 'latest', prepared.text)).toBe('latest\n\n' + prepared.text)
    expect(Object.keys(prepared.reference).sort()).toEqual(['prompt', 'target', 'turn'])
    for (const privateText of ['private-draft', 'private-token', 'private-receipt', access.context]) expect(prepared.text).not.toContain(privateText)
    expect(request).not.toHaveBeenCalled()
    expect(JSON.stringify(state)).toBe(before)
  } finally { request.mockRestore() }
})
