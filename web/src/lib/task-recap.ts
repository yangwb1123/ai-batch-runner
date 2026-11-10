import { ReadProblem } from './api'
import { appendEvents, eventPage, MAX_EVENT_BYTES, type Events } from './task-events'
import { detailRecord, record, taskId, type Turn } from './task-models'
import type { Patch, StatePort } from './task-state'
import type { ReadTask } from './task-selection'
import { checkedTurnPage, type TurnBoundary, type TurnPosition } from './turn-query'
import { cursor } from './task-values'

export type RecapRow = { turn: Turn; events: Events; more: boolean; trimmed: boolean }
export type RecapPage = { rows: RecapRow[]; next_cursor: string; readAt: number }
export type RecapDirection = 'latest' | 'older' | 'newer' | 'refresh'
export const RECAP_TURNS = 5
const ROW_BYTES = Math.floor(MAX_EVENT_BYTES / RECAP_TURNS)
export const recapReset = () => ({ recap: null as RecapPage | null, recapCursor: '', recapLower: null as TurnBoundary | null,
  recapTrail: [] as TurnPosition[], recapPage: 1 })

async function readRow(read: ReadTask, turn: Turn, signal: AbortSignal): Promise<RecapRow> {
  const path = `/sessions/${encodeURIComponent(turn.session_id)}/events?limit=50&turn_id=${encodeURIComponent(turn.turn_id)}`
  const first = eventPage(await read(`${path}&after=0`, signal), turn.session_id, 0, turn.turn_id)
  const head = appendEvents(null, first, ROW_BYTES)
  if (first.data.length < 50) return { turn, ...head, more: false }
  const next = eventPage(await read(`${path}&after=${first.next_cursor}`, signal), turn.session_id, first.next_cursor, turn.turn_id)
  const ids = new Set(first.data.map((event) => event.event_id))
  if (next.data.some((event) => ids.has(event.event_id))) throw new ReadProblem('invalid')
  const tail = appendEvents(head.events, next, ROW_BYTES)
  return { turn, events: tail.events, more: next.data.length === 50, trimmed: head.trimmed || tail.trimmed }
}

export function createRecapReader(port: StatePort, read: ReadTask) {
  return async (signal: AbortSignal): Promise<Patch> => {
    const state = port.get(), id = state.selectedSession
    if (!id) return {}
    const raw = await read(`/sessions/${encodeURIComponent(taskId(id))}`, signal)
    const detail = detailRecord(record(raw).data, id)
    if (!state.detail || detail.instance_id !== state.detail.instance_id) throw new ReadProblem('invalid')
    const params = new URLSearchParams({ limit: String(RECAP_TURNS), before: cursor(state.recapCursor) })
    const value = await read(`/sessions/${encodeURIComponent(id)}/turns?${params}`, signal)
    const page = checkedTurnPage(value, id, detail.instance_id, state.recapCursor, undefined, state.recapLower)
    if (page.data.length > RECAP_TURNS || (page.next_cursor && state.recapTrail.some((position) => position.before === page.next_cursor))) throw new ReadProblem('invalid')
    const rows = await Promise.all(page.data.map((turn) => readRow(read, turn, signal)))
    signal.throwIfAborted()
    const current = port.get()
    if (current.contextEpoch !== state.contextEpoch || current.selectedSession !== id || current.detail?.instance_id !== detail.instance_id) throw new ReadProblem('invalid')
    return { recap: { rows, next_cursor: page.next_cursor, readAt: Date.now() } }
  }
}
