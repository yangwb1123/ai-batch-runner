import { expect, test } from 'bun:test'
import { get } from 'svelte/store'
import { createTasks } from '../src/lib/tasks'
import { projectConversation } from '../src/lib/conversation'
import { appendEvents, MAX_EVENT_BYTES } from '../src/lib/task-events'
import { access, detail, fixture, json, pause, turn } from './task-fixture'

function recapFixture(override: (path: string, init?: RequestInit) => Response | Promise<Response> | undefined = () => undefined,
  timeoutMs = 12000) {
  const rows = Array.from({ length: 12 }, (_, index) => ({ ...turn, turn_id: `round-${20 - index}`,
    created_at: turn.created_at - index, updated_at: turn.updated_at - index, prompt: `instruction ${20 - index}` }))
  const events = new Map<string, ReturnType<typeof output>[]>()
  const requests: { path: string; method?: string }[] = []
  const tasks = createTasks({ timeoutMs, fetcher: fixture((path, init) => {
    requests.push({ path, method: init?.method })
    const custom = override(path, init)
    if (custom) return custom
    const url = new URL(path, 'https://abr.test')
    const query = new URLSearchParams(url.search)
    if (url.pathname === '/web/api/sessions/session-a/turns') {
      const id = query.get('turn_id'), search = query.get('q') || '', state = query.get('state') || 'all'
      const matching = rows.filter((row) => id ? row.turn_id === id : row.prompt.includes(search) && (state === 'all' || row.state === state))
      const start = Number((query.get('before') || 'p0').slice(1)), end = start + Number(query.get('limit') || 20)
      return json({ data: matching.slice(start, end), next_cursor: !id && end < matching.length ? `p${end}` : '' })
    }
    if (url.pathname.startsWith('/web/api/turns/')) {
      const found = rows.find((row) => row.turn_id === url.pathname.split('/').at(-1))
      return found ? json({ data: found }) : json({}, 404)
    }
    if (url.pathname === '/web/api/sessions/session-a/events') {
      const after = Number(query.get('after')), data = (events.get(query.get('turn_id') || '') || []).filter((row) => row.cursor > after).slice(0, 50)
      return json({ data, next_cursor: data.at(-1)?.cursor ?? after })
    }
  }) })
  return { tasks, rows, events, requests, ready: async () => { await tasks.refresh(); await tasks.open('session-a') } }
}
function output(id: string, cursor = 1, text = `reply ${id}`) {
  return { session_id: 'session-a', turn_id: id, event_id: `${id}-${cursor}`, cursor,
    kind: 'message.completed', payload: { text, item_id: 'shared-item', chunk_index: 0, chunk_count: 1 } }
}

test('recap explicitly reads five bound rounds without changing the live selection or searching history', async () => {
  const data = recapFixture()
  try {
    data.rows.slice(0, 5).forEach((row) => data.events.set(row.turn_id, [output(row.turn_id)]))
    await data.ready()
    await data.tasks.turnSearch({ q: 'instruction 10', state: 'completed' })
    const before = get(data.tasks)
    expect(before.recap).toBeNull()
    data.requests.length = 0
    await pause()
    await data.tasks.recapRead('latest')
    const after = get(data.tasks)
    expect(after.recap?.rows.map((row) => row.turn.turn_id)).toEqual(data.rows.slice(0, 5).map((row) => row.turn_id))
    expect(after).toMatchObject({ selectedTurn: before.selectedTurn, turnMode: 'live', turnQuery: before.turnQuery, recapPage: 1, recapCursor: '' })
    expect(after.turns).toBe(before.turns)
    expect(after.events).toBe(before.events)
    expect(after.readAt).toBe(before.readAt)
    expect(after.recap!.readAt).toBeGreaterThan(before.readAt!)
    expect(after.recap?.rows.map((row) => projectConversation(row.events.data).messages[0].text)).toEqual(data.rows.slice(0, 5).map((row) => `reply ${row.turn_id}`))
    expect(data.requests.every((request) => !request.method)).toBeTrue()
    expect(data.requests.filter((request) => request.path.includes('/turns?')).map((request) => request.path)).toEqual(['/web/api/sessions/session-a/turns?limit=5&before='])
  } finally { data.tasks.dispose() }
})

