import { expect, test } from 'bun:test'
import { reviewRecords, reviewPage, reviewStatusLabel } from '../src/lib/result-review'
import { appendReference, prepareReference, referenceMatches } from '../src/lib/result-followup'
import { initial, type Tasks } from '../src/lib/task-state'
import { type Event, MAX_EVENTS } from '../src/lib/task-events'
import { access, detail, turn } from './task-fixture'

function event(cursor: number, kind: string, fields: Partial<Event> = {}): Event {
  return { cursor, event_id: `event-${cursor}`, turn_id: turn.turn_id, kind, text: '', truncated: false,
    chunk: '', chunk_index: null, chunk_count: null, created_at: 1700000000, ...fields }
}
function snapshot(events: Event[] = []): Tasks {
  return { ...initial(), initialized: true, access: { ...access, mode: 'control', scopes: [...access.scopes] }, detail: { ...detail },
    selectedSession: detail.session_id, selectedTurn: turn.turn_id, currentTurn: { ...turn }, readAt: 1700000001000,
    events: { data: events, next_cursor: events.at(-1)?.cursor ?? 0 } }
}
function failed() {
  return snapshot([event(1, 'command.completed', { item_id: 'failing', text: 'pytest review-target', exit_code: 7,
    output_text: '<script>doNotExecute()</script>\nFAIL: expected a, observed b', output_truncated: false })])
}

test('a failure after the 64th command remains searchable and pageable in the loaded window', () => {
  const state = snapshot(Array.from({ length: 95 }, (_, i) => event(i + 1, 'command.completed', {
    item_id: `cmd-${i}`, text: `command ${i}`, exit_code: i === 79 ? 9 : 0,
    output_text: i === 79 ? 'literal_%_failure in more than 64 commands' : '', output_truncated: false,
  })))
  const review = reviewRecords(state)!
  expect(review.records).toHaveLength(95)
  expect(review.counts.failure).toBe(1); expect(review.counts.attention).toBe(1)
  expect(reviewPage(review.records).rows[0]!.key).toBe('command:item:cmd-79')
  expect(reviewPage(review.records, '_%_', 'all').rows.map((row) => row.key)).toEqual(['command:item:cmd-79'])
  const second = reviewPage(review.records, '', 'all', 2)
  expect(second.rows).toHaveLength(20); expect(second.total).toBe(95); expect(second.pages).toBe(5)
  expect(reviewPage(review.records, '', 'all', 99).page).toBe(5)
})

test('plain prose and zero command exit do not produce validation or goal acceptance', () => {
  const state = snapshot([event(1, 'message.completed', { text: 'VERDICT: PASS and all tests passed' }),
    event(2, 'command.completed', { exit_code: 0, text: 'echo PASS' })])
  const view = reviewRecords(state)!
  expect(view.records).toHaveLength(1); expect(view.counts.success).toBe(1)
  expect(view.records[0]!.category).toBe('command')
  expect(view).not.toHaveProperty('accepted'); expect(view).not.toHaveProperty('goalAcceptance', 'passed')
  expect(reviewPage(view.records).total).toBe(0)
  expect(reviewStatusLabel('success')).toBe('报告成功')
})

test('unknown, contradictory and incomplete positive records stay in attention without becoming success', () => {
  const state = snapshot([
    event(1, 'command.completed', { item_id: 'unknown-code', text: 'unknown' }),
    event(2, 'command.completed', { item_id: 'contradiction', exit_code: 0, state: 'failed' }),
    event(3, 'validation.completed', { item_id: 'incomplete', state: 'passed', exit_code: 0, evidence_partial: true }),
    event(4, 'gate.verdict', { item_id: 'denied', state: 'rejected', reason: 'missing checks' }),
    event(5, 'artifact.created', { item_id: 'file', state: 'recorded', truncated: true }),
  ])
  const view = reviewRecords(state)!
  expect(view.counts.success).toBe(0); expect(view.counts.attention).toBe(5)
  expect(view.counts.failure).toBe(1); expect(view.counts.unknown).toBe(4)
  expect(reviewPage(view.records, 'missing checks', 'failure').rows[0]!.key).toBe('gate:item:denied')
})

