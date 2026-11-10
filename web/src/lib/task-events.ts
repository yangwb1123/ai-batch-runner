import { ReadProblem, displayText } from './api'
import { boolean, boundedText, record, taskId, timestamp } from './task-values'
import { EVIDENCE_KINDS, evidenceFields, type EvidenceFields } from './task-evidence'

export type Event = { cursor: number; event_id: string; turn_id: string; kind: string; text: string; truncated: boolean; chunk: string;
  chunk_index: number | null; chunk_count: number | null; created_at: number | null;
  item_id?: string; state?: string; exit_code?: number | null; paths?: string[];
  output_text?: string; output_truncated?: boolean } & EvidenceFields
export type Events = { data: Event[]; next_cursor: number }
export const MAX_EVENTS = 500
export const MAX_EVENT_BYTES = 1024 * 1024

function chunkFields(payload: Record<string, unknown>) {
  if (payload.chunk_index === undefined && payload.chunk_count === undefined) return { chunk_index: null, chunk_count: null, chunk: '' }
  const index = payload.chunk_index, count = payload.chunk_count
  if (!Number.isInteger(index) || !Number.isInteger(count) || Number(index) < 0 || Number(count) < 1 || Number(count) > 256 || Number(index) >= Number(count)) throw new ReadProblem('invalid')
  return { chunk_index: Number(index), chunk_count: Number(count), chunk: `${Number(index) + 1}/${count}` }
}
function optionalFields(payload: Record<string, unknown>): Pick<Event, 'item_id' | 'state' | 'exit_code' | 'paths'> {
  const fields: Pick<Event, 'item_id' | 'state' | 'exit_code' | 'paths'> = {}
  if (payload.item_id !== undefined && payload.item_id !== '') {
    const item = boundedText(payload.item_id, 256)
    if (item !== payload.item_id) throw new ReadProblem('invalid')
    fields.item_id = item
  }
  if (payload.state !== undefined) fields.state = boundedText(payload.state, 128)
  if (payload.exit_code !== undefined) {
    if (payload.exit_code !== null && !Number.isSafeInteger(payload.exit_code)) throw new ReadProblem('invalid')
    fields.exit_code = payload.exit_code as number | null
  }
  if (payload.paths !== undefined) {
    if (!Array.isArray(payload.paths) || payload.paths.length > 16) throw new ReadProblem('invalid')
    fields.paths = payload.paths.map((value) => boundedText(value, 512))
  }
  return fields
}
function outputFields(payload: Record<string, unknown>, kind: unknown): Pick<Event, 'output_text' | 'output_truncated'> {
  if (payload.output_text === undefined && payload.output_truncated === undefined) return {}
  if (!['command.completed', 'command.output'].includes(String(kind))) throw new ReadProblem('invalid')
  return { output_text: boundedText(payload.output_text, 8192), output_truncated: boolean(payload.output_truncated) }
}
function eventRecord(value: unknown, session: string, turn: string): Event {
  const row = record(value), payload = record(row.payload)
  if (row.kind === 'command.output' && (payload.exit_code !== undefined || payload.state !== undefined)) throw new ReadProblem('invalid')
  if (row.session_id !== session || !Number.isSafeInteger(row.cursor) || Number(row.cursor) < 1 || (turn && row.turn_id !== turn)) throw new ReadProblem('invalid')
  const original = typeof payload.text === 'string' ? payload.text : typeof payload.reason === 'string' ? payload.reason : ''
  const evidence = EVIDENCE_KINDS.includes(String(row.kind)) ? evidenceFields(payload, String(row.kind)) : {}
  return { cursor: Number(row.cursor), event_id: taskId(row.event_id), turn_id: taskId(row.turn_id), kind: displayText(row.kind),
    text: boundedText(original), truncated: payload.truncated === true, ...chunkFields(payload), ...optionalFields(payload), ...evidence,
    ...outputFields(payload, row.kind),
    created_at: row.created_at === undefined ? null : timestamp(row.created_at) }
}
export function eventPage(value: unknown, session: string, after: number, turn = ''): Events {
  const row = record(value)
  if (!Array.isArray(row.data) || row.data.length > 50) throw new ReadProblem('invalid')
  const data = row.data.map((item) => eventRecord(item, session, turn))
  let position = after
  for (const event of data) {
    if (event.cursor <= position) throw new ReadProblem('invalid')
    position = event.cursor
  }
  if (row.next_cursor !== position || new Set(data.map((event) => event.event_id)).size !== data.length) throw new ReadProblem('invalid')
  return { data, next_cursor: position }
}
export function appendEvents(previous: Events | null, next: Events, maxBytes = MAX_EVENT_BYTES): { events: Events; trimmed: boolean } {
  if (!Number.isSafeInteger(maxBytes) || maxBytes < 1 || maxBytes > MAX_EVENT_BYTES) throw new ReadProblem('invalid')
  const data = [...(previous?.data ?? []), ...next.data]
  let bytes = data.reduce((total, event) => total + new TextEncoder().encode(JSON.stringify(event)).length, 0), removed = 0
  while (data.length - removed > MAX_EVENTS || bytes > maxBytes) {
    bytes -= new TextEncoder().encode(JSON.stringify(data[removed])).length
    removed++
  }
  return { events: { data: data.slice(removed), next_cursor: next.next_cursor }, trimmed: removed > 0 }
}
