import { expect, test } from 'bun:test'
import { get } from 'svelte/store'
import { createTasks } from '../src/lib/tasks'
import { fixture, json, turn, pause, access } from './task-fixture'
import { turnFixture } from './turn-fixture'
import { checkedTurnPage, defaultTurnQuery, exactTurnPath, turnHistoryPath, turnQuery } from '../src/lib/turn-query'
import { parseTaskRoute, taskRouteHash, taskRouteUrl } from '../src/lib/task-route'

test('opening a known old turn outside the current page reads its complete prompt', async () => {
  const old = { ...turn, turn_id: 'old-turn', created_at: turn.created_at - 100, prompt: 'Original old instruction, beyond the latest page.' }
  const tasks = createTasks({ fetcher: fixture((path) => {
    if (path.includes('turn_id=old-turn') && path.includes('/turns?')) return json({ data: [old], next_cursor: '' })
    if (path === '/web/api/turns/old-turn') return json({ data: old })
  }) })
  try {
    await tasks.refresh(); await tasks.open('session-a'); await tasks.selectTurn('old-turn')
    expect(get(tasks).currentTurn?.prompt).toBe(old.prompt)
    expect(get(tasks).selectedTurn).toBe(old.turn_id)
  } finally { tasks.dispose() }
})

test('history and precise routes are bounded and carry only session/turn identity', () => {
  const target = { session: '项目 #1', turn: '轮次?2' }, hash = taskRouteHash(target)
  expect(parseTaskRoute(hash)).toEqual(target)
  expect(taskRouteUrl(target, 'https://abr.test/?code=private#tasks')).toBe('https://abr.test/' + hash)
  expect(parseTaskRoute('#tasks/session-a')).toEqual({ session: 'session-a', turn: '' })
  expect(parseTaskRoute('#overview')).toBeNull()
  for (const value of ['#tasks/', '#tasks/a/turns', '#tasks/a/turns/', '#tasks/a/turns/b/extra', '#tasks/%2F/turns/b', '#tasks/a/turns/..', '#tasks/%zz']) {
    expect(() => parseTaskRoute(value)).toThrow()
  }
  expect(turnHistoryPath('session-a', 'page20', { q: '中文 %_', state: 'failed' })).toBe('/sessions/session-a/turns?limit=20&before=page20&q=%E4%B8%AD%E6%96%87+%25_&state=failed')
  expect(exactTurnPath('session-a', 'old')).toBe('/sessions/session-a/turns?limit=1&turn_id=old')
  for (const q of ['x'.repeat(129), 'bad\u202e', 'bad\u0080']) expect(() => turnQuery({ q, state: 'all' })).toThrow()
  expect(() => turnQuery({ q: '', state: 'invented' })).toThrow()
})

test('search, next, previous and first are read-only and preserve the selected turn', async () => {
  const data = turnFixture((_path, init) => { expect(init?.method).toBeUndefined(); return undefined })
  try {
    await data.ready(); await data.tasks.turnSearch({ q: 'historical', state: 'completed' })
    const first = get(data.tasks).turns!.data.map((item) => item.turn_id)
    expect(first).toHaveLength(20)
    await data.tasks.turnNavigate('next')
    expect(get(data.tasks).turns?.data).toHaveLength(2)
    expect(get(data.tasks).turnHistoryPage).toBe(2)
    expect(get(data.tasks).selectedTurn).toBe(turn.turn_id)
    await data.tasks.turnNavigate('previous')
    expect(get(data.tasks).turns!.data.map((item) => item.turn_id)).toEqual(first)
    await data.tasks.turnSearch(defaultTurnQuery())
    expect(get(data.tasks).turnHistoryTrail).toHaveLength(0)
    expect(get(data.tasks).turns?.data[0].turn_id).toBe(turn.turn_id)
  } finally { data.tasks.dispose() }
})

