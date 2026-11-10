import { ReadProblem } from './api'
import { boolean, boundedText, cursor, member, record, taskId, timestamp, TURN_STATES } from './task-values'

export type Turn = { turn_id: string; session_id: string; instance_id: string; state: string;
  created_at: number; updated_at: number; prompt: string; prompt_truncated: boolean }
export type Turns = { data: Turn[]; next_cursor: string }
export type TurnStatus = Omit<Turn, 'prompt' | 'prompt_truncated'>

export function turnStatusRecord(value: unknown, session: string, id = ''): TurnStatus {
  const row = record(value)
  if (row.session_id !== session || (id && row.turn_id !== id)) throw new ReadProblem('invalid')
  return { turn_id: taskId(row.turn_id), session_id: taskId(row.session_id), instance_id: taskId(row.instance_id),
    state: member(row.state, TURN_STATES), created_at: timestamp(row.created_at), updated_at: timestamp(row.updated_at) }
}
export function turnRecord(value: unknown, session: string): Turn {
  const row = record(value)
  return { ...turnStatusRecord(row, session), prompt: boundedText(row.prompt), prompt_truncated: boolean(row.prompt_truncated) }
}
export function turnPage(value: unknown, session: string, before = ''): Turns {
  const row = record(value)
  if (!Array.isArray(row.data) || row.data.length > 20) throw new ReadProblem('invalid')
  const data = row.data.map((item) => turnRecord(item, session)), next = cursor(row.next_cursor, before)
  if ((next && !data.length) || new Set(data.map((turn) => turn.turn_id)).size !== data.length) throw new ReadProblem('invalid')
  for (let index = 1; index < data.length; index++) {
    if (data[index].created_at > data[index - 1].created_at) throw new ReadProblem('invalid')
  }
  return { data, next_cursor: next }
}
