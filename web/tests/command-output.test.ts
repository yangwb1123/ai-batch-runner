import { expect, test } from 'bun:test'
import { eventPage, type Event } from '../src/lib/task-events'
import { projectCommandRows } from '../src/lib/command-results'
import { projectResults } from '../src/lib/task-results'
import { exportTurnRecord } from '../src/lib/task-export'
import { initial, type Tasks } from '../src/lib/task-state'
import { access, detail, turn } from './task-fixture'

function row(cursor: number, kind: string, extra: Partial<Event> = {}): Event {
  return { cursor, event_id: `e-${cursor}`, turn_id: turn.turn_id, kind, text: '', truncated: false,
    chunk: '', chunk_index: null, chunk_count: null, created_at: 1700000000, ...extra }
}
function page(kind: string, payload: unknown) {
  return eventPage({ data: [{ cursor: 1, event_id: 'e', session_id: detail.session_id,
    turn_id: turn.turn_id, kind, payload }], next_cursor: 1 }, detail.session_id, 0, turn.turn_id)
}
function state(events: Event[]): Tasks {
  return { ...initial(), initialized: true, access: { ...access, mode: 'control' }, detail, currentTurn: turn,
    selectedSession: detail.session_id, selectedTurn: turn.turn_id, readAt: 1700000001000,
    events: { data: events, next_cursor: events.at(-1)?.cursor ?? 0 } }
}

test('terminal output is typed, bounded and belongs only to command records', () => {
  expect(page('command.completed', { output_text: 'one\ntwo', output_truncated: false }).data[0])
    .toMatchObject({ output_text: 'one\ntwo', output_truncated: false })
  for (const payload of [{ output_text: 'x' }, { output_truncated: false },
    { output_text: [], output_truncated: false }, { output_text: 'x', output_truncated: 'false' },
    { output_text: '猫'.repeat(2731), output_truncated: false }]) {
    expect(() => page('command.output', payload)).toThrow()
  }
  expect(() => page('message.completed', { output_text: 'not a command', output_truncated: false })).toThrow()
  expect(() => page('command.output', { output_text: 'text', output_truncated: false, exit_code: 0 })).toThrow()
  expect(() => page('command.output', { output_text: 'text', output_truncated: false, state: 'completed' })).toThrow()
  expect(page('command.completed', {}).data[0]?.output_text).toBeUndefined()
})

test('Codex output survives completion and terminal snapshots replace instead of duplicate', () => {
  const rows = projectCommandRows([
    row(1, 'command.started', { item_id: 'a', text: 'pytest' }),
    row(2, 'command.output', { item_id: 'a', output_text: 'old', output_truncated: false }),
    row(3, 'command.output', { item_id: 'a', output_text: 'final', output_truncated: false }),
    row(4, 'command.completed', { item_id: 'a', text: 'pytest', exit_code: 7 }),
  ], true)
  expect(rows).toHaveLength(1)
  expect(rows[0]).toMatchObject({ kind: 'command.completed', text: 'pytest', exit_code: 7, output_text: 'final' })
})

test('late output cannot reopen completion and reused item start clears old output', () => {
  const events = [row(1, 'command.completed', { item_id: 'a', text: 'run', exit_code: 0 }),
    row(2, 'command.output', { item_id: 'a', output_text: 'final', output_truncated: false })]
  expect(projectCommandRows(events, true)[0]).toMatchObject({ kind: 'command.completed', exit_code: 0 })
  events.push(row(3, 'command.started', { item_id: 'a', text: 'new command' }))
  const item = projectCommandRows(events, true)[0]!
  expect(item.output_text).toBeUndefined()
  expect(item.exit_code).toBeNull()
  expect(item.evidence_partial).toBe(true)
})

test('unidentified output is independent; orphan output cannot imply completed execution', () => {
  const rows = projectCommandRows([row(1, 'command.completed', { text: 'run', exit_code: 0 }),
    row(2, 'command.output', { output_text: 'unattributed', output_truncated: false }),
    row(3, 'command.started', { item_id: 'a', text: 'active' })], true)
  expect(rows).toHaveLength(3)
  expect(rows[1]).toMatchObject({ kind: 'command.output', exit_code: null, evidence_partial: true })
  expect(rows[0]?.output_text).toBeUndefined()
})

test('a later missing exit code never inherits an earlier successful exit', () => {
  const rows = projectCommandRows([row(1, 'command.completed', { item_id: 'a', exit_code: 0 }),
    row(2, 'command.completed', { item_id: 'a', exit_code: null })], true)
  expect(rows[0]?.exit_code).toBeNull()
})

test('orphan output cannot declare success in an active or terminal turn', () => {
  for (const terminal of [false, true]) {
    const rows = projectCommandRows([row(1, 'command.output', { item_id: 'a', output_text: 'text',
      output_truncated: false, exit_code: 0, state: 'completed' })], terminal)
    expect(rows[0]?.exit_code).toBeNull()
    expect(rows[0]?.state).not.toBe('completed')
  }
})

test('empty output is distinct from missing and truncated output remains partial', () => {
  const result = projectResults(state([
    row(1, 'command.completed', { item_id: 'a', output_text: '', output_truncated: false, exit_code: 0 }),
    row(2, 'command.completed', { item_id: 'b', exit_code: 0 }),
    row(3, 'command.completed', { item_id: 'c', output_text: 'partial', output_truncated: true, exit_code: 1 }),
  ]))!
  expect(result.commands[0]?.output_text).toBe('')
  expect(result.commands[1]?.output_text).toBeUndefined()
  expect(result.partial).toBe(true)
  expect(result.validations).toEqual([])
})

test('command output projection keeps an overall UTF8 display bound', () => {
  const events = Array.from({ length: 100 }, (_, index) => row(index + 1, 'command.completed',
    { item_id: `cmd-${index}`, output_text: '猫'.repeat(2730), output_truncated: false }))
  const rows = projectCommandRows(events, true)
  expect(rows.reduce((total, item) => total + new TextEncoder().encode(item.output_text ?? '').length, 0)).toBeLessThanOrEqual(262144)
  expect(rows.some((item) => item.output_truncated)).toBe(true)
  expect(rows.every((item) => !item.output_text?.includes('\ufffd'))).toBe(true)
})

test('export includes only read command output and keeps provenance and partial coverage', () => {
  const snapshot = state([row(1, 'command.completed', { item_id: 'a', output_text: 'stored output', output_truncated: true })])
  const record = JSON.parse(exportTurnRecord(snapshot).text)
  expect(record.events[0]).toMatchObject({ output_text: 'stored output', output_truncated: true })
  expect(record.coverage.command_output_partial).toBe(true)
  expect(record.verification.goal_acceptance).toBe('not_independently_verified')
  expect(record.verification.file_contents).toBe('not_read_or_verified')
  snapshot.selectedTurn = 'other'
  expect(() => exportTurnRecord(snapshot)).toThrow()
})

test('complete exported output is distinct from a limited UI preview', () => {
  const events = Array.from({ length: 50 }, (_, index) => row(index + 1, 'command.completed',
    { item_id: `cmd-${index}`, output_text: 'x'.repeat(8192), output_truncated: false, exit_code: 0 }))
  const record = JSON.parse(exportTurnRecord(state(events)).text)
  expect(record.events.reduce((total: number, item: Event) => total + item.output_text!.length, 0)).toBe(409600)
  expect(record.coverage.command_output_partial).toBe(false)
  expect(record.coverage.command_output_display_limited).toBe(true)
})
