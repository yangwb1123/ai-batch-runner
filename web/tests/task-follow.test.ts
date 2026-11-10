import { expect, test } from 'bun:test'
import { get } from 'svelte/store'
import { createTasks } from '../src/lib/tasks'
import { fixture, json, detail, turn, access, pause } from './task-fixture'
import type { Turn } from '../src/lib/task-history'
import { latestTurnPath } from '../src/lib/turn-query'

function followFixture(override: (path: string, init?: RequestInit) => Response | Promise<Response> | undefined = () => undefined) {
  const rows: Turn[] = [{ ...turn }], session = { ...detail }, requests: { path: string; method?: string }[] = []
  const events = new Map<string, { cursor: number; event_id: string; session_id: string; turn_id: string; kind: string; payload: { text: string } }[]>()
  const tasks = createTasks({ fetcher: fixture((path, init) => {
    requests.push({ path, method: init?.method })
    const custom = override(path, init)
    if (custom) return custom
    const url = new URL(path, 'https://abr.test')
    const query = new URLSearchParams(url.search)
    if (url.pathname === '/web/api/sessions/session-a') return json({ data: session })
    if (url.pathname === '/web/api/sessions/session-a/turns') {
      const id = query.get('turn_id'), search = (query.get('q') || '').toLowerCase(), state = query.get('state') || 'all'
      const matches = rows.filter((row) => id ? row.turn_id === id : (state === 'all' || row.state === state) &&
        [row.turn_id, row.prompt].some((text) => text.toLowerCase().includes(search)))
      const start = Number((query.get('before') || 'page0').slice(4)), end = start + Number(query.get('limit') || 20)
      return json({ data: matches.slice(start, end), next_cursor: !id && end < matches.length ? `page${end}` : '' })
    }
    if (url.pathname.startsWith('/web/api/turns/')) {
      const found = rows.find((row) => row.turn_id === url.pathname.split('/').at(-1))
      return found ? json({ data: found }) : json({}, 404)
    }
    if (url.pathname === '/web/api/sessions/session-a/events') {
      const after = Number(query.get('after')), items = events.get(query.get('turn_id') || '') ?? []
      const data = items.filter((row) => row.cursor > after).slice(0, 50)
      return json({ data, next_cursor: data.at(-1)?.cursor ?? after })
    }
  }) })
  const add = (id: string, state = 'running') => {
    const value = { ...turn, turn_id: id, state, created_at: turn.created_at + rows.length + 1,
      updated_at: turn.updated_at + rows.length + 1, prompt: `instruction ${id}` }
    rows.unshift(value); return value
  }
  const output = (id: string, count: number, offset = 0) => events.set(id, Array.from({ length: count }, (_, i) => ({
    cursor: offset + i + 1, event_id: `event-${id}-${i}`, session_id: detail.session_id, turn_id: id,
    kind: 'message.completed', payload: { text: `output ${id} ${i}` },
  })))
  return { tasks, rows, session, requests, add, output,
    ready: async () => { await tasks.refresh(); await tasks.open('session-a') } }
}

test('task-level selection follows another client active turn and then a newer completed turn', async () => {
  const data = followFixture()
  try {
    await data.ready(); expect(get(data.tasks).selectedTurn).toBe('turn-a')
    data.add('external-running'); data.session.active_turn_id = 'external-running'
    await data.tasks.sync()
    expect(get(data.tasks).selectedTurn).toBe('external-running')
    expect(get(data.tasks).turnMode).toBe('live')
    expect(get(data.tasks).liveTurn?.turn_id).toBe('external-running')
    data.rows[0].state = 'completed'; data.session.active_turn_id = ''
    data.add('external-completed', 'completed'); await data.tasks.refresh()
    expect(get(data.tasks).currentTurn?.turn_id).toBe('external-completed')
    expect(get(data.tasks).liveTurn?.turn_id).toBe('external-completed')
    expect(data.requests.every((request) => !request.method)).toBeTrue()
  } finally { data.tasks.dispose() }
})

test('pinning the same turn stays fixed while its live hint advances without fetching live output', async () => {
  const data = followFixture()
  try {
    data.output('turn-a', 1); await data.ready(); await data.tasks.selectTurn('turn-a')
    data.add('elsewhere'); data.session.active_turn_id = 'elsewhere'; data.output('elsewhere', 1, 99)
    data.requests.length = 0; await data.tasks.sync()
    expect(get(data.tasks)).toMatchObject({ turnMode: 'fixed', selectedTurn: 'turn-a', liveTurn: { turn_id: 'elsewhere' } })
    expect(get(data.tasks).events?.data.map((row) => row.turn_id)).toEqual(['turn-a'])
    expect(data.requests.filter((row) => row.path.includes('/events?')).every((row) => row.path.includes('turn_id=turn-a'))).toBeTrue()
    await data.tasks.open('session-a')
    expect(get(data.tasks)).toMatchObject({ turnMode: 'live', selectedTurn: 'elsewhere' })
  } finally { data.tasks.dispose() }
})

