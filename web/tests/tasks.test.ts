import { expect, test } from 'bun:test'
import { get } from 'svelte/store'
import { createTasks } from '../src/lib/tasks'
import { prepareAction, taskRequest } from '../src/lib/task-api'
import { accessRecord, eventPage } from '../src/lib/task-models'
import type { Fetcher } from '../src/lib/api'

const access = { mode: 'control', task_access: true, context: 'a'.repeat(64),
  scopes: ['agent.instances:read', 'agent.sessions:read', 'agent.sessions:write', 'agent.turns:cancel'] } as const
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } })
const signal = new AbortController().signal
const detail = { session_id: 'session-a', instance_id: 'alpha', name: 'task', project_id: 'fixed', status: 'idle',
  online: true, controllable: true, frozen: false, active_turn_id: '' }
const receipt = { request_id: 'request-a', instance_id: 'alpha', session_id: '', status: 'queued', name: 'task' }

function fixture(extra?: Fetcher): Fetcher {
  return async (url, options) => {
    const path = String(url)
    if (options?.method === 'POST' && extra) return extra(url, options)
    if (path === '/web/api/access') return json({ data: access })
    if (path.includes('/instances?')) return json({ data: [{ instance_id: 'alpha', online: true, session_capacity: 4 }] })
    if (path.includes('/task-sessions?')) return json({ data: [{ ...detail, updated_at: 1700000000, category: 'idle', latest_turn: null }], next_cursor: '' })
    if (path.includes('/sessions?')) return json({ data: [detail] })
    if (path.includes('/session-operations?')) return json({ data: [], next_cursor: '' })
    if (path.includes('/sessions/session-a/turns?')) return json({ data: [], next_cursor: '' })
    if (path === '/web/api/session-requests/request-a') return json({ data: receipt })
    if (path.includes('/events?')) return json({ data: [], next_cursor: 0 })
    if (path === '/web/api/sessions/session-a') return json({ data: detail })
    throw new Error('unexpected route')
  }
}

test('task access fails closed for contradictory or malformed authority', () => {
  expect(accessRecord(access).task_access).toBeTrue()
  for (const change of [{ context: '' }, { mode: 'unknown' }, { scopes: ['admin'] }, { mode: 'read_only' }, { task_access: 'yes' }]) {
    expect(() => accessRecord({ ...access, ...change })).toThrow()
  }
})

test('prompt and target are frozen, bounded in UTF8, with no browser bearer token', async () => {
  const action = prepareAction('send', 'session-a', 'inspect', accessRecord(access))
  let sent: RequestInit | undefined
  await taskRequest(action.path, signal, (async (_url, init) => { sent = init; return json({ data: {} }, 202) }) as Fetcher, action)
  expect(sent).toMatchObject({ credentials: 'same-origin', redirect: 'error', method: 'POST', body: '{"prompt":"inspect"}' })
  expect(sent?.headers).toMatchObject({ 'X-ABR-Context': access.context, 'Idempotency-Key': action.key })
  expect(JSON.stringify(sent)).not.toContain('Bearer')
  expect(Object.isFrozen(action)).toBeTrue()
  expect(() => prepareAction('send', 'session-a', '字'.repeat(6000), accessRecord(access))).toThrow()
  expect(() => prepareAction('create', '../alpha', 'task', accessRecord(access))).toThrow()
  expect(() => prepareAction('send', 'session-a', 'inspect', accessRecord({ ...access, scopes: access.scopes.slice(0, 2) }))).toThrow()
})

test('events are ordered and bound to the selected session, not HTML or claimed success', () => {
  const event = { cursor: 1, event_id: 'event-a', session_id: 'session-a', instance_id: 'alpha', turn_id: 'turn-a',
    kind: 'message.delta', payload: { text: '<img src=x onerror=alert(1)>', chunk_index: 0, chunk_count: 1, truncated: true } }
  const result = eventPage({ data: [event], next_cursor: 1 }, 'session-a', 0)
  expect(result.data[0].text).toContain('<img')
  expect(result.data[0].truncated).toBeTrue()
  expect(() => eventPage({ data: [event, event], next_cursor: 1 }, 'session-a', 0)).toThrow()
  expect(() => eventPage({ data: [event], next_cursor: 1 }, 'other', 0)).toThrow()
  expect(() => eventPage({ data: [], next_cursor: 9 }, 'session-a', 0)).toThrow()
})

test('lost response locks new mutations; explicit retry keeps target, body and key', async () => {
  const calls: RequestInit[] = []
  const tasks = createTasks({ fetcher: fixture(async (_url, options) => {
    calls.push(options!)
    if (calls.length === 1) throw new Error('private upstream diagnostic')
    return json({ data: receipt }, 202)
  }) })
  try {
    await tasks.refresh()
    await tasks.act('create', 'alpha', 'task')
    expect(get(tasks).outcome).toBe('unknown')
    expect(get(tasks).problem).not.toContain('private')
    await tasks.act('create', 'alpha', 'different')
    expect(calls.length).toBe(1)
    await tasks.retry()
    expect(calls.length).toBe(2)
    expect(calls[1].body).toBe(calls[0].body)
    expect(calls[1].headers).toEqual(calls[0].headers)
    expect(get(tasks).outcome).toBe('accepted')
    expect(get(tasks).receipt?.status).toBe('queued')
  } finally { tasks.dispose() }
})

