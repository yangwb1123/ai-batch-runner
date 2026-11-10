import { expect, test } from 'bun:test'
import { projectResults } from '../src/lib/task-results'
import { exportTurnRecord, MAX_RECORD_BYTES } from '../src/lib/task-export'
import { initial, type Tasks } from '../src/lib/task-state'
import { eventPage, type Event } from '../src/lib/task-events'
import { access, detail, turn } from './task-fixture'

function row(cursor: number, kind: string, extra: Partial<Event> = {}): Event {
  return { cursor, event_id: `event-${cursor}`, turn_id: turn.turn_id, kind, text: '', truncated: false,
    chunk: '', chunk_index: null, chunk_count: null, created_at: 1700000000, ...extra }
}
function state(events: Event[] = []): Tasks {
  return { ...initial(), initialized: true, access: { ...access, mode: 'control' }, detail: { ...detail },
    currentTurn: { ...turn }, selectedSession: detail.session_id, selectedTurn: turn.turn_id,
    readAt: 1700000001000, events: { data: events, next_cursor: events.at(-1)?.cursor ?? 0 } }
}

test('results use typed events, never prose or a zero command exit as validation', () => {
  const result = projectResults(state([
    row(1, 'message.completed', { text: 'VERDICT: PASS; every test passed' }),
    row(2, 'command.started', { text: 'pytest', item_id: 'command-a' }),
    row(3, 'command.completed', { item_id: 'command-a', exit_code: 0 }),
    row(4, 'command.completed', { text: 'unknown exit' }),
    row(5, 'command.completed', { text: 'failed exit', exit_code: 2 }),
  ]))!
  expect(result.commands).toHaveLength(3)
  expect(result.commands.map((item) => [item.text, item.exit_code])).toEqual([
    ['pytest', 0], ['unknown exit', null], ['failed exit', 2],
  ])
  expect(result.validations).toEqual([])
  expect(result.gates).toEqual([])
  expect(result.goalAcceptance).toBe('not_independently_verified')
})

test('typed validation, gates, artifact metadata and partial windows stay explicit', () => {
  const snapshot = state([
    row(1, 'validation.started', { item_id: 'check', text: 'unit', state: 'running' }),
    row(2, 'validation.completed', { item_id: 'check', text: 'unit', state: 'passed', exit_code: 0 }),
    row(3, 'gate.verdict', { text: 'review', state: 'rejected', reason: 'missing evidence' }),
    row(4, 'artifact.created', { text: 'report', paths: ['report.txt'], state: 'recorded', sha256: 'a'.repeat(64), size_bytes: 16 }),
    row(5, 'file.changed', { paths: ['src/main.py', 'src/main.py'], truncated: true }),
  ])
  snapshot.eventsMore = snapshot.eventsTrimmed = true
  const result = projectResults(snapshot)!
  expect(result.validations).toHaveLength(1)
  expect(result.validations[0]?.state).toBe('passed')
  expect(result.gates[0]?.state).toBe('rejected')
  expect(result.files).toEqual(['src/main.py'])
  expect(result.artifacts[0]?.sha256).toBe('a'.repeat(64))
  expect(result.partial).toBe(true)
  expect(result.goalAcceptance).toBe('not_independently_verified')
})

test('mismatched selected task or event identity never produces a result or export', () => {
  const mutations: ((value: Tasks) => void)[] = [
    (value) => { value.selectedTurn = 'another-turn' },
    (value) => { value.currentTurn!.session_id = 'another-session' },
    (value) => { value.currentTurn!.instance_id = 'another-instance' },
    (value) => { value.selectedSession = 'another-session' },
    (value) => { value.events!.data[0]!.turn_id = 'another-turn' },
    (value) => { value.access = null },
    (value) => { value.access!.task_access = false },
  ]
  for (const mutate of mutations) {
    const snapshot = state([row(1, 'command.completed')]); mutate(snapshot)
    expect(projectResults(snapshot)).toBeNull()
    expect(() => exportTurnRecord(snapshot)).toThrow()
  }
})