test('returning to live retains search and older page while using an independent unfiltered latest read', async () => {
  const data = followFixture()
  try {
    for (let index = 0; index < 24; index++) data.add(`archive-${index}`, 'completed')
    await data.ready(); await data.tasks.turnSearch({ q: 'archive', state: 'completed' }); await data.tasks.turnNavigate('next')
    await data.tasks.selectTurn('archive-0')
    const before = get(data.tasks), history = before.turns, readAt = before.turnHistoryReadAt
    data.add('fresh-completion', 'completed'); await data.tasks.sync()
    expect(get(data.tasks)).toMatchObject({ turnMode: 'fixed', selectedTurn: 'archive-0', liveTurn: { turn_id: 'fresh-completion' } })
    data.requests.length = 0; await data.tasks.returnToLive()
    const after = get(data.tasks)
    expect(after).toMatchObject({ turnMode: 'live', selectedTurn: 'fresh-completion', turnQuery: before.turnQuery,
      turnHistoryCursor: before.turnHistoryCursor, turnHistoryPage: 2, turnHistoryTrail: before.turnHistoryTrail })
    expect(after.turns).toBe(history); expect(after.turnHistoryReadAt).toBe(readAt)
    expect(data.requests.filter((row) => row.path.includes('/turns?')).map((row) => row.path)).toEqual(['/web/api/sessions/session-a/turns?limit=1'])
    await data.tasks.sync(); expect(get(data.tasks).selectedTurn).toBe('fresh-completion')
  } finally { data.tasks.dispose() }
})

test('active turn wins over newer history and is exactly resolved outside a filtered window', async () => {
  const data = followFixture()
  try {
    data.add('active-away'); data.add('newer-completed', 'completed'); await data.ready()
    await data.tasks.turnSearch({ q: 'inspect', state: 'completed' })
    data.session.active_turn_id = 'active-away'; data.requests.length = 0; await data.tasks.returnToLive()
    expect(get(data.tasks)).toMatchObject({ selectedTurn: 'active-away', liveTurn: { turn_id: 'active-away' } })
    expect(data.requests.some((row) => row.path === '/web/api/sessions/session-a/turns?limit=1&turn_id=active-away')).toBeTrue()
    expect(get(data.tasks).turns?.data.map((row) => row.turn_id)).toEqual(['turn-a'])
  } finally { data.tasks.dispose() }
})

test('fixed output gets a fresh active hint even when its retained history has an older active status', async () => {
  const data = followFixture()
  try {
    const active = data.add('active-away', 'queued'); await data.ready()
    await data.tasks.turnSearch({ q: 'active', state: 'all' }); await data.tasks.selectTurn('turn-a')
    const retained = get(data.tasks).turns
    active.state = 'running'; data.session.active_turn_id = active.turn_id; data.requests.length = 0; await data.tasks.sync()
    expect(get(data.tasks)).toMatchObject({ turnMode: 'fixed', selectedTurn: 'turn-a', liveTurn: { turn_id: active.turn_id, state: 'running' } })
    expect(get(data.tasks).turns).toBe(retained)
    expect(data.requests.filter((row) => row.path.includes('/events?')).every((row) => row.path.includes('turn_id=turn-a'))).toBeTrue()
  } finally { data.tasks.dispose() }
})

test('live target change drops a trimmed event window and starts the new turn from cursor zero', async () => {
  const data = followFixture()
  try {
    data.output('turn-a', 510); await data.ready(); await data.tasks.loadEvents(); await data.tasks.loadEvents(); await data.tasks.loadEvents()
    expect(get(data.tasks).eventsTrimmed).toBeTrue(); expect(get(data.tasks).events?.data).toHaveLength(500)
    data.add('new-output'); data.output('new-output', 1, 800); data.requests.length = 0; await data.tasks.sync()
    expect(get(data.tasks)).toMatchObject({ selectedTurn: 'new-output', eventsTrimmed: false, eventsMore: false })
    expect(get(data.tasks).events?.data.map((row) => row.turn_id)).toEqual(['new-output'])
    expect(data.requests.filter((row) => row.path.includes('/events?')).map((row) => row.path)).toEqual([
      '/web/api/sessions/session-a/events?limit=50&after=0&turn_id=new-output',
    ])
  } finally { data.tasks.dispose() }
})

