import { projectConversation, replyCompletionMissing as missingReplyCompletion } from './conversation'
import type { Event } from './task-events'
import type { Tasks } from './task-state'
import { projectCommandRows, type CommandRow } from './command-results'

export type EvidenceRow = Event & { key: string }
export type { CommandRow } from './command-results'

/** Bind every displayed record to the same currently authorized selection. */
export function resultContext(snapshot: Tasks) {
  const { access, detail, currentTurn: turn, events } = snapshot
  if (!access?.task_access || !['agent.instances:read', 'agent.sessions:read'].every((scope) => access.scopes.includes(scope)) ||
      !detail || !turn || !events || snapshot.selectedSession !== detail.session_id || snapshot.selectedTurn !== turn.turn_id ||
      turn.session_id !== detail.session_id || turn.instance_id !== detail.instance_id || events.data.some((event) => event.turn_id !== turn.turn_id)) return null
  return { detail, turn, events }
}

function grouped(events: readonly Event[], kinds: readonly string[]): EvidenceRow[] {
  const records = new Map<string, EvidenceRow>()
  for (const event of events) {
    if (!kinds.includes(event.kind)) continue
    const key = event.item_id ? `item:${event.item_id}` : `event:${event.event_id}`
    const previous = records.get(key)
    records.set(key, { ...event, key, text: event.text || previous?.text || '',
      truncated: event.truncated || previous?.truncated === true,
      evidence_partial: event.evidence_partial || previous?.evidence_partial === true })
  }
  return [...records.values()]
}

function completedActivities(events: readonly Event[], kinds: readonly string[], terminal: boolean): EvidenceRow[] {
  return grouped(events, kinds).map((event) => terminal && event.kind.endsWith('.started')
    ? { ...event, state: 'unknown', evidence_partial: true } : event)
}

export function projectResults(snapshot: Tasks) {
  const context = resultContext(snapshot)
  if (!context) return null
  const events = context.events.data
  const conversation = projectConversation(events)
  const terminal = !['queued', 'running', 'cancel_requested'].includes(context.turn.state)
  const commands = projectCommandRows(events, terminal)
  const validations = completedActivities(events, ['validation.started', 'validation.completed'], terminal)
  const gates = grouped(events, ['gate.verdict'])
  const artifacts = grouped(events, ['artifact.created'])
  const changes = events.filter((event) => event.kind === 'file.changed')
  const files = [...new Set(changes.flatMap((event) => event.paths ?? []))]
  const diagnostics = events.filter((event) => ['turn.failed', 'turn.interrupted', 'turn.lost'].includes(event.kind))
  const activityCompletionMissing = terminal && [...commands, ...validations].some((event) => event.kind.endsWith('.started') || event.kind === 'command.output')
  const commandOutputPartial = events.some((event) => event.output_truncated)
  const commandOutputDisplayLimited = commands.some((event) => event.output_display_limited)
  const replyCompletionMissing = missingReplyCompletion(events, context.turn.state)
  return { ...context, commands, validations, gates, artifacts, files, changes, diagnostics, activityCompletionMissing, commandOutputPartial, commandOutputDisplayLimited, replyCompletionMissing,
    partial: snapshot.eventsTrimmed || snapshot.eventsMore || context.turn.prompt_truncated || conversation.partial ||
      activityCompletionMissing || replyCompletionMissing || commandOutputPartial || commandOutputDisplayLimited || events.some((event) => event.truncated || event.evidence_partial),
    goalAcceptance: 'not_independently_verified' as const }
}

export function evidenceLabel(state?: string): string {
  return ({ passed: '报告通过', failed: '报告失败', rejected: '报告拒绝', cancelled: '已取消',
    running: '执行中', recorded: '已记录元数据', unknown: '结果未知' } as Record<string, string>)[state ?? ''] ?? '结果未知'
}

export function commandLabel(event: CommandRow): string {
  if (event.kind === 'command.output') return '命令状态未提供'
  if (event.kind === 'command.started') return event.state === 'unknown' ? '未收到结束记录' : '执行中'
  return event.exit_code === null ? '退出码未提供' : `退出码 ${event.exit_code}`
}