test('active and missing completions remain distinct and diagnostic-less failed turns use real turn identity', () => {
  const state = snapshot([event(1, 'command.started', { item_id: 'working', text: 'work' }),
    event(2, 'validation.started', { item_id: 'checking', state: 'running' })])
  state.currentTurn!.state = 'running'
  expect(reviewRecords(state)!.counts.running).toBe(2)
  state.currentTurn!.state = 'failed'
  const view = reviewRecords(state)!
  expect(view.counts.running).toBe(0); expect(view.records.filter((row) => row.category === 'turn')).toHaveLength(1)
  expect(view.records.find((row) => row.category === 'turn')).toMatchObject({ key: 'turn:turn-a', source: null, status: 'failure' })
  state.events!.data.push(event(3, 'turn.failed', { text: 'reported process failure' }))
  expect(reviewRecords(state)!.records.filter((row) => row.category === 'turn')).toHaveLength(0)
  expect(reviewRecords(state)!.records.find((row) => row.category === 'diagnostic')!.source!.event_id).toBe('event-3')
})

test('projection rejects mismatched authority, oversized windows and malformed query/page conditions', () => {
  for (const change of [(s: Tasks) => { s.selectedTurn = 'foreign' }, (s: Tasks) => { s.access = null },
    (s: Tasks) => { s.events!.data[0]!.turn_id = 'foreign' }, (s: Tasks) => { s.currentTurn!.instance_id = 'foreign' }]) {
    const state = failed(); change(state); expect(reviewRecords(state)).toBeNull()
  }
  expect(reviewRecords(snapshot(Array.from({ length: MAX_EVENTS + 1 }, (_, i) => event(i + 1, 'command.completed'))))).toBeNull()
  expect(reviewRecords(snapshot([event(1, 'command.completed', { text: 'x'.repeat(1024 * 1024) })]))).toBeNull()
  const rows = reviewRecords(failed())!.records
  for (const q of ['x'.repeat(129), '\u202e', '\n', '\u0085']) expect(() => reviewPage(rows, q)).toThrow()
  for (const page of [0, -1, 0.5, Infinity, NaN]) expect(() => reviewPage(rows, '', 'all', page)).toThrow()
  expect(() => reviewPage(rows, '', 'invented' as 'all')).toThrow()
})

test('reference templates contain only bounded visible source facts and never draft, pending or auth data', () => {
  const state = failed()
  Object.assign(state, { draft: 'PRIVATE DRAFT', token: 'PRIVATE TOKEN' })
  state.pending = { kind: 'send', target: detail.session_id, path: '/sessions/session-a/turns',
    body: 'PRIVATE PENDING', key: 'PRIVATE KEY', context: access.context }
  Object.assign(state.events!.data[0]!, { internal_secret: 'PRIVATE EXTRA' })
  const prepared = prepareReference(state, 'command:item:failing')
  expect(prepared.text).toContain('session-a'); expect(prepared.text).toContain('turn-a'); expect(prepared.text).toContain('event-1')
  expect(prepared.text).toContain('不是目标验收'); expect(prepared.text).toContain('<script>doNotExecute()</script>')
  expect(prepared.text).toContain('目标'); expect(prepared.text).toContain('范围'); expect(prepared.text).toContain('验收')
  for (const text of ['PRIVATE DRAFT', 'PRIVATE TOKEN', 'PRIVATE PENDING', 'PRIVATE KEY', 'PRIVATE EXTRA', access.context]) expect(prepared.text).not.toContain(text)
  expect(new TextEncoder().encode(prepared.text).length).toBeLessThanOrEqual(8192)
  expect(appendReference(state, prepared.reference, 'keep my draft', prepared.text)).toBe(`keep my draft\n\n${prepared.text}`)
  expect(state.pending!.body).toBe('PRIVATE PENDING')
})

