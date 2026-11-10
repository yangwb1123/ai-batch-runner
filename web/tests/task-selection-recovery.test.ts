import { expect, test } from 'bun:test'
import { get } from 'svelte/store'
import { createTasks } from '../src/lib/tasks'
import { fixture, json } from './task-fixture'

test('missing session deep link clears only the selection and leaves the catalog usable', async () => {
  const tasks = createTasks({ fetcher: fixture((path) => path === '/web/api/sessions/missing-session' ? json({}, 404) : undefined) })
  try {
    await tasks.refresh(); await tasks.open('missing-session')
    expect(get(tasks).selectedSession).toBe('')
    expect(get(tasks).problem).toContain('不存在')
    expect(get(tasks).access?.task_access).toBeTrue()
    await tasks.refresh(); await tasks.open('session-a')
    expect(get(tasks).detail?.session_id).toBe('session-a')
    expect(get(tasks).catalog?.data).toHaveLength(1)
  } finally { tasks.dispose() }
})

test('missing selection never clears or replays an uncertain submission', async () => {
  let posts = 0
  const tasks = createTasks({ fetcher: fixture((path, init) => {
    if (init?.method === 'POST') { posts++; return Promise.reject(new Error('unknown receipt')) }
    return path === '/web/api/sessions/missing-session' ? json({}, 404) : undefined
  }) })
  try {
    await tasks.refresh(); await tasks.act('send', 'session-a', 'submit once'); await tasks.sync()
    const pending = get(tasks).pending
    await tasks.open('missing-session')
    expect(get(tasks)).toMatchObject({ selectedSession: '', outcome: 'unknown' })
    expect(get(tasks).pending).toBe(pending)
    await tasks.act('send', 'session-a', 'do not resubmit')
    expect(posts).toBe(1)
  } finally { tasks.dispose() }
})

test('a missing catalog endpoint is still an error, not an empty success', async () => {
  const tasks = createTasks({ fetcher: fixture((path) => path.startsWith('/web/api/task-sessions?') ? json({}, 404) : undefined) })
  try {
    await tasks.refresh()
    expect(get(tasks)).toMatchObject({ catalog: null, access: null, sessions: [] })
    expect(get(tasks).problem).not.toBe('')
  } finally { tasks.dispose() }
})

test('a malformed null success is not treated as a missing selected task', async () => {
  const tasks = createTasks({ fetcher: fixture((path) => path === '/web/api/sessions/session-a' ? json(null) : undefined) })
  try {
    await tasks.refresh(); await tasks.open('session-a')
    expect(get(tasks)).toMatchObject({ access: null, detail: null, selectedSession: 'session-a' })
    expect(get(tasks).problem).toContain('校验')
  } finally { tasks.dispose() }
})
