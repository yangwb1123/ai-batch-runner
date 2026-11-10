import type { Fetcher } from '../src/lib/api'

export const access = { mode: 'control', task_access: true, context: 'a'.repeat(64),
  scopes: ['agent.instances:read', 'agent.sessions:read', 'agent.sessions:write', 'agent.turns:cancel'] }
export const detail = { session_id: 'session-a', instance_id: 'alpha', name: 'task', project_id: 'fixed', status: 'idle',
  online: true, controllable: true, frozen: false, active_turn_id: '' }
export const turn = { turn_id: 'turn-a', session_id: 'session-a', instance_id: 'alpha', state: 'completed',
  created_at: 1700000000, updated_at: 1700000001, prompt: 'inspect', prompt_truncated: false }
export const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } })
export const pause = (ms = 10) => new Promise((resolve) => setTimeout(resolve, ms))

export function fixture(overrides: (path: string, init?: RequestInit) => Response | Promise<Response> | undefined = () => undefined): Fetcher {
  return async (url, init) => {
    const path = String(url), custom = overrides(path, init)
    if (custom) return custom
    if (path === '/web/api/access') return json({ data: access })
    if (path.includes('/instances?')) return json({ data: [{ instance_id: 'alpha', online: true, session_capacity: 4 }] })
    if (path.includes('/task-sessions?')) return json({ data: [{ ...detail, updated_at: 1700000000,
      category: 'completed', latest_turn: { turn_id: turn.turn_id, state: turn.state, created_at: turn.created_at, updated_at: turn.updated_at } }], next_cursor: '' })
    if (path.includes('/sessions?')) return json({ data: [detail] })
    if (path.includes('/session-operations?')) return json({ data: [], next_cursor: '' })
    if (path.includes('/sessions/session-a/turns?')) return json({ data: [turn], next_cursor: '' })
    if (path.includes('/events?')) {
      const after = Number(new URL(path, 'https://abr.test').searchParams.get('after'))
      return json({ data: [], next_cursor: after })
    }
    if (path === '/web/api/sessions/session-a') return json({ data: detail })
    if (path === '/web/api/turns/turn-a') return json({ data: turn })
    throw new Error(`unexpected fixture route: ${path}`)
  }
}

export function event(cursor: number, text = 'output') {
  return { cursor, event_id: `event-${cursor}`, session_id: 'session-a', turn_id: 'turn-a',
    kind: 'message.delta', payload: { text, chunk_index: 0, chunk_count: 1 } }
}
export function eventPages(total: number, text = 'output') {
  return (path: string) => {
    if (!path.includes('/events?')) return undefined
    const after = Number(new URL(path, 'https://abr.test').searchParams.get('after'))
    const data = Array.from({ length: Math.min(50, Math.max(0, total - after)) }, (_, index) => event(after + index + 1, text))
    return json({ data, next_cursor: data.at(-1)?.cursor ?? after })
  }
}