test('reference fingerprints ignore ordinary read clocks but reject changed source facts and identities', () => {
  const state = failed(), prepared = prepareReference(state, 'command:item:failing')
  state.readAt! += 1000; state.refreshing = true
  expect(referenceMatches(state, prepared.reference)).toBe(true)
  expect(() => appendReference(state, prepared.reference, '', prepared.text)).toThrow()
  state.refreshing = false
  expect(appendReference(state, prepared.reference, '', prepared.text)).toBe(prepared.text)
  for (const change of [(s: Tasks) => { s.contextEpoch++ }, (s: Tasks) => { s.access!.context = 'b'.repeat(64) },
    (s: Tasks) => { s.events!.data[0]!.output_text = 'changed output' }, (s: Tasks) => { s.events!.data = [] },
    (s: Tasks) => { s.access!.scopes = s.access!.scopes.filter((scope) => scope !== 'agent.sessions:write') },
    (s: Tasks) => { s.selectedTurn = 'other' }, (s: Tasks) => { s.access!.scopes = ['agent.sessions:read'] }]) {
    const current = failed(); change(current)
    expect(referenceMatches(current, prepared.reference)).toBe(false)
    expect(() => appendReference(current, prepared.reference, '', prepared.text)).toThrow()
  }
})

test('draft availability, write scope, uncertain submissions and byte budgets fail without truncating existing draft', () => {
  const state = failed(), prepared = prepareReference(state, 'command:item:failing')
  for (const change of [(s: Tasks) => { s.busy = true }, (s: Tasks) => { s.refreshing = true },
    (s: Tasks) => { s.syncStopped = true }, (s: Tasks) => { s.readAt = null }, (s: Tasks) => { s.outcome = 'unknown' },
    (s: Tasks) => { s.detail!.status = 'closed' }, (s: Tasks) => { s.access!.scopes = s.access!.scopes.filter((scope) => scope !== 'agent.sessions:write') }]) {
    const current = failed(); change(current)
    expect(() => prepareReference(current, 'command:item:failing')).toThrow()
    expect(() => appendReference(current, prepared.reference, 'keep', prepared.text)).toThrow()
  }
  const existing = '原'.repeat(5460)
  expect(() => appendReference(state, prepared.reference, existing, 'append')).toThrow('16384')
  expect(existing).toHaveLength(5460)
  expect(appendReference(state, prepared.reference, '', 'x'.repeat(16384))).toHaveLength(16384)
  for (const text of ['', '   ', 'x'.repeat(16385), '\u202e', '\ud800']) expect(() => appendReference(state, prepared.reference, '', text)).toThrow()
})

test('template excerpts flag clipping and a turn-only reference never fabricates an event', () => {
  const state = failed(); state.events!.data[0]!.text = '🧪'.repeat(4096)
  state.events!.data[0]!.output_text = '\n'.repeat(8192)
  state.events!.data[0]!.reason = 'reason '.repeat(140)
  const prepared = prepareReference(state, 'command:item:failing')
  expect(new TextEncoder().encode(prepared.text).length).toBeLessThanOrEqual(8192)
  expect(prepared.text).toContain('"truncated": true')
  const empty = snapshot(); empty.currentTurn!.state = 'lost'
  const reference = prepareReference(empty, 'turn:turn-a')
  expect(reference.text).toContain('"source": null'); expect(reference.text).not.toContain('"event_id"')
  empty.currentTurn!.state = 'failed'
  expect(referenceMatches(empty, reference.reference)).toBe(false)
})

test('reference excerpts distinguish missing command output from provided empty output', () => {
  const state = failed()
  const quoted = () => {
    const text = prepareReference(state, 'command:item:failing').text
    return JSON.parse(text.slice(text.indexOf('{'), text.lastIndexOf('}') + 1)).source.projected.output
  }
  delete state.events!.data[0]!.output_text
  expect(quoted()).toEqual({ available: false, text: null, truncated: false })
  state.events!.data[0]!.output_text = ''
  expect(quoted()).toEqual({ available: true, text: '', truncated: false })
})

test('inapplicable positive states and contradictory validation codes never appear as successful review records', () => {
  const state = snapshot([event(1, 'artifact.created', { state: 'passed' }),
    event(2, 'validation.started', { state: 'passed' }),
    event(3, 'validation.completed', { state: 'passed', exit_code: 9 }),
    event(4, 'gate.verdict', { state: 'running' }),
    event(5, 'command.completed', { state: 'unknown', exit_code: 0 })])
  const view = reviewRecords(state)!
  expect(view.counts.success).toBe(0); expect(view.counts.unknown).toBe(5)
})

