import { ReadProblem, displayText, type Session } from './api'
import { boolean, boundedText, cursor, member, record, taskId, timestamp, TURN_STATES } from './task-values'

export type CatalogCategory = 'active' | 'attention' | 'completed' | 'idle' | 'closed'
export type CatalogQuery = { instance_id: string; state: 'all' | CatalogCategory; q: string }
export type CatalogEntry = Session & { category: CatalogCategory;
  latest_turn: { turn_id: string; state: string; created_at: number; updated_at: number } | null }
export type CatalogPage = { data: CatalogEntry[]; next_cursor: string }
export type CatalogPosition = { after: string; lower: string }
export const CATALOG_PAGE_SIZE = 20
export const CATALOG_HISTORY_LIMIT = 32
const CATEGORIES = ['active', 'attention', 'completed', 'idle', 'closed']
export class CatalogReadProblem extends ReadProblem {
  constructor() { super('invalid') }
}

export const defaultCatalogQuery = (): CatalogQuery => ({ instance_id: '', state: 'all', q: '' })

export function catalogQuery(value: unknown): CatalogQuery {
  const row = record(value)
  if (typeof row.q !== 'string' || row.q.length > 128 || new TextEncoder().encode(row.q).length > 512 ||
      /[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/.test(row.q)) throw new ReadProblem('invalid')
  return { instance_id: row.instance_id === '' ? '' : taskId(row.instance_id),
    state: member(row.state, ['all', ...CATEGORIES]) as CatalogQuery['state'], q: row.q.trim() }
}

export function catalogPath(query: CatalogQuery, after = ''): string {
  const valid = catalogQuery(query), params = new URLSearchParams({ limit: String(CATALOG_PAGE_SIZE), state: valid.state })
  if (valid.instance_id) params.set('instance_id', valid.instance_id)
  if (valid.q) params.set('q', valid.q)
  if (after) params.set('after', cursor(after))
  return `/task-sessions?${params}`
}

function latestTurn(value: unknown): CatalogEntry['latest_turn'] {
  if (value === null) return null
  const row = record(value), created = timestamp(row.created_at), updated = timestamp(row.updated_at)
  if (updated < created) throw new ReadProblem('invalid')
  return { turn_id: taskId(row.turn_id), state: member(row.state, TURN_STATES), created_at: created, updated_at: updated }
}

function entry(value: unknown, query: CatalogQuery): CatalogEntry {
  const row = record(value), instance = taskId(row.instance_id)
  const category = member(row.category, CATEGORIES) as CatalogCategory
  if ((query.instance_id && instance !== query.instance_id) || (query.state !== 'all' && category !== query.state)) throw new ReadProblem('invalid')
  return { session_id: taskId(row.session_id), instance_id: instance,
    name: displayText(boundedText(row.name, 1024), ''), project_id: displayText(boundedText(row.project_id, 1024), ''),
    status: boundedText(row.status, 128), online: boolean(row.online), updated_at: timestamp(row.updated_at),
    category, latest_turn: latestTurn(row.latest_turn) }
}

export function catalogPage(value: unknown, query: CatalogQuery, after = '', lower = ''): CatalogPage {
  const row = record(value), valid = catalogQuery(query), next = cursor(row.next_cursor, after)
  if (!Array.isArray(row.data) || row.data.length > CATALOG_PAGE_SIZE || (next && !row.data.length)) throw new ReadProblem('invalid')
  const data = row.data.map((item) => entry(item, valid))
  let previous = lower
  for (const item of data) {
    if (previous && item.session_id <= previous) throw new ReadProblem('invalid')
    previous = item.session_id
  }
  return { data, next_cursor: next }
}