test('older, newer and refresh replace the recap window without moving its cursor on refresh', async () => {
  const data = recapFixture()
  try {
    await data.ready(); await data.tasks.recapRead('latest'); await data.tasks.recapRead('older')
    expect(get(data.tasks)).toMatchObject({ recapPage: 2, recapCursor: 'p5' })
    expect(get(data.tasks).recap?.rows[0].turn.turn_id).toBe('round-15')
    await data.tasks.recapRead('refresh')
    expect(get(data.tasks)).toMatchObject({ recapPage: 2, recapCursor: 'p5' })
    await data.tasks.recapRead('older')
    expect(get(data.tasks).recap?.rows).toHaveLength(2)
    expect(get(data.tasks).recap?.next_cursor).toBe('')
    await data.tasks.recapRead('newer')
    expect(get(data.tasks)).toMatchObject({ recapPage: 2, recapCursor: 'p5' })
    await data.tasks.recapRead('latest')
    expect(get(data.tasks)).toMatchObject({ recapPage: 1, recapCursor: '', recapTrail: [] })
  } finally { data.tasks.dispose() }
})

test('background live following does not refetch or replace the explicitly read recap snapshot', async () => {
  const data = recapFixture()
  try {
    await data.ready(); await data.tasks.recapRead('latest')
    const page = get(data.tasks).recap
    data.rows.unshift({ ...turn, turn_id: 'fresh-turn', created_at: turn.created_at + 1, updated_at: turn.updated_at + 1 })
    data.requests.length = 0
    await data.tasks.sync()
    expect(get(data.tasks).selectedTurn).toBe('fresh-turn')
    expect(get(data.tasks).recap).toBe(page)
    expect(data.requests.filter((request) => request.path.includes('limit=5&'))).toHaveLength(0)
  } finally { data.tasks.dispose() }
})

test('five noisy rounds retain at most one MiB and preserve per-round unread and trimmed flags', async () => {
  const data = recapFixture()
  try {
    data.rows.slice(0, 5).forEach((row) => data.events.set(row.turn_id,
      Array.from({ length: 120 }, (_, index) => output(row.turn_id, index + 1, '字'.repeat(4000)))))
    await data.ready(); data.requests.length = 0
    await data.tasks.recapRead('latest')
    const rows = get(data.tasks).recap!.rows
    const bytes = rows.reduce((sum, row) => sum + row.events.data.reduce((size, event) => size + new TextEncoder().encode(JSON.stringify(event)).length, 0), 0)
    expect(bytes).toBeLessThanOrEqual(1024 * 1024)
    expect(rows.reduce((sum, row) => sum + row.events.data.length, 0)).toBeLessThanOrEqual(500)
    expect(rows.every((row) => row.more && row.trimmed && row.events.next_cursor === 100)).toBeTrue()
    expect(data.requests.filter((request) => request.path.includes('/events?'))).toHaveLength(10)
  } finally { data.tasks.dispose() }
})

test('read-only permission can read recap without a write scope', async () => {
  const data = recapFixture((path) => path === '/web/api/access' ? json({ data: { ...access, scopes: access.scopes.filter((scope) => !scope.endsWith(':write') && !scope.endsWith(':cancel')) } }) : undefined)
  try {
    await data.ready(); await data.tasks.recapRead('latest')
    expect(get(data.tasks).recap?.rows).toHaveLength(5)
    expect(data.requests.every((request) => !request.method)).toBeTrue()
  } finally { data.tasks.dispose() }
})

