/** Evidence projection only. Turn status comes from Hub, never from message wording. */
type MessageEvent = { kind: string; text: string; truncated: boolean; item_id?: string;
  chunk_index?: number | null; chunk_count?: number | null }
export type Message = { id: string; text: string; complete: boolean; partial: boolean }
type Item = Message & { next: number; count: number }
const MAX_BYTES = 262144
const encoder = new TextEncoder()

/** Inspect completion metadata in the existing event window, beyond the reply display cap. */
export function replyCompletionMissing(events: readonly MessageEvent[], state: string): boolean {
  if (['queued', 'running', 'cancel_requested'].includes(state)) return false
  const completed = new Map<string, boolean>()
  for (const event of events) {
    if (!['message.started', 'message.delta', 'message.completed'].includes(event.kind)) continue
    completed.set(event.item_id || 'legacy', event.kind === 'message.completed' &&
      (event.chunk_index ?? 0) + 1 === (event.chunk_count ?? 1))
  }
  return [...completed.values()].some((complete) => !complete)
}

function prefix(value: string, remaining: number): string {
  if (encoder.encode(value).length <= remaining) return value
  const result: string[] = []
  for (const char of value) {
    remaining -= encoder.encode(char).length
    if (remaining < 0) break
    result.push(char)
  }
  return result.join('')
}

function completed(item: Item, event: MessageEvent): string {
  const index = event.chunk_index ?? 0, count = event.chunk_count ?? 1
  if (index > 0 && item.next === 0) item.text = ''
  if (index === 0) {
    item.next = 0
    item.count = count
    item.partial = false
    item.text = ''
  }
  if (index !== item.next || count !== item.count) item.partial = true
  item.next = index + 1
  item.complete = item.next === count
  return item.text + event.text
}

export function projectConversation(events: readonly MessageEvent[]) {
  const items = new Map<string, Item>()
  let bytes = 0, legacy = false, overflow = false
  for (const event of events) {
    if (!['message.started', 'message.delta', 'message.completed'].includes(event.kind)) continue
    const id = event.item_id || 'legacy'
    legacy ||= !event.item_id
    if (!items.has(id) && items.size >= 64) { overflow = true; continue }
    const item = items.get(id) ?? { id, text: '', complete: false, partial: false, next: 0, count: 1 }
    const previous = encoder.encode(item.text).length
    let text = item.text
    if (event.kind === 'message.completed') text = completed(item, event)
    if (event.kind === 'message.delta') { text += event.text; item.complete = false }
    const bounded = prefix(text, MAX_BYTES - bytes + previous)
    item.partial ||= event.truncated || bounded !== text
    item.text = bounded
    bytes += encoder.encode(bounded).length - previous
    items.set(id, item)
  }
  const messages = [...items.values()]
  return { messages, legacy, partial: overflow || messages.some((row) => row.partial || (row.next > 0 && !row.complete)) }
}
