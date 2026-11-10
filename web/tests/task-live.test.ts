import { expect, test } from 'bun:test'
import { get } from 'svelte/store'
import { createTasks } from '../src/lib/tasks'
import { access, detail, fixture, json, pause, turn } from './task-fixture'

test('opening a long session selects its latest turn before reading output', async () => {
  const calls: string[] = []
  const tasks = createTasks({ fetcher: fixture((path) => { calls.push(path); return undefined }) })
  try {
    await tasks.refresh(); await tasks.open('session-a')
    expect(get(tasks)).toMatchObject({ selectedSession: 'session-a', selectedTurn: 'turn-a' })
    expect(get(tasks).turns?.data[0].prompt).toBe('inspect')
    expect(calls.some((path) => path.includes('/events?') && path.includes('turn_id=turn-a'))).toBeTrue()
  } finally { tasks.dispose() }
})

test('read refresh keeps selected identity and detail until it has a replacement', async () => {
  let hold = false, release: () => void = () => undefined
  const gate = new Promise<void>((resolve) => { release = resolve })
  const tasks = createTasks({ fetcher: fixture((path) => hold && path === '/web/api/access'
    ? gate.then(() => json({ data: access })) : undefined) })
  try {
    await tasks.refresh(); await tasks.open('session-a'); hold = true
    const refresh = tasks.refresh()
    expect(get(tasks).selectedSession).toBe('session-a')
    expect(get(tasks).detail?.session_id).toBe('session-a')
    release(); await refresh
    expect(get(tasks).selectedTurn).toBe('turn-a')
  } finally { release(); tasks.dispose() }
})

test('accepted send opens returned turn automatically without a second POST', async () => {
  let posts = 0
  const tasks = createTasks({ fetcher: fixture((path, init) => {
    if (posts && path === '/web/api/sessions/session-a') return json({ data: { ...detail, active_turn_id: 'turn-new' } })
    if (path === '/web/api/turns/turn-new') return json({ data: { ...turn, turn_id: 'turn-new', state: 'queued' } })
    if (path.includes('/turns?') && new URL(path, 'https://abr.test').searchParams.get('turn_id') === 'turn-new') {
      return json({ data: [{ ...turn, turn_id: 'turn-new', state: 'queued', prompt: 'next task' }], next_cursor: '' })
    }
    if (init?.method !== 'POST') return undefined
    posts++; return json({ data: { turn_id: 'turn-new', session_id: 'session-a', state: 'queued' } }, 202)
  }) })
  try {
    await tasks.refresh(); await tasks.open('session-a'); await tasks.act('send', 'session-a', 'next task')
    expect(get(tasks)).toMatchObject({ outcome: 'accepted', selectedSession: 'session-a', selectedTurn: 'turn-new' })
    expect(get(tasks).detail?.session_id).toBe('session-a')
    expect(get(tasks).currentTurn?.prompt).toBe('next task')
    expect(posts).toBe(1)
  } finally { tasks.dispose() }
})

test('unknown writes stay locked after successful background reads', async () => {
  let posts = 0
  const tasks = createTasks({ fetcher: fixture((_path, init) => {
    if (init?.method !== 'POST') return undefined
    posts++; return Promise.reject(new Error('lost response'))
  }) })
  try {
    await tasks.refresh(); await tasks.act('send', 'session-a', 'do once')
    const pending = get(tasks).pending
    await tasks.sync(); await tasks.act('send', 'session-a', 'do twice')
    expect(get(tasks).outcome).toBe('unknown')
    expect(get(tasks).pending).toBe(pending)
    expect(posts).toBe(1)
  } finally { tasks.dispose() }
})

test('identity change clears selected content and advances the draft privacy epoch', async () => {
  let context = access.context
  const tasks = createTasks({ fetcher: fixture((path) => path === '/web/api/access' ? json({ data: { ...access, context } }) : undefined) })
  try {
    await tasks.refresh(); await tasks.open('session-a')
    const epoch = get(tasks).contextEpoch
    context = 'b'.repeat(64); await tasks.sync()
    expect(get(tasks).contextEpoch).toBeGreaterThan(epoch)
    expect(get(tasks)).toMatchObject({ selectedSession: '', selectedTurn: '', detail: null, events: null })
  } finally { tasks.dispose() }
})

test('active polling reads only, stops after three failed windows, and disposes its timer', async () => {
  let reads = 0, posts = 0
  const tasks = createTasks({ pollIntervalMs: 10, fetcher: async (_url, init) => {
    if (init?.method === 'POST') posts++
    reads++; return json({}, 503)
  } })
  try {
    tasks.setActive(true)
    for (let attempt = 0; attempt < 30 && !get(tasks).syncStopped; attempt++) await pause()
    expect(get(tasks).syncStopped).toBeTrue()
    expect(reads).toBe(3); expect(posts).toBe(0)
    tasks.setActive(false)
    const stopped = reads; await pause(30)
    expect(reads).toBe(stopped)
  } finally { tasks.dispose() }
})