test('recap preserves unknown submission identity and makes no extra POST', async () => {
  const data = recapFixture((_path, init) => init?.method === 'POST' ? Promise.reject(new Error('lost receipt')) : undefined)
  try {
    await data.ready(); await data.tasks.act('send', 'session-a', 'keep exact original body')
    const pending = get(data.tasks).pending
    await data.tasks.refresh(); await data.tasks.recapRead('latest')
    expect(get(data.tasks).outcome).toBe('unknown')
    expect(get(data.tasks).pending).toBe(pending)
    expect(pending?.body).toBe('{"prompt":"keep exact original body"}')
    expect(get(data.tasks).recap?.rows).toHaveLength(5)
    expect(data.requests.filter((request) => request.method === 'POST')).toHaveLength(1)
  } finally { data.tasks.dispose() }
})

test('same-session fixed selection and return to live retain the completed recap snapshot', async () => {
  const data = recapFixture()
  try {
    await data.ready(); await data.tasks.recapRead('latest'); await data.tasks.recapRead('older')
    const page = get(data.tasks).recap
    await data.tasks.selectTurn('round-10')
    expect(get(data.tasks)).toMatchObject({ turnMode: 'fixed', selectedTurn: 'round-10', recapCursor: 'p5', recapPage: 2 })
    expect(get(data.tasks).recap).toBe(page)
    await data.tasks.returnToLive()
    expect(get(data.tasks).turnMode).toBe('live')
    expect(get(data.tasks).recap).toBe(page)
    data.tasks.setActive(false)
    expect(get(data.tasks)).toMatchObject({ recap: null, recapPage: 1, recapTrail: [], recapCursor: '' })
  } finally { data.tasks.dispose() }
})

test('navigation cancels an in-flight recap and late bytes cannot overwrite the new fixed turn', async () => {
  let hold = false, release: ((response: Response) => void) | undefined, signal: AbortSignal | undefined
  const data = recapFixture((path, init) => {
    if (hold && path.includes('/events?limit=50&turn_id=round-20&after=0')) {
      signal = init?.signal as AbortSignal
      return new Promise<Response>((resolve) => { release = resolve })
    }
  })
  try {
    await data.ready(); hold = true
    const reading = data.tasks.recapRead('latest'); await pause()
    expect(get(data.tasks)).toMatchObject({ busy: false, refreshing: true })
    expect(release).toBeDefined()
    await data.tasks.selectTurn('round-10')
    expect(signal?.aborted).toBeTrue()
    expect(get(data.tasks)).toMatchObject({ selectedTurn: 'round-10', turnMode: 'fixed', recap: null })
    release!(json({ data: [output('round-20')], next_cursor: 1 })); await reading
    expect(get(data.tasks)).toMatchObject({ selectedTurn: 'round-10', turnMode: 'fixed', recap: null })
  } finally { release?.(json({ data: [], next_cursor: 0 })); data.tasks.dispose() }
})

test('closing authorization change rejects the entire recap without publishing any rows', async () => {
  let armed = false, checks = 0
  const data = recapFixture((path) => armed && path === '/web/api/access'
    ? json({ data: { ...access, context: ++checks === 1 ? access.context : 'b'.repeat(64) } }) : undefined)
  try {
    await data.ready(); const epoch = get(data.tasks).contextEpoch
    armed = true; data.requests.length = 0
    await data.tasks.recapRead('latest')
    expect(data.requests.some((request) => request.path.includes('/events?'))).toBeTrue()
    expect(get(data.tasks)).toMatchObject({ recap: null, access: null, selectedSession: '', syncStopped: true })
    expect(get(data.tasks).contextEpoch).toBeGreaterThan(epoch)
  } finally { data.tasks.dispose() }
})

test('first authorization change cannot continue reading a captured old-session target', async () => {
  let changed = false
  const data = recapFixture((path) => changed && path === '/web/api/access'
    ? json({ data: { ...access, context: 'b'.repeat(64) } }) : undefined)
  try {
    await data.ready(); changed = true; data.requests.length = 0
    await data.tasks.recapRead('latest')
    expect(get(data.tasks)).toMatchObject({ selectedSession: '', recap: null })
    expect(data.requests.every((request) => request.path === '/web/api/access')).toBeTrue()
  } finally { data.tasks.dispose() }
})

