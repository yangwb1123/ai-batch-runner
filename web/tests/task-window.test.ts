import { expect, test } from 'bun:test'
import { get } from 'svelte/store'
import { createTasks } from '../src/lib/tasks'
import { eventPage, turnPage } from '../src/lib/task-models'
import { MAX_EVENT_BYTES } from '../src/lib/task-events'
import { turnStatusRecord } from '../src/lib/task-history'
import { access, event, eventPages, fixture, json, turn } from './task-fixture'

test('event paging appends three bounded pages and explicitly reports trimmed history', async () => {
  const tasks = createTasks({ fetcher: fixture(eventPages(700)) })
  try {
    await tasks.refresh(); await tasks.open('session-a')
    expect(get(tasks).events?.data.length).toBe(150)
    expect(get(tasks).eventsMore).toBeTrue()
    await tasks.loadEvents(); await tasks.loadEvents(); await tasks.loadEvents()
    expect(get(tasks).events?.data.length).toBe(500)
    expect(get(tasks).events?.data[0].cursor).toBe(101)
    expect(get(tasks).eventsTrimmed).toBeTrue()
    await tasks.loadEvents()
    expect(get(tasks).events?.next_cursor).toBe(700)
    expect(get(tasks).eventsMore).toBeFalse()
  } finally { tasks.dispose() }
})

test('event memory is bounded by bytes as well as event count', async () => {
  const tasks = createTasks({ fetcher: fixture(eventPages(100, 'x'.repeat(16384))) })
  try {
    await tasks.refresh(); await tasks.open('session-a')
    const state = get(tasks)
    expect(state.eventsTrimmed).toBeTrue()
    expect(state.events?.next_cursor).toBe(100)
    const bytes = state.events!.data.reduce((sum, value) => sum + new TextEncoder().encode(JSON.stringify(value)).length, 0)
    expect(bytes).toBeLessThanOrEqual(MAX_EVENT_BYTES)
  } finally { tasks.dispose() }
})

test('confirmed create request opens actual returned session using only GET followups', async () => {
  let posts = 0
  const queued = { request_id: 'request-a', instance_id: 'alpha', session_id: '', status: 'queued' }
  const tasks = createTasks({ fetcher: fixture((path, init) => {
    if (init?.method === 'POST') { posts++; return json({ data: queued }, 202) }
    if (path === '/web/api/session-requests/request-a') return json({ data: { ...queued, session_id: 'session-a', status: 'created' } })
    return undefined
  }) })
  try {
    await tasks.refresh(); await tasks.act('create', 'alpha', 'new task')
    expect(get(tasks)).toMatchObject({ selectedSession: 'session-a', selectedTurn: 'turn-a', tracking: null, outcome: 'accepted' })
    expect(get(tasks).detail?.session_id).toBe('session-a')
    expect(posts).toBe(1)
  } finally { tasks.dispose() }
})

test('read window cannot publish a mixture of two login contexts', async () => {
  let calls = 0
  const tasks = createTasks({ fetcher: fixture((path) => path === '/web/api/access'
    ? json({ data: { ...access, context: ++calls === 1 ? access.context : 'b'.repeat(64) } }) : undefined) })
  try {
    await tasks.refresh()
    expect(get(tasks)).toMatchObject({ access: null, instances: [], sessions: [], detail: null, syncStopped: true })
    expect(get(tasks).contextEpoch).toBeGreaterThan(0)
  } finally { tasks.dispose() }
})

test('turn history is session bound, bounded, sorted, and preserves the original prompt', () => {
  expect(turnPage({ data: [turn], next_cursor: '' }, 'session-a').data[0].prompt).toBe('inspect')
  expect(() => turnPage({ data: [turn], next_cursor: '' }, 'other')).toThrow()
  expect(() => turnPage({ data: [{ ...turn, prompt: '字'.repeat(6000) }], next_cursor: '' }, 'session-a')).toThrow()
  expect(() => turnPage({ data: [turn, { ...turn, turn_id: 'new', created_at: turn.created_at + 1 }], next_cursor: '' }, 'session-a')).toThrow()
  expect(() => turnPage({ data: [turn], next_cursor: 'same' }, 'session-a', 'same')).toThrow()
})

test('event metadata preserves safe item identity and rejects another turn or invalid chunks', () => {
  const row = event(1), payload = { ...row.payload, item_id: 'opaque-item', exit_code: 1, paths: ['src/main.py'] }
  const parsed = eventPage({ data: [{ ...row, payload }], next_cursor: 1 }, 'session-a', 0, 'turn-a')
  expect(parsed.data[0]).toMatchObject({ item_id: 'opaque-item', chunk_index: 0, chunk_count: 1, exit_code: 1, paths: ['src/main.py'] })
  expect(eventPage({ data: [{ ...row, payload: { ...payload, item_id: 'message/1' } }], next_cursor: 1 }, 'session-a', 0).data[0].item_id).toBe('message/1')
  expect(() => eventPage({ data: [row], next_cursor: 1 }, 'session-a', 0, 'other')).toThrow()
  expect(() => eventPage({ data: [{ ...row, payload: { ...payload, chunk_index: 1 } }], next_cursor: 1 }, 'session-a', 0)).toThrow()
})

test('selected turn status cannot substitute a different turn, session or invalid state', () => {
  expect(turnStatusRecord(turn, 'session-a', 'turn-a').state).toBe('completed')
  expect(() => turnStatusRecord({ ...turn, turn_id: 'other' }, 'session-a', 'turn-a')).toThrow()
  expect(() => turnStatusRecord(turn, 'other', 'turn-a')).toThrow()
  expect(() => turnStatusRecord({ ...turn, state: 'claimed-success' }, 'session-a', 'turn-a')).toThrow()
})
