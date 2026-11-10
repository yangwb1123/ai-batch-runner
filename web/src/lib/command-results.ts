import type { Event } from './task-events'

export type CommandRow = Event & { key: string; exit_code: number | null; output_display_limited?: boolean }
const encoder = new TextEncoder()
const MAX_DISPLAY_BYTES = 262144

function outputPrefix(value: string, maximum: number): string {
  if (encoder.encode(value).length <= maximum) return value
  const result: string[] = []
  for (const char of value) {
    maximum -= encoder.encode(char).length
    if (maximum < 0) break
    result.push(char)
  }
  return result.join('')
}

function mergeCommand(event: Event, key: string, previous?: CommandRow): CommandRow {
  if (event.kind === 'command.started') return { ...event, key, exit_code: null }
  const row = { ...previous, ...event, key, text: event.text || previous?.text || '',
    exit_code: event.exit_code ?? null,
    truncated: event.truncated || previous?.truncated === true,
    evidence_partial: event.evidence_partial || previous?.evidence_partial === true }
  if (event.kind === 'command.output') {
    row.kind = previous?.kind ?? 'command.output'
    row.state = previous?.state
    row.exit_code = previous?.exit_code ?? null
  }
  return row
}

/** Snapshot output never changes execution state or joins unrelated legacy records. */
export function projectCommandRows(events: readonly Event[], terminal: boolean): CommandRow[] {
  const records = new Map<string, CommandRow>()
  for (const event of events) {
    if (!['command.started', 'command.completed', 'command.output'].includes(event.kind)) continue
    const key = event.item_id ? `item:${event.item_id}` : `event:${event.event_id}`
    records.set(key, mergeCommand(event, key, records.get(key)))
  }
  let remaining = MAX_DISPLAY_BYTES
  return [...records.values()].map((row) => {
    if (row.output_text !== undefined) {
      const text = outputPrefix(row.output_text, remaining)
      remaining -= encoder.encode(text).length
      row.output_display_limited = text !== row.output_text
      row.output_truncated ||= text !== row.output_text
      row.output_text = text
    }
    if (terminal && row.kind !== 'command.completed') {
      row.state = 'unknown'
      row.evidence_partial = true
    }
    return row
  })
}
