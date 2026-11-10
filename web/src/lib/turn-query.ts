import { ReadProblem } from './api'
import { cursor, member, record, taskId, TURN_STATES } from './task-values'
import { turnPage, type Turns, type TurnStatus } from './task-history'

export type TurnQuery = { q: string; state: string }
export type TurnBoundary = Pick<TurnStatus, 'turn_id' | 'created_at'>
export type TurnPosition = { before: string; lower: TurnBoundary | null }
export class TurnReadProblem extends ReadProblem {
  constructor(kind: 'invalid' | 'limited' = 'invalid') { super(kind) }
}
export const defaultTurnQuery = (): TurnQuery => ({ q: '', state: 'all' })
export function turnQuery(value: unknown): TurnQuery {
  const row = record(value)
  if (typeof row.q !== 'string' || row.q.length > 128 || new TextEncoder().encode(row.q).length > 512 ||
      /[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/.test(row.q)) throw new ReadProblem('invalid')
  return { q: row.q.trim(), state: member(row.state, ['all', ...TURN_STATES]) }
}
export function turnHistoryPath(session: string, before = '', query: TurnQuery = defaultTurnQuery()): string {
  const valid = turnQuery(query), params = new URLSearchParams({ limit: '20', before: cursor(before) })
  if (valid.q) params.set('q', valid.q)
  if (valid.state !== 'all') params.set('state', valid.state)
  return `/sessions/${encodeURIComponent(taskId(session))}/turns?${params}`
}
export function exactTurnPath(session: string, turn: string): string {
  return `/sessions/${encodeURIComponent(taskId(session))}/turns?${new URLSearchParams({ limit: '1', turn_id: taskId(turn) })}`
}
export function latestTurnPath(session: string): string {
  return `/sessions/${encodeURIComponent(taskId(session))}/turns?limit=1`
}
export function olderTurn(left: TurnBoundary, right: TurnBoundary): boolean {
  return left.created_at < right.created_at || (left.created_at === right.created_at && left.turn_id < right.turn_id)
}
export function checkedTurnPage(value: unknown, session: string, instance: string, before = '', query = defaultTurnQuery(), lower: TurnBoundary | null = null): Turns {
  const page = turnPage(value, session, before), valid = turnQuery(query)
  let prior = lower
  for (const turn of page.data) {
    if (turn.instance_id !== instance || turn.updated_at < turn.created_at || (valid.state !== 'all' && turn.state !== valid.state) ||
        (prior && !olderTurn(turn, prior))) throw new ReadProblem('invalid')
    prior = turn
  }
  return page
}
export const turnBoundary = (turn: TurnStatus): TurnBoundary => ({ turn_id: turn.turn_id, created_at: turn.created_at })
