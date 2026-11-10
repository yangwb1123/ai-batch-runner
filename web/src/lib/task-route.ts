import { ReadProblem } from './api'
import { taskId } from './task-values'

export type TaskRoute = { session: string; turn: string }
export function parseTaskRoute(hash: string): TaskRoute | null {
  if (!hash.startsWith('#tasks/')) return null
  if (hash.length > 5000) throw new ReadProblem('invalid')
  const parts = hash.slice(1).split('/')
  if (parts.length !== 2 && (parts.length !== 4 || parts[2] !== 'turns')) throw new ReadProblem('invalid')
  return { session: taskId(decodeURIComponent(parts[1])), turn: parts.length === 4 ? taskId(decodeURIComponent(parts[3])) : '' }
}
export function taskRouteHash(route: TaskRoute): string {
  return `#tasks/${encodeURIComponent(taskId(route.session))}` + (route.turn ? `/turns/${encodeURIComponent(taskId(route.turn))}` : '')
}
export function taskRouteUrl(route: TaskRoute, current: string): string {
  const url = new URL(current)
  url.search = ''; url.hash = taskRouteHash(route)
  return url.href
}