test('denied reads and ordinary failed reads clear recap while retaining ordinary unknown pending', async () => {
  let denied = false, failRead = false
  const data = recapFixture((path, init) => {
    if (init?.method === 'POST') return Promise.reject(new Error('lost receipt'))
    if (denied && path === '/web/api/access') return json({}, 403)
    if (failRead && path.includes('/session-operations?')) return json({}, 500)
  })
  try {
    await data.ready(); await data.tasks.act('send', 'session-a', 'pending')
    const pending = get(data.tasks).pending
    await data.tasks.refresh(); await data.tasks.recapRead('latest')
    failRead = true; await data.tasks.refresh()
    expect(get(data.tasks).recap).toBeNull()
    expect(get(data.tasks).pending).toBe(pending)
    failRead = false; await data.tasks.refresh(); await data.tasks.recapRead('latest')
    denied = true; await data.tasks.recapRead('refresh')
    expect(get(data.tasks)).toMatchObject({ recap: null, selectedSession: '', pending: null, access: null })
  } finally { data.tasks.dispose() }
})

const invalidPages: [string, (rows: typeof turn[]) => typeof turn[]][] = [
  ['six rows', (rows) => [...rows, { ...rows[4], turn_id: 'extra', created_at: rows[4].created_at - 1 }]],
  ['wrong session', (rows) => [{ ...rows[0], session_id: 'another-session' }, ...rows.slice(1)]],
  ['wrong instance', (rows) => [{ ...rows[0], instance_id: 'another-instance' }, ...rows.slice(1)]],
  ['bad order', (rows) => [...rows].reverse()],
  ['same-time bad id order', (rows) => [rows[0], { ...rows[1], created_at: rows[0].created_at, turn_id: 'zzzz' }, ...rows.slice(2)]],
  ['empty cursor page', () => []],
]
test.each(invalidPages)('recap rejects %s instead of showing empty success', async (_kind, malformed) => {
  let armed = false
  const data = recapFixture((path) => {
    if (!armed || !path.includes('/turns?limit=5&')) return
    const rows = data.rows.slice(0, 5).map((row) => ({ ...row }))
    return json({ data: malformed(rows), next_cursor: 'p5' })
  })
  try {
    await data.ready(); armed = true; await data.tasks.recapRead('latest')
    expect(get(data.tasks)).toMatchObject({ recap: null, syncStopped: true, access: null })
    expect(get(data.tasks).problem).not.toBe('')
  } finally { data.tasks.dispose() }
})

test.each(['cross turn', 'nonadvancing cursor', 'repeated event id', 'oversized page'])('recap rejects %s events', async (kind) => {
  let armed = false
  const data = recapFixture((path) => {
    if (!armed || !path.includes('/events?limit=50&turn_id=round-20')) return
    const second = path.endsWith('after=50')
    const events = Array.from({ length: kind === 'oversized page' ? 51 : 50 }, (_, index) => output('round-20', (second ? 50 : 0) + index + 1))
    if (kind === 'cross turn') events[0].turn_id = 'round-19'
    if (kind === 'repeated event id' && second) events[0].event_id = 'round-20-1'
    return json({ data: events, next_cursor: kind === 'nonadvancing cursor' ? 0 : events.at(-1)!.cursor })
  })
  try {
    await data.ready(); armed = true; await data.tasks.recapRead('latest')
    expect(get(data.tasks)).toMatchObject({ recap: null, syncStopped: true })
  } finally { data.tasks.dispose() }
})