test('fixed missing status clears selection without selecting the newest or replaying an unknown write', async () => {
  let missing = false, posts = 0
  const data = followFixture((path, init) => {
    if (init?.method === 'POST') { posts++; return Promise.reject(new Error('lost receipt')) }
    if (missing && path === '/web/api/turns/turn-a') return json({}, 404)
  })
  try {
    await data.ready(); await data.tasks.act('send', 'session-a', 'keep original body')
    const pending = get(data.tasks).pending
    await data.tasks.sync()
    await data.tasks.openTarget('session-a', 'turn-a'); data.add('newest'); missing = true; await data.tasks.sync()
    expect(get(data.tasks)).toMatchObject({ turnMode: 'fixed', selectedSession: '', selectedTurn: '', currentTurn: null, liveTurn: null, events: null, outcome: 'unknown' })
    expect(get(data.tasks).pending).toBe(pending); expect(pending?.body).toContain('keep original body')
    await data.tasks.sync(); expect(get(data.tasks).selectedTurn).toBe(''); expect(posts).toBe(1)
  } finally { data.tasks.dispose() }
})

test('an exact missing fixed link or active identity never falls back to another turn', async () => {
  const data = followFixture()
  try {
    await data.ready(); await data.tasks.openTarget('session-a', 'absent')
    expect(get(data.tasks)).toMatchObject({ turnMode: 'fixed', selectedSession: '', liveTurn: null })
    data.session.active_turn_id = 'absent'; await data.tasks.open('session-a')
    expect(get(data.tasks)).toMatchObject({ selectedSession: '', currentTurn: null, liveTurn: null })
    expect(get(data.tasks).problem).toContain('不存在')
  } finally { data.tasks.dispose() }
})

test('empty live sessions do not read unfiltered output', async () => {
  const data = followFixture()
  try {
    data.rows.length = 0; await data.ready()
    expect(get(data.tasks)).toMatchObject({ turnMode: 'live', selectedTurn: '', liveTurn: null, currentTurn: null, events: null })
    expect(data.requests.some((row) => row.path.includes('/events?'))).toBeFalse()
  } finally { data.tasks.dispose() }
})

test('accepted send retains existing history reset and enters live mode with one submission', async () => {
  let posts = 0
  const data = followFixture((_path, init) => {
    if (init?.method !== 'POST') return undefined
    posts++; data.add('sent-turn', 'queued'); data.session.active_turn_id = 'sent-turn'
    return json({ data: { turn_id: 'sent-turn', session_id: 'session-a', state: 'queued' } }, 202)
  })
  try {
    await data.ready(); await data.tasks.turnSearch({ q: 'inspect', state: 'completed' }); await data.tasks.selectTurn('turn-a')
    await data.tasks.act('send', 'session-a', 'new task')
    expect(get(data.tasks)).toMatchObject({ outcome: 'accepted', turnMode: 'live', selectedTurn: 'sent-turn', liveTurn: { turn_id: 'sent-turn' },
      turnQuery: { q: '', state: 'all' }, turnHistoryPage: 1, turnHistoryCursor: '' })
    expect(posts).toBe(1)
  } finally { data.tasks.dispose() }
})

test('unknown write preserves its exact pending object while readonly fixed and live selections change', async () => {
  let posts = 0
  const data = followFixture((_path, init) => {
    if (init?.method !== 'POST') return undefined
    posts++; return Promise.reject(new Error('unknown receipt'))
  })
  try {
    await data.ready(); await data.tasks.act('send', 'session-a', 'do once')
    const pending = get(data.tasks).pending
    await data.tasks.sync(); await data.tasks.selectTurn('turn-a')
    expect(get(data.tasks)).toMatchObject({ turnMode: 'fixed', selectedTurn: 'turn-a', outcome: 'unknown' })
    data.add('later'); await data.tasks.returnToLive(); await data.tasks.sync()
    expect(get(data.tasks)).toMatchObject({ turnMode: 'live', selectedTurn: 'later', outcome: 'unknown' })
    expect(get(data.tasks).pending).toBe(pending); expect(pending?.body).toContain('do once')
    await data.tasks.act('send', 'session-a', 'do twice'); expect(posts).toBe(1)
  } finally { data.tasks.dispose() }
})