test('maximum valid Unicode identities still fit the template without shortening identity or hiding excerpt cuts', () => {
  const state = failed(), id = '界'.repeat(256)
  state.detail!.session_id = state.currentTurn!.session_id = state.selectedSession = id
  state.detail!.instance_id = state.currentTurn!.instance_id = id
  state.currentTurn!.turn_id = state.selectedTurn = state.events!.data[0]!.turn_id = id
  const source = state.events!.data[0]!
  Object.assign(source, { event_id: id, item_id: 'i'.repeat(256), text: 't'.repeat(16384),
    reason: 'r'.repeat(1024), output_text: 'o'.repeat(8192), paths: Array(16).fill('p'.repeat(512)) })
  const prepared = prepareReference(state, `command:item:${source.item_id}`)
  expect(new TextEncoder().encode(prepared.text).length).toBeLessThanOrEqual(8192)
  expect(prepared.text.match(new RegExp(id, 'g'))).toHaveLength(4)
  expect(prepared.text).toContain('"truncated": true')
})

test('grouped command references preserve actual started, completed and output event provenance', () => {
  const state = snapshot([
    event(1, 'command.started', { item_id: 'joined', text: 'pytest genuine command', state: 'running' }),
    event(2, 'command.completed', { item_id: 'joined', exit_code: 7 }),
    event(3, 'command.output', { item_id: 'joined', output_text: 'reported output tail', output_truncated: false }),
  ])
  const record = reviewRecords(state)!.records[0]!
  expect(record.sources.map((source) => [source.event_id, source.kind, source.exit_code])).toEqual([
    ['event-1', 'command.started', undefined], ['event-2', 'command.completed', 7], ['event-3', 'command.output', undefined],
  ])
  const prepared = prepareReference(state, 'command:item:joined')
  const quote = JSON.parse(prepared.text.slice(prepared.text.indexOf('{'), prepared.text.lastIndexOf('}') + 1))
  expect(quote.source.projection).toBe('grouped_reported_events')
  expect(quote.source).not.toHaveProperty('event_id'); expect(quote.source).not.toHaveProperty('kind')
  expect(quote.source.events).toEqual([
    { event_id: 'event-1', cursor: 1, kind: 'command.started', reported_state: 'running' },
    { event_id: 'event-2', cursor: 2, kind: 'command.completed', exit_code: 7 },
    { event_id: 'event-3', cursor: 3, kind: 'command.output' },
  ])
  expect(quote.source.projected.text.text).toBe('pytest genuine command')
  expect(quote.source.projected.exit_code).toBe(7)
  expect(quote.source.projected.output.text).toBe('reported output tail')
})

test('contributor identity includes category, legacy identity and every omitted source event', () => {
  const state = snapshot([event(1, 'command.completed', { item_id: 'shared', exit_code: 7 }),
    ...Array.from({ length: 12 }, (_, index) => event(index + 2, 'command.output', { item_id: 'shared',
      output_text: `output ${index}`, output_truncated: false })),
    event(14, 'validation.completed', { item_id: 'shared', state: 'failed', exit_code: 1 }),
    event(15, 'command.completed', { text: 'legacy one', exit_code: 8 }),
    event(16, 'command.completed', { text: 'legacy two', exit_code: 9 })])
  const records = reviewRecords(state)!.records
  expect(records.find((row) => row.key === 'command:item:shared')!.sources).toHaveLength(13)
  expect(records.find((row) => row.key === 'validation:item:shared')!.sources.map((row) => row.event_id)).toEqual(['event-14'])
  expect(records.find((row) => row.key === 'command:event:event-15')!.sources.map((row) => row.event_id)).toEqual(['event-15'])
  const prepared = prepareReference(state, 'command:item:shared')
  const quote = JSON.parse(prepared.text.slice(prepared.text.indexOf('{'), prepared.text.lastIndexOf('}') + 1))
  expect(quote.source.events_truncated).toBe(true)
  expect(quote.source.events.length).toBeLessThanOrEqual(8)
  const omitted = state.events!.data.find((row) => !quote.source.events.some((source: { event_id: string }) => source.event_id === row.event_id))!
  omitted.output_text = 'changed omitted contributor'
  expect(referenceMatches(state, prepared.reference)).toBe(false)
})