test('export whitelists the current loaded window and excludes pending credentials and drafts', () => {
  const snapshot = state([row(1, 'message.completed', { text: 'reply' })])
  Object.assign(snapshot, { draft: 'unsent-secret', pending: { body: 'pending-secret', key: 'idempotency-secret' } })
  Object.assign(snapshot.currentTurn!, { secret: 'extra-turn-secret' })
  Object.assign(snapshot.events!.data[0]!, { secret: 'extra-event-secret' })
  snapshot.eventsMore = true
  const output = exportTurnRecord(snapshot)
  const record = JSON.parse(output.text)
  expect(record.schema).toBe('abr.turn-record.v1')
  expect(record.session.session_id).toBe(detail.session_id)
  expect(record.turn.turn_id).toBe(turn.turn_id)
  expect(record.replies[0].text).toBe('reply')
  expect(record.coverage.more_events_available).toBe(true)
  expect(record.coverage.scope).toBe('currently_loaded_events')
  expect(record.verification.goal_acceptance).toBe('not_independently_verified')
  for (const value of [access.context, 'unsent-secret', 'pending-secret', 'idempotency-secret', 'extra-turn-secret', 'extra-event-secret']) {
    expect(output.text).not.toContain(value)
  }
  expect(output.filename).toBe('abr-turn-turn-a.json')
})

test('export refuses stopped or unsettled reads and oversized serialized data', () => {
  for (const patch of [{ busy: true }, { refreshing: true }, { syncStopped: true }, { readAt: null }]) {
    expect(() => exportTurnRecord({ ...state(), ...patch })).toThrow()
  }
  const snapshot = state([row(1, 'message.completed', { text: 'x'.repeat(MAX_RECORD_BYTES) })])
  expect(() => exportTurnRecord(snapshot)).toThrow()
})

test('new evidence metadata is checked without silently accepting malformed positive evidence', () => {
  const page = (payload: Record<string, unknown>) => eventPage({ data: [{ cursor: 1, event_id: 'e', session_id: 's',
    turn_id: 't', kind: 'artifact.created', payload }], next_cursor: 1 }, 's', 0, 't')
  expect(page({ sha256: 'a'.repeat(64), size_bytes: 4, media_type: 'text/plain', evidence_partial: true,
    reason: 'metadata only' }).data[0]).toMatchObject({ sha256: 'a'.repeat(64), size_bytes: 4, evidence_partial: true })
  for (const payload of [{ sha256: 'invalid' }, { size_bytes: true }, { size_bytes: -1 }, { size_bytes: Infinity },
    { evidence_partial: 'false' }, { reason: [] }, { media_type: 'x'.repeat(129) }]) expect(() => page(payload)).toThrow()
})

test('new metadata limits do not narrow legacy terminal reason text', () => {
  const reason = 'x'.repeat(2048)
  const page = eventPage({ data: [{ cursor: 1, event_id: 'e', session_id: 's', turn_id: 't',
    kind: 'turn.failed', payload: { reason } }], next_cursor: 1 }, 's', 0, 't')
  expect(page.data[0]?.text).toBe(reason)
})

test('terminal turns expose missing activity completion instead of still running', () => {
  const snapshot = state([row(1, 'command.started', { item_id: 'cmd', text: 'unfinished' }),
    row(2, 'validation.started', { item_id: 'validation', text: 'unfinished validation', state: 'running' })])
  const result = projectResults(snapshot)!
  expect(result.partial).toBe(true)
  expect(result.commands[0]?.state).toBe('unknown')
  expect(result.validations[0]?.state).toBe('unknown')
  expect(JSON.parse(exportTurnRecord(snapshot).text).coverage.activity_completion_missing).toBe(true)
})

test('typed evidence cannot pass with a conflicting result or an inapplicable event kind', () => {
  for (const [kind, payload] of [
    ['validation.completed', { state: 'passed', exit_code: 7 }],
    ['validation.completed', { state: 'failed', exit_code: 0 }],
    ['validation.started', { state: 'passed' }],
    ['artifact.created', { state: 'passed' }],
    ['gate.verdict', {}],
  ] as const) {
    const page = eventPage({ data: [{ cursor: 1, event_id: 'e', session_id: 's', turn_id: 't', kind, payload }], next_cursor: 1 }, 's', 0, 't')
    expect(page.data[0]?.state).toBe('unknown')
    expect(page.data[0]?.evidence_partial).toBe(true)
  }
  expect(() => eventPage({ data: [{ cursor: 1, event_id: 'e', session_id: 's', turn_id: 't',
    kind: 'validation.completed', payload: { state: 'passed', truncated: 'true' } }], next_cursor: 1 }, 's', 0, 't')).toThrow()
})