test.each(['context', 'revoke', 'invalid', 'forget'] as const)('%s clears the authoritative live hint', async (cause) => {
  let changed = false
  const data = followFixture((path) => {
    if (!changed) return undefined
    if (path === '/web/api/access' && cause === 'context') return json({ data: { ...access, context: 'b'.repeat(64) } })
    if (path === '/web/api/access' && cause === 'revoke') return json({}, 403)
    if (path === '/web/api/sessions/session-a' && cause === 'invalid') return json({ data: { ...detail, instance_id: '' } })
  })
  try {
    await data.ready(); expect(get(data.tasks).liveTurn?.turn_id).toBe('turn-a'); changed = true
    if (cause === 'forget') data.tasks.forget()
    else await data.tasks.sync()
    expect(get(data.tasks).liveTurn).toBeNull(); expect(get(data.tasks).events).toBeNull()
  } finally { data.tasks.dispose() }
})

test.each(['oversize', 'instance', 'session', 'time', 'cursor'] as const)('independent latest query rejects %s evidence', async (cause) => {
  let changed = false
  const data = followFixture((path) => {
    if (!changed || path !== '/web/api/sessions/session-a/turns?limit=1') return undefined
    const row = { ...turn, ...(cause === 'instance' ? { instance_id: 'other' } : {}),
      ...(cause === 'session' ? { session_id: 'other' } : {}), ...(cause === 'time' ? { updated_at: 1 } : {}) }
    return json({ data: cause === 'oversize' ? [{ ...row, turn_id: 'another', created_at: row.created_at + 1 }, row] : [row],
      next_cursor: cause === 'cursor' ? 'bad\nvalue' : '' })
  })
  try {
    await data.ready(); await data.tasks.turnSearch({ q: 'inspect', state: 'all' }); changed = true; await data.tasks.sync()
    expect(get(data.tasks).liveTurn).toBeNull(); expect(get(data.tasks).access).toBeNull()
    expect(get(data.tasks).problem).toContain('校验')
  } finally { data.tasks.dispose() }
})

test('latest path retains task identifier validation and the existing request budget', () => {
  expect(latestTurnPath('session-a')).toBe('/sessions/session-a/turns?limit=1')
  expect(() => latestTurnPath('../secret')).toThrow(); expect(() => createTasks({ timeoutMs: 12001 })).toThrow()
})

test('switching modes cannot abort a busy write or replay its request', async () => {
  let signal: AbortSignal | null | undefined, release: () => void = () => undefined, posts = 0
  const gate = new Promise<void>((resolve) => { release = resolve })
  const data = followFixture((_path, init) => {
    if (init?.method !== 'POST') return undefined
    signal = init.signal; posts++
    return gate.then(() => { data.add('after-write', 'queued'); data.session.active_turn_id = 'after-write'
      return json({ data: { turn_id: 'after-write', session_id: 'session-a', state: 'queued' } }, 202) })
  })
  try {
    await data.ready(); await data.tasks.selectTurn('turn-a')
    const writing = data.tasks.act('send', 'session-a', 'once'); await pause()
    await data.tasks.returnToLive(); await data.tasks.selectTurn('another')
    expect(get(data.tasks)).toMatchObject({ busy: true, turnMode: 'fixed', selectedTurn: 'turn-a' })
    expect(signal?.aborted).toBeFalse(); release(); await writing
    expect(get(data.tasks)).toMatchObject({ turnMode: 'live', selectedTurn: 'after-write', outcome: 'accepted' }); expect(posts).toBe(1)
  } finally { release(); data.tasks.dispose() }
})

test('a late prior live read cannot republish its target after pinning historical output', async () => {
  let hold = false, release: () => void = () => undefined, entered: () => void = () => undefined
  const gate = new Promise<void>((resolve) => { release = resolve }), started = new Promise<void>((resolve) => { entered = resolve })
  const data = followFixture((path) => {
    if (!hold || path !== '/web/api/turns/later') return undefined
    hold = false; entered(); return gate.then(() => json({ data: data.rows[0] }))
  })
  try {
    await data.ready(); data.add('later'); hold = true
    const reading = data.tasks.sync(); await started; await data.tasks.selectTurn('turn-a')
    release(); await reading
    expect(get(data.tasks)).toMatchObject({ turnMode: 'fixed', selectedTurn: 'turn-a', currentTurn: { turn_id: 'turn-a' }, liveTurn: { turn_id: 'later' } })
  } finally { release(); data.tasks.dispose() }
})
