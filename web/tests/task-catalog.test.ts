import { expect, test } from 'bun:test'
import { catalogPage, catalogQuery, catalogPath } from '../src/lib/task-catalog'

const query = { instance_id: '', state: 'all', q: '' } as const
const entry = { session_id: 'session-a', instance_id: 'alpha', name: 'task', project_id: 'fixed',
  online: true, status: 'idle', updated_at: 1700000000, category: 'attention',
  latest_turn: { turn_id: 'turn-a', state: 'failed', created_at: 1700000000, updated_at: 1700000001 } }

test('catalog keeps idle session availability separate from latest failed outcome', () => {
  const page = catalogPage({ data: [entry], next_cursor: '' }, query)
  expect(page.data[0]).toMatchObject({ status: 'idle', category: 'attention', latest_turn: { state: 'failed' } })
  expect(catalogPage({ data: [{ ...entry, latest_turn: null, category: 'idle' }], next_cursor: '' }, query).data[0].latest_turn).toBeNull()
})

test('catalog rejects duplicate, unordered, cross-instance, wrong-filter and non-progress pages', () => {
  const bad = [
    { data: [entry, entry], next_cursor: '' },
    { data: [{ ...entry, session_id: 'z' }, entry], next_cursor: '' },
    { data: [], next_cursor: 'cursor' },
    { data: [{ ...entry, latest_turn: { ...entry.latest_turn, updated_at: 0 } }], next_cursor: '' },
    { data: [{ ...entry, category: 'success' }], next_cursor: '' },
    { data: [{ ...entry, online: 'true' }], next_cursor: '' },
  ]
  for (const value of bad) expect(() => catalogPage(value, query)).toThrow()
  expect(() => catalogPage({ data: [entry], next_cursor: '' }, { ...query, instance_id: 'other' })).toThrow()
  expect(() => catalogPage({ data: [entry], next_cursor: '' }, { ...query, state: 'completed' })).toThrow()
  expect(() => catalogPage({ data: [entry], next_cursor: 'cursor' }, query, 'cursor')).toThrow()
  expect(() => catalogPage({ data: Array.from({ length: 21 }, () => entry), next_cursor: '' }, query)).toThrow()
})

test('catalog query is bounded and encoded; no arbitrary query fields reach the request', () => {
  const normalized = catalogQuery({ ...query, q: '  项目_% & next  ', ignored: 'private' })
  expect(normalized).toEqual({ ...query, q: '项目_% & next' })
  const url = new URL(catalogPath(normalized), 'https://abr.test')
  expect(url.pathname).toBe('/task-sessions')
  expect(url.searchParams.get('q')).toBe('项目_% & next')
  expect(url.searchParams.get('limit')).toBe('20')
  for (const change of [{ state: 'unknown' }, { instance_id: '../alpha' }, { q: 'x'.repeat(129) }, { q: 'a\nb' }]) {
    expect(() => catalogQuery({ ...query, ...change })).toThrow()
  }
})