test('selected status refresh and exact target changes do not rerun retained search windows', async () => {
  let refuseSearch = false
  const data = turnFixture((path) => path.includes('q=historical') && refuseSearch ? json({}, 503) : undefined)
  try {
    await data.ready(); await data.tasks.turnSearch({ q: 'historical', state: 'failed' })
    const prior = get(data.tasks).turns, readAt = get(data.tasks).turnHistoryReadAt
    const queries = data.requests.filter((path) => path.includes('q=historical')).length
    refuseSearch = true
    await data.tasks.sync(); await data.tasks.selectTurn('older-40')
    expect(data.requests.filter((path) => path.includes('q=historical'))).toHaveLength(queries)
    expect(get(data.tasks).currentTurn?.prompt).toContain('instruction 40')
    expect(get(data.tasks).turns).toBe(prior); expect(get(data.tasks).turnHistoryReadAt).toBe(readAt)
  } finally { data.tasks.dispose() }
})

test('exact missing, foreign and swapped responses never fall back to the latest turn', async () => {
  for (const type of ['missing', 'foreign', 'swapped']) {
    const data = turnFixture((path) => {
      if (!path.includes('turn_id=unknown')) return
      return json({ data: type === 'missing' ? [] : [{ ...turn, turn_id: type === 'foreign' ? 'unknown' : 'swapped',
        session_id: type === 'foreign' ? 'other-session' : 'session-a' }], next_cursor: '' })
    })
    try {
      await data.tasks.refresh(); await data.tasks.openTarget('session-a', 'unknown')
      expect(get(data.tasks).currentTurn).toBeNull(); expect(get(data.tasks).events).toBeNull()
      expect(get(data.tasks).problem).not.toBe('')
      expect(get(data.tasks).selectedTurn).not.toBe(turn.turn_id)
    } finally { data.tasks.dispose() }
  }
})

test('failed query resets its filters so refresh recovers without losing an unknown write', async () => {
  for (const status of [400, 500, 503]) {
    let posts = 0
    const data = turnFixture((path, init) => {
      if (init?.method === 'POST') { posts++; return Promise.reject(new Error('unknown submission')) }
      if (path.includes('q=broken')) return json({}, status)
    })
    try {
      await data.ready(); await data.tasks.act('send', 'session-a', 'only once'); await data.tasks.sync()
      const pending = get(data.tasks).pending
      await data.tasks.turnSearch({ q: 'broken', state: 'failed' })
      expect(get(data.tasks).turnQuery).toEqual(defaultTurnQuery()); expect(get(data.tasks).turns).toBeNull()
      expect(get(data.tasks).problem).toContain('搜索条件已重置')
      await data.tasks.refresh()
      expect(get(data.tasks).detail?.session_id).toBe('session-a'); expect(get(data.tasks).currentTurn?.turn_id).toBe(turn.turn_id)
      expect(get(data.tasks).pending).toBe(pending); expect(get(data.tasks).outcome).toBe('unknown'); expect(posts).toBe(1)
    } finally { data.tasks.dispose() }
  }
})

test('late history content and cross-tab identity changes cannot repopulate cleared records', async () => {
  let release!: (value: Response) => void, identity = access.context
  const data = turnFixture((path) => {
    if (path === '/web/api/access') return json({ data: { ...access, context: identity } })
    if (path.includes('q=delayed')) return new Promise((resolve) => { release = resolve })
  })
  try {
    await data.ready()
    const pending = data.tasks.turnSearch({ q: 'delayed', state: 'all' }); await pause()
    data.tasks.forget(); release(json({ data: [turn], next_cursor: '' })); await pending
    expect(get(data.tasks).turns).toBeNull(); expect(get(data.tasks).turnQuery).toEqual(defaultTurnQuery())
    await data.ready(); identity = 'c'.repeat(64)
    await data.tasks.turnSearch({ q: 'historical', state: 'all' })
    expect(get(data.tasks).selectedSession).toBe(''); expect(get(data.tasks).turns).toBeNull()
    expect(get(data.tasks).turnQuery).toEqual(defaultTurnQuery())
  } finally { data.tasks.dispose() }
})

test('shared turn parser rejects cross-instance, state mismatch, repeated and non-progress pages', () => {
  const value = { data: [turn], next_cursor: '' }
  expect(() => checkedTurnPage(value, 'session-a', 'wrong')).toThrow()
  expect(() => checkedTurnPage(value, 'session-a', 'alpha', '', { q: '', state: 'failed' })).toThrow()
  expect(() => checkedTurnPage(value, 'session-a', 'alpha', '', defaultTurnQuery(), turn)).toThrow()
  expect(() => checkedTurnPage({ ...value, data: [turn, turn] }, 'session-a', 'alpha')).toThrow()
})