test('new invalid input cannot inherit a previous accepted receipt', async () => {
  let calls = 0
  const tasks = createTasks({ fetcher: fixture(async () => { calls++; return json({ data: receipt }, 202) }) })
  try {
    await tasks.refresh()
    await tasks.act('create', 'alpha', 'task')
    expect(get(tasks).outcome).toBe('accepted')
    await tasks.act('create', 'alpha', '字'.repeat(100))
    expect(calls).toBe(1)
    expect(get(tasks)).toMatchObject({ outcome: 'rejected', receipt: null, pending: null })
    expect(get(tasks).problem).toContain('校验')
  } finally { tasks.dispose() }
})

test('pending lock prevents a second create and never retries automatically', async () => {
  let calls = 0
  const tasks = createTasks({ fetcher: fixture(async () => {
    calls++; await new Promise((resolve) => setTimeout(resolve, 10)); return json({ data: receipt }, 202)
  }) })
  try {
    await tasks.refresh()
    const first = tasks.act('create', 'alpha', 'task')
    await tasks.act('create', 'alpha', 'task')
    await first
    expect(calls).toBe(1)
  } finally { tasks.dispose() }
})

test('read failure clears sensitive facts; disposal prevents late publications', async () => {
  let failing = false
  const tasks = createTasks({ fetcher: async (url, options) => failing ? json({}, 403) : fixture()(url, options) })
  await tasks.refresh()
  await tasks.open('session-a')
  expect(get(tasks).detail?.session_id).toBe('session-a')
  failing = true
  await tasks.refresh()
  expect(get(tasks)).toMatchObject({ detail: null, access: null, sessions: [], events: null })
  tasks.dispose()
  await tasks.refresh()
  expect(get(tasks).access).toBeNull()
})

test('malformed accepted response remains unknown, never a fabricated success', async () => {
  const tasks = createTasks({ fetcher: fixture(async () => json({ data: { ...receipt, instance_id: 'beta' } }, 202)) })
  try {
    await tasks.refresh()
    await tasks.act('create', 'alpha', 'task')
    expect(get(tasks).outcome).toBe('unknown')
    expect(get(tasks).receipt).toBeNull()
  } finally { tasks.dispose() }
})

test('an unknown submission is never replayed under a different login context', async () => {
  let changed = false, posts = 0
  const base = fixture(async () => { posts++; throw new Error('lost') })
  const tasks = createTasks({ fetcher: async (url, options) => String(url) === '/web/api/access' && changed
    ? json({ data: { ...access, context: 'b'.repeat(64) } }) : base(url, options) })
  try {
    await tasks.refresh()
    await tasks.act('create', 'alpha', 'task')
    changed = true
    await tasks.retry()
    expect(posts).toBe(1)
    expect(get(tasks).outcome).toBe('unknown')
    expect(get(tasks).pending).toBeNull()
    await tasks.refresh()
    await tasks.act('create', 'alpha', 'different')
    expect(posts).toBe(1)
  } finally { tasks.dispose() }
})

test('whole write deadline expires despite continuous response bytes and never auto-replays', async () => {
  let calls = 0
  const tasks = createTasks({ timeoutMs: 15, fetcher: fixture(async (_url, init) => {
    calls++
    return new Response(new ReadableStream({ async pull(controller) {
      await new Promise((resolve) => setTimeout(resolve, 2))
      if (init?.signal?.aborted) { controller.error(new DOMException('aborted', 'AbortError')); return }
      controller.enqueue(new TextEncoder().encode(' '))
    } }), { status: 202, headers: { 'Content-Type': 'application/json' } })
  }) })
  try {
    await tasks.refresh()
    await tasks.act('create', 'alpha', 'task')
    expect(get(tasks)).toMatchObject({ busy: false, outcome: 'unknown', receipt: null })
    expect(calls).toBe(1)
  } finally { tasks.dispose() }
})

test('a disposed task pane cannot publish a late read or issue a write', async () => {
  let release: () => void = () => undefined
  const waiting = new Promise<void>((resolve) => { release = resolve })
  let calls = 0
  const tasks = createTasks({ fetcher: async (url, options) => { calls++; await waiting; return fixture()(url, options) } })
  const pending = tasks.refresh()
  tasks.dispose()
  release()
  await pending
  await tasks.act('create', 'alpha', 'task')
  // Access is read before any sensitive list; disposal cancels that whole read window.
  expect(calls).toBe(1)
  expect(get(tasks)).toMatchObject({ access: null, instances: [], detail: null, receipt: null })
})
