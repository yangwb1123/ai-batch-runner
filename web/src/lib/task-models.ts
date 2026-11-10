import { ReadProblem, displayText } from './api'
import { boolean, member, record, taskId, TURN_STATES } from './task-values'
export { record, taskId } from './task-values'
export { eventPage } from './task-events'
export type { Event, Events } from './task-events'
export { turnPage } from './task-history'
export type { Turn, Turns } from './task-history'

export const TASK_SCOPES = ['agent.instances:read', 'agent.sessions:read', 'agent.sessions:write', 'agent.turns:cancel']
export const FILE_READ_SCOPE = 'agent.artifacts:read'
export type Access = { mode: 'control' | 'read_only'; task_access: boolean; scopes: string[]; context: string; file_snapshots?: boolean }
export type Detail = { session_id: string; instance_id: string; name: string; project_id: string; status: string;
  controllable: boolean; online: boolean; frozen: boolean; active_turn_id: string }
export type Operation = { operation: 'create' | 'close'; request_id: string; instance_id: string;
  session_id: string; name: string; status: string }
export type Operations = { data: Operation[]; next_cursor: string }
export type Receipt = { id: string; session_id: string; status: string; kind?: ActionKind; target?: string }
export type ActionKind = 'create' | 'send' | 'cancel' | 'close'
export function accessRecord(value: unknown): Access {
  const row = record(value)
  const mode = member(row.mode, ['control', 'read_only']) as Access['mode']
  const enabled = boolean(row.task_access)
  const files = row.file_snapshots === undefined ? undefined : boolean(row.file_snapshots)
  if (!Array.isArray(row.scopes) || row.scopes.length > 5 || new Set(row.scopes).size !== row.scopes.length ||
      row.scopes.some((scope) => ![...TASK_SCOPES, FILE_READ_SCOPE].includes(scope)) || typeof row.context !== 'string' || !/^[a-f0-9]{64}$/.test(row.context)) throw new ReadProblem('invalid')
  const scopes = row.scopes as string[]
  if ((!enabled && scopes.length) || (enabled && (mode !== 'control' || !TASK_SCOPES.slice(0, 2).every((scope) => scopes.includes(scope))))) throw new ReadProblem('invalid')
  if ((files === true && !enabled) || scopes.includes(FILE_READ_SCOPE) !== (files === true)) throw new ReadProblem('invalid')
  return { mode, task_access: enabled, scopes, context: row.context, ...(files === undefined ? {} : { file_snapshots: files }) }
}
export function detailRecord(value: unknown, id: string): Detail {
  const row = record(value)
  if (taskId(row.session_id) !== id) throw new ReadProblem('invalid')
  return { session_id: id, instance_id: taskId(row.instance_id), name: displayText(row.name), project_id: displayText(row.project_id),
    status: displayText(row.status), controllable: boolean(row.controllable), online: boolean(row.online), frozen: boolean(row.frozen),
    active_turn_id: row.active_turn_id === '' ? '' : taskId(row.active_turn_id) }
}
export function operationPage(value: unknown, before = ''): Operations {
  const row = record(value)
  if (!Array.isArray(row.data) || row.data.length > 20 || typeof row.next_cursor !== 'string' || !/^[A-Za-z0-9_-]{0,2048}$/.test(row.next_cursor)) throw new ReadProblem('invalid')
  if (row.next_cursor && (row.next_cursor === before || !row.data.length)) throw new ReadProblem('invalid')
  const data = row.data.map((item) => {
    const entry = record(item)
    const operation = member(entry.operation, ['create', 'close']) as Operation['operation']
    return { operation, request_id: taskId(entry.request_id), instance_id: taskId(entry.instance_id), name: displayText(entry.name, ''),
      session_id: entry.session_id === '' ? '' : taskId(entry.session_id),
      status: member(entry.status, ['queued', 'failed', 'lost', operation === 'create' ? 'created' : 'closed']) }
  })
  if (new Set(data.map((item) => `${item.operation}:${item.request_id}`)).size !== data.length) throw new ReadProblem('invalid')
  return { data, next_cursor: row.next_cursor }
}
export function receiptRecord(value: unknown, kind: ActionKind, target: string): Receipt {
  target = taskId(target)
  const row = record(record(value).data)
  if (kind === 'send' || kind === 'cancel') {
    const id = taskId(row.turn_id), session = taskId(row.session_id)
    if ((kind === 'send' && session !== target) || (kind === 'cancel' && id !== target)) throw new ReadProblem('invalid')
    return { id, session_id: session, status: member(row.state, TURN_STATES), kind, target }
  }
  if ((kind === 'create' && row.instance_id !== target) || (kind === 'close' && row.session_id !== target)) throw new ReadProblem('invalid')
  const status = member(row.status, ['queued', 'failed', 'lost', kind === 'create' ? 'created' : 'closed'])
  const session = row.session_id === '' ? '' : taskId(row.session_id)
  if ((kind === 'close' || status === 'created') && !session) throw new ReadProblem('invalid')
  return { id: taskId(row.request_id), session_id: session, status, kind, target }
}
