import { projectConversation } from './conversation'
import type { Event } from './task-events'
import type { Tasks } from './task-state'
import { projectResults, resultContext } from './task-results'
import { renderTurnReport } from './task-report'

export const MAX_RECORD_BYTES = 2 * 1024 * 1024
export type ExportTarget = Readonly<{ epoch: number; context: string; session: string; instance: string; turn: string }>

export function exportTarget(snapshot: Tasks): ExportTarget | null {
  const context = resultContext(snapshot)
  if (!context || snapshot.busy || snapshot.refreshing || snapshot.syncStopped || snapshot.readAt === null) return null
  return Object.freeze({ epoch: snapshot.contextEpoch, context: snapshot.access!.context,
    session: context.detail.session_id, instance: context.detail.instance_id, turn: context.turn.turn_id })
}

export function exportMatches(snapshot: Tasks, target: ExportTarget): boolean {
  const current = exportTarget(snapshot)
  return !!current && current.epoch === target.epoch && current.context === target.context &&
    current.session === target.session && current.instance === target.instance && current.turn === target.turn
}

function exportEvent(event: Event) {
  const { cursor, event_id, turn_id, kind, text, truncated, chunk_index, chunk_count, created_at,
    item_id, state, exit_code, paths, reason, evidence_partial, sha256, size_bytes, media_type, output_text, output_truncated } = event
  return { cursor, event_id, turn_id, kind, text, truncated, chunk_index, chunk_count, created_at,
    item_id, state, exit_code, paths, reason, evidence_partial, sha256, size_bytes, media_type, output_text, output_truncated }
}

function turnRecord(snapshot: Tasks) {
  const context = projectResults(snapshot)
  if (!context || !exportTarget(snapshot)) {
    throw new Error('请先刷新并核对当前任务，再下载已读取的轮次记录。')
  }
  const { detail, turn, events } = context
  const conversation = projectConversation(events.data)
  const record = { schema: 'abr.turn-record.v1', exported_at: new Date().toISOString(),
    session: { session_id: detail.session_id, instance_id: detail.instance_id, project_id: detail.project_id, name: detail.name },
    turn: { turn_id: turn.turn_id, session_id: turn.session_id, instance_id: turn.instance_id, state: turn.state,
      created_at: turn.created_at, updated_at: turn.updated_at, prompt: turn.prompt, prompt_truncated: turn.prompt_truncated },
    replies: conversation.messages.map(({ id, text, complete, partial }) => ({ id, text, complete, partial })),
    events: events.data.map(exportEvent),
    coverage: { scope: 'currently_loaded_events', observed_at: snapshot.readAt, first_cursor: events.data[0]?.cursor ?? null,
      next_cursor: events.next_cursor, earlier_events_dropped: snapshot.eventsTrimmed, more_events_available: snapshot.eventsMore,
      activity_completion_missing: context.activityCompletionMissing,
      reply_completion_missing: context.replyCompletionMissing,
      command_output_partial: context.commandOutputPartial,
      command_output_display_limited: context.commandOutputDisplayLimited,
      event_payload_partial: events.data.some((event) => event.truncated || event.evidence_partial), replies_partial: conversation.partial,
      prompt_truncated: turn.prompt_truncated },
    verification: { goal_acceptance: 'not_independently_verified', file_contents: 'not_read_or_verified',
      evidence_source: 'gateway_reported_events', command_exit_zero_is_validation: false },
  }
  return record
}

export type TurnRecord = ReturnType<typeof turnRecord>

function download(text: string, turn: string, extension: string) {
  if (new TextEncoder().encode(text).length > MAX_RECORD_BYTES) throw new Error('本轮记录超过下载容量上限，未生成文件。仍可复制需要的回复。')
  const safeId = /^[A-Za-z0-9._-]{1,128}$/.test(turn) ? turn : 'selected'
  return { text, filename: `abr-turn-${safeId}.${extension}` }
}

export function exportTurnRecord(snapshot: Tasks) {
  const record = turnRecord(snapshot)
  return download(JSON.stringify(record, null, 2) + '\n', record.turn.turn_id, 'json')
}

export function exportTurnReport(snapshot: Tasks) {
  const record = turnRecord(snapshot)
  return { ...download(renderTurnReport(record), record.turn.turn_id, 'txt'), mime: 'text/plain;charset=utf-8' }
}
