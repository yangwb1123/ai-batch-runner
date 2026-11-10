import { expect, test } from 'bun:test'
import { get } from 'svelte/store'
import { createTasks } from '../src/lib/tasks'
import { access, detail, fixture, json } from './task-fixture'

const entry = { ...detail, updated_at: 1700000000, latest_turn: null, category: 'idle' }
const first = { data: [entry], next_cursor: 'second_page' }
const second = { data: [{ ...entry, session_id: 'session-z' }], next_cursor: '' }
const query = { instance_id: '', state: 'all' as const, q: '' }
const catalog = (path: string) => path.startsWith('/web/api/task-sessions?')

test('server query and bounded page navigation retain selected task and have no writes', async () => {
  const calls: string[] = [], posts: string[] = []
  const tasks = createTasks({ fetcher: fixture((path, init) => {
    if (init?.method === 'POST') posts.push(path)
    if (!catalog(path)) return undefined
    calls.push(path)
    return json(path.includes('after=') ? second : first)
  }) })
  try {
    await tasks.refresh(); await tasks.open('session-a'); await tasks.catalogNavigate('next')
    expect(get(tasks)).toMatchObject({ catalogPage: 2, selectedSession: 'session-a', selectedTurn: 'turn-a' })
    expect(get(tasks).catalog?.data.map((row) => row.session_id)).toEqual(['session-z'])
    await tasks.sync()
    expect(calls.at(-1)).toContain('after=second_page')
    await tasks.catalogNavigate('previous')
    expect(get(tasks)).toMatchObject({ catalogPage: 1, selectedSession: 'session-a' })
    await tasks.catalogSearch({ ...query, q: '工程_%', instance_id: 'alpha' })
    const params = new URL(calls.at(-1)!, 'https://abr.test').searchParams
    expect(params.get('q')).toBe('工程_%'); expect(params.get('instance_id')).toBe('alpha')
    expect(params.has('after')).toBeFalse(); expect(posts).toEqual([])
  } finally { tasks.dispose() }
})

test('a failed catalog refresh clears old list; query errors cannot relabel old rows', async () => {
  let fail = false
  const tasks = createTasks({ fetcher: fixture((path) => catalog(path) ? json(fail ? {} : first, fail ? 503 : 200) : undefined) })
  try {
    await tasks.refresh()
    await tasks.catalogSearch({ ...query, q: 'x'.repeat(129) })
    expect(get(tasks).catalogQuery.q).toBe('')
    expect(get(tasks).problem).not.toBe('')
    fail = true; await tasks.catalogSearch({ ...query, q: 'changed' })
    expect(get(tasks).catalog).toBeNull(); expect(get(tasks).sessions).toEqual([])
    expect(get(tasks).catalogQuery.q).toBe('changed')
  } finally { tasks.dispose() }
})

test('identity change discards page cursors and search before issuing the new account query', async () => {
  let context = access.context
  const requests: string[] = []
  const tasks = createTasks({ fetcher: fixture((path) => {
    if (path === '/web/api/access') return json({ data: { ...access, context } })
    if (catalog(path)) { requests.push(path); return json(first) }
    return undefined
  }) })
  try {
    await tasks.refresh(); await tasks.catalogSearch({ ...query, q: 'private-search' })
    context = 'b'.repeat(64); await tasks.sync()
    expect(get(tasks)).toMatchObject({ catalogQuery: query, catalogAfter: '', catalogPage: 1, catalogTrail: [] })
    expect(requests.at(-1)).not.toContain('private-search')
  } finally { tasks.dispose() }
})

test('late page response after forget cannot restore list, query, or selection', async () => {
  let release: () => void = () => undefined, waiting = false
  const gate = new Promise<void>((resolve) => { release = resolve })
  const tasks = createTasks({ fetcher: fixture((path) => {
    if (!catalog(path)) return undefined
    return waiting ? gate.then(() => json(second)) : json(first)
  }) })
  try {
    await tasks.refresh(); waiting = true
    const next = tasks.catalogNavigate('next')
    await new Promise((resolve) => setTimeout(resolve, 5))
    tasks.forget(); release(); await next
    expect(get(tasks)).toMatchObject({ catalog: null, catalogQuery: query, catalogTrail: [], selectedSession: '' })
  } finally { release(); tasks.dispose() }
})

test('a rejected page cursor recovers from the first page on explicit refresh', async () => {
  const paths: string[] = []
  const tasks = createTasks({ fetcher: fixture((path) => {
    if (!catalog(path)) return undefined
    paths.push(path)
    return path.includes('after=') ? json({}, 400) : json(first)
  }) })
  try {
    await tasks.refresh(); await tasks.catalogSearch({ ...query, q: 'saved-search' })
    await tasks.catalogNavigate('next')
    expect(get(tasks)).toMatchObject({ access: null, catalog: null, catalogAfter: '', catalogPage: 1, catalogTrail: [] })
    expect(get(tasks).catalogQuery.q).toBe('saved-search')
    await tasks.refresh()
    expect(get(tasks).catalog?.data).toHaveLength(1)
    expect(paths.at(-1)).not.toContain('after=')
  } finally { tasks.dispose() }
})

test('long catalogs retain only 32 back cursors and can always restart from home', async () => {
  const tasks = createTasks({ fetcher: fixture((path) => {
    if (!catalog(path)) return undefined
    const after = new URL(path, 'https://abr.test').searchParams.get('after') ?? ''
    const index = after ? Number(after.slice(1)) : 0
    return json({ data: [{ ...entry, session_id: `session-${String(index).padStart(5, '0')}` }], next_cursor: `p${index + 1}` })
  }) })
  try {
    await tasks.refresh()
    for (let index = 0; index < 40; index++) await tasks.catalogNavigate('next')
    expect(get(tasks).catalogPage).toBe(41); expect(get(tasks).catalogTrail).toHaveLength(32)
    expect(get(tasks).sessions).toHaveLength(1)
    for (let index = 0; index < 32; index++) await tasks.catalogNavigate('previous')
    expect(get(tasks).catalogPage).toBe(9); expect(get(tasks).catalogTrail).toHaveLength(0)
    await tasks.catalogNavigate('first')
    expect(get(tasks)).toMatchObject({ catalogAfter: '', catalogPage: 1, catalogTrail: [] })
  } finally { tasks.dispose() }
})

test('a changed authority cursor clears search, selected detail and the draft privacy epoch', async () => {
  const tasks = createTasks({ fetcher: fixture((path) => catalog(path)
    ? path.includes('after=') ? json({}, 403) : json(first) : undefined) })
  try {
    await tasks.refresh(); await tasks.open('session-a'); await tasks.catalogSearch({ ...query, q: 'private' })
    const epoch = get(tasks).contextEpoch
    await tasks.catalogNavigate('next')
    expect(get(tasks)).toMatchObject({ catalog: null, catalogQuery: query, catalogAfter: '', selectedSession: '', detail: null })
    expect(get(tasks).contextEpoch).toBeGreaterThan(epoch)
  } finally { tasks.dispose() }
})
