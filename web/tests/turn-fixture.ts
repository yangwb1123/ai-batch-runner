import { createTasks } from '../src/lib/tasks'
import { fixture, json, turn } from './task-fixture'

export const historicalTurns = () => [turn, ...Array.from({ length: 44 }, (_, index) => ({ ...turn,
  turn_id: `older-${index}`, created_at: turn.created_at - index - 1, state: index % 2 ? 'failed' : 'completed',
  prompt: `Visible historical instruction ${index} password=[REDACTED] literal_%_` }))]
export function turnFixture(override: (path: string, init?: RequestInit) => Response | Promise<Response> | undefined = () => undefined) {
  const rows = historicalTurns(), requests: string[] = []
  const tasks = createTasks({ fetcher: fixture((path, init) => {
    requests.push(path)
    const custom = override(path, init)
    if (custom) return custom
    const url = new URL(path, 'https://abr.test')
    if (url.pathname === '/web/api/sessions/session-a/turns') {
      const query = url.searchParams, id = query.get('turn_id'), text = (query.get('q') || '').toLowerCase(), state = query.get('state') || 'all'
      const matches = rows.filter((item) => id ? item.turn_id === id : (state === 'all' || item.state === state) &&
        [item.prompt, item.turn_id].some((value) => value.toLowerCase().includes(text)))
      const start = Number((query.get('before') || 'page0').slice(4)), end = start + (id ? 1 : Number(query.get('limit') || 20))
      return json({ data: matches.slice(start, end), next_cursor: end < matches.length ? `page${end}` : '' })
    }
    if (url.pathname.startsWith('/web/api/turns/')) {
      const found = rows.find((item) => item.turn_id === url.pathname.split('/').at(-1))
      return found ? json({ data: found }) : json({}, 404)
    }
  }) })
  return { tasks, rows, requests, ready: async () => { await tasks.refresh(); await tasks.open('session-a') } }
}