test('older page enforces its prior boundary and rejects a cursor loop', async () => {
  let broken = ''
  const data = recapFixture((path) => {
    if (!path.includes('/turns?limit=5&before=p')) return
    if (broken === 'boundary') return json({ data: data.rows.slice(0, 5), next_cursor: 'p10' })
    if (broken === 'loop') return json({ data: data.rows.slice(5, 10), next_cursor: 'p5' })
    if (broken === 'trail-loop') return json({ data: data.rows.slice(10), next_cursor: 'p5' })
  })
  try {
    await data.ready(); await data.tasks.recapRead('latest'); broken = 'boundary'; await data.tasks.recapRead('older')
    expect(get(data.tasks)).toMatchObject({ recap: null, recapCursor: '', recapTrail: [] })
    broken = ''; await data.tasks.refresh(); await data.tasks.recapRead('latest')
    broken = 'loop'; await data.tasks.recapRead('older')
    expect(get(data.tasks)).toMatchObject({ recap: null, syncStopped: true })
    broken = ''; await data.tasks.refresh(); await data.tasks.recapRead('latest'); await data.tasks.recapRead('older')
    broken = 'trail-loop'; await data.tasks.recapRead('older')
    expect(get(data.tasks)).toMatchObject({ recap: null, syncStopped: true })
  } finally { data.tasks.dispose() }
})

test('chunk gaps, empty replies and mixed execution states remain distinct per round', async () => {
  const data = recapFixture()
  try {
    data.rows[0].state = 'running'; data.rows[1].state = 'failed'
    data.events.set('round-20', [{ ...output('round-20'), payload: { ...output('round-20').payload, chunk_index: 1, chunk_count: 3 } }])
    data.events.set('round-18', [{ ...output('round-18'), payload: { ...output('round-18').payload, text: '' } }])
    await data.ready(); await data.tasks.recapRead('latest')
    const rows = get(data.tasks).recap!.rows
    expect(rows.slice(0, 3).map((row) => row.turn.state)).toEqual(['running', 'failed', 'completed'])
    expect(projectConversation(rows[0].events.data).partial).toBeTrue()
    expect(rows[1]).toMatchObject({ events: { data: [] }, more: false, trimmed: false })
    expect(projectConversation(rows[2].events.data).messages[0].text).toBe('')
  } finally { data.tasks.dispose() }
})

test('the entire recap shares the original execution deadline across page progress', async () => {
  let slow = false
  const data = recapFixture((path) => {
    if (!slow) return
    if (path.includes('/turns?limit=5&')) return pause(15).then(() => json({ data: data.rows.slice(0, 5), next_cursor: 'p5' }))
    if (path.includes('/events?')) return pause(20).then(() => json({ data: [], next_cursor: 0 }))
  }, 25)
  try {
    await data.ready(); slow = true; await data.tasks.recapRead('latest')
    expect(get(data.tasks)).toMatchObject({ recap: null, busy: false, refreshing: false })
    expect(get(data.tasks).problem).not.toBe('')
  } finally { data.tasks.dispose() }
})

test('recap event retention cannot use a relaxed or invalid byte limit', () => {
  const empty = { data: [], next_cursor: 0 }
  expect(() => appendEvents(null, empty, MAX_EVENT_BYTES + 1)).toThrow()
  expect(() => appendEvents(null, empty, 0)).toThrow()
  expect(() => appendEvents(null, empty, Number.NaN)).toThrow()
  expect(appendEvents(null, empty)).toEqual({ events: empty, trimmed: false })
})

// Each older-page cursor is supplied by the preceding response, so pagination is intentionally sequential.
async function olderPages(tasks: ReturnType<typeof createTasks>, count: number): Promise<void> {
  if (!count) return
  await tasks.recapRead('older')
  await olderPages(tasks, count - 1)
}

