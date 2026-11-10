import { expect, test } from 'bun:test'
import { get } from 'svelte/store'
import { createTasks } from '../src/lib/tasks'
import { access, fixture, json, pause, turn } from './task-fixture'

test('selected turn state stays authoritative while browsing an older history page', async () => {
  let completed = false
  const tasks = createTasks({ fetcher: fixture((path) => {
    if (path.includes('/sessions/session-a/turns?')) {
      const older = path.includes('before=older')
      return json({ data: [older ? { ...turn, turn_id: 'old-turn', created_at: turn.created_at - 100 } : { ...turn, state: 'running' }], next_cursor: older ? '' : 'older' })
    }
    if (path === '/web/api/turns/turn-a') return json({ data: { ...turn, state: completed ? 'completed' : 'running', updated_at: turn.updated_at + Number(completed) } })
    return undefined
  }) })
  try {
    await tasks.refresh(); await tasks.open('session-a'); await tasks.turnHistory('older')
    expect(get(tasks).turns?.data[0].turn_id).toBe('old-turn')
    expect(get(tasks).currentTurn?.state).toBe('running')
    completed = true; await tasks.sync()
    expect(get(tasks).currentTurn).toMatchObject({ turn_id: 'turn-a', state: 'completed', prompt: 'inspect' })
    expect(get(tasks).turnHistoryCursor).toBe('older')
    expect(get(tasks).turns?.data[0].turn_id).toBe('old-turn')
  } finally { tasks.dispose() }
})

test('polling waits for the previous complete window and hidden panes abort reads', async () => {
  let calls = 0, request: AbortSignal | null | undefined, release: () => void = () => undefined
  const gate = new Promise<void>((resolve) => { release = resolve })
  const tasks = createTasks({ pollIntervalMs: 10, fetcher: fixture((path, init) => {
    if (path !== '/web/api/access') return undefined
    calls++; request = init?.signal
    return gate.then(() => json({ data: access }))
  }) })
  try {
    tasks.setActive(true); await pause(50)
    expect(calls).toBe(1)
    tasks.setActive(false)
    expect(request?.aborted).toBeTrue()
    release(); await pause(30)
    expect(calls).toBe(1)
    expect(get(tasks)).toMatchObject({ access: null, instances: [], busy: false, refreshing: false, watching: false })
  } finally { release(); tasks.dispose() }
})

test('revoking task access under the same login clears content and draft context', async () => {
  let revoked = false
  const tasks = createTasks({ fetcher: fixture((path) => path === '/web/api/access'
    ? json({ data: revoked ? { ...access, task_access: false, scopes: [] } : access }) : undefined) })
  try {
    await tasks.refresh(); await tasks.open('session-a')
    const epoch = get(tasks).contextEpoch
    revoked = true; await tasks.sync()
    expect(get(tasks)).toMatchObject({ detail: null, events: null, turns: null, currentTurn: null, selectedSession: '' })
    expect(get(tasks).contextEpoch).toBeGreaterThan(epoch)
  } finally { tasks.dispose() }
})

test('forget clears history, raises privacy epoch and cannot restart polling by itself', async () => {
  let calls = 0
  const tasks = createTasks({ pollIntervalMs: 10, fetcher: fixture(() => { calls++; return undefined }) })
  try {
    await tasks.refresh(); await tasks.open('session-a')
    const epoch = get(tasks).contextEpoch
    tasks.setActive(true); tasks.forget()
    const stopped = calls; await pause(40)
    expect(get(tasks)).toMatchObject({ selectedSession: '', selectedTurn: '', detail: null, receipt: null, watching: false })
    expect(get(tasks).contextEpoch).toBeGreaterThan(epoch)
    expect(calls).toBe(stopped)
  } finally { tasks.dispose() }
})