test('recap retains at most 32 return positions and always supports latest', async () => {
  const data = recapFixture()
  try {
    data.rows.push(...Array.from({ length: 170 }, (_, index) => ({ ...turn, turn_id: `ancient-${index}`,
      created_at: turn.created_at - index - 100, updated_at: turn.updated_at - index - 100 })))
    await data.ready(); await data.tasks.recapRead('latest'); await olderPages(data.tasks, 34)
    expect(get(data.tasks)).toMatchObject({ recapPage: 35, recapCursor: 'p170' })
    expect(get(data.tasks).recapTrail).toHaveLength(32)
    expect(get(data.tasks).recap?.rows).toHaveLength(5)
    await data.tasks.recapRead('newer')
    expect(get(data.tasks)).toMatchObject({ recapPage: 34, recapCursor: 'p165' })
    await data.tasks.recapRead('latest')
    expect(get(data.tasks)).toMatchObject({ recapPage: 1, recapCursor: '', recapTrail: [] })
  } finally { data.tasks.dispose() }
})

test.each(['leave workspace', 'forget', 'dispose', 'clear selection', 'other session'])('%s clears and cancels recap without accepting late old-session output', async (action) => {
  let hold = false, release: ((response: Response) => void) | undefined, signal: AbortSignal | undefined
  const data = recapFixture((path, init) => {
    if (path === '/web/api/sessions/session-b') return json({ data: { ...detail, session_id: 'session-b' } })
    if (path.includes('/sessions/session-b/turns?')) return json({ data: [], next_cursor: '' })
    if (hold && path.includes('/events?limit=50&turn_id=round-20&after=0')) {
      signal = init?.signal as AbortSignal
      return new Promise<Response>((resolve) => { release = resolve })
    }
  })
  try {
    await data.ready(); await data.tasks.recapRead('latest'); hold = true
    const reading = data.tasks.recapRead('refresh'); await pause()
    if (action === 'leave workspace') data.tasks.setActive(false)
    if (action === 'forget') data.tasks.forget()
    if (action === 'dispose') data.tasks.dispose()
    if (action === 'clear selection') data.tasks.clearSelection()
    if (action === 'other session') await data.tasks.openTarget('session-b')
    expect(signal?.aborted).toBeTrue()
    expect(get(data.tasks)).toMatchObject({ recap: null, recapPage: 1, recapCursor: '', recapTrail: [] })
    release!(json({ data: [output('round-20')], next_cursor: 1 })); await reading
    expect(get(data.tasks).recap).toBeNull()
    if (action === 'other session') expect(get(data.tasks).selectedSession).toBe('session-b')
  } finally { release?.(json({ data: [], next_cursor: 0 })); data.tasks.dispose() }
})

test('recap does not take ownership of an in-flight write or alter its pending key', async () => {
  let release: ((response: Response) => void) | undefined
  const data = recapFixture((_path, init) => init?.method === 'POST'
    ? new Promise<Response>((resolve) => { release = resolve }) : undefined)
  try {
    await data.ready()
    const writing = data.tasks.act('send', 'session-a', 'original in flight'); await pause()
    const pending = get(data.tasks).pending
    data.requests.length = 0; await data.tasks.recapRead('latest')
    expect(get(data.tasks)).toMatchObject({ busy: true, recap: null })
    expect(get(data.tasks).pending).toBe(pending)
    expect(data.requests).toHaveLength(0)
    release!(json({}, 503)); await writing
    expect(get(data.tasks)).toMatchObject({ outcome: 'unknown', pending })
  } finally { release?.(json({}, 503)); data.tasks.dispose() }
})

test('a session response cannot substitute another instance during recap', async () => {
  let changed = false
  const data = recapFixture((path) => changed && path === '/web/api/sessions/session-a'
    ? json({ data: { ...detail, instance_id: 'other-instance' } }) : undefined)
  try {
    await data.ready(); changed = true; data.requests.length = 0
    await data.tasks.recapRead('latest')
    expect(get(data.tasks)).toMatchObject({ recap: null, syncStopped: true })
    expect(data.requests.some((request) => request.path.includes('/events?'))).toBeFalse()
  } finally { data.tasks.dispose() }
})
