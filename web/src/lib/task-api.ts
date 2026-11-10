import { boundedJson, ReadProblem, type Fetcher } from './api'
import { taskId, type Access, type ActionKind } from './task-models'

export type Action = Readonly<{ kind: ActionKind; target: string; path: string; body: string; key: string; context: string }>
export class TaskHTTPError extends Error {
  constructor(public readonly status: number) { super('task request rejected') }
}

export function prepareAction(kind: ActionKind, target: string, text: string, access: Access): Action {
  const scope = kind === 'cancel' ? 'agent.turns:cancel' : 'agent.sessions:write'
  if (!access.task_access || !access.scopes.includes(scope)) throw new ReadProblem('forbidden')
  const id = encodeURIComponent(taskId(target))
  const paths = { create: `/instances/${id}/sessions`, send: `/sessions/${id}/turns`, cancel: `/turns/${id}/cancel`, close: `/sessions/${id}/close` }
  const body = kind === 'create' ? { name: text.trim() } : kind === 'send' ? { prompt: text } : {}
  if (kind === 'create' || kind === 'send') {
    const value = kind === 'create' ? text.trim() : text
    if (!value.trim() || new TextEncoder().encode(value).length > (kind === 'create' ? 256 : 16384)) throw new ReadProblem('invalid')
    if (kind === 'create' && /[\p{Cc}\p{Cf}]/u.test(value)) throw new ReadProblem('invalid')
  }
  const encoded = JSON.stringify(body)
  if (new TextEncoder().encode(encoded).length > 65536) throw new ReadProblem('invalid')
  return Object.freeze({ kind, target, path: `/web/api${paths[kind]}`, body: encoded, key: crypto.randomUUID(), context: access.context })
}

export async function taskRequest(path: string, signal: AbortSignal, fetcher: Fetcher, action?: Action): Promise<unknown> {
  const init: RequestInit = { credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal }
  if (action) Object.assign(init, { method: 'POST', body: action.body, headers: {
    'Content-Type': 'application/json', 'Idempotency-Key': action.key, 'X-ABR-Context': action.context,
  } })
  const response = await fetcher(path, init)
  signal.throwIfAborted()
  if (!response.ok || (action && response.status !== 202)) {
    await response.body?.cancel().catch(() => undefined)
    if (!response.ok) throw new TaskHTTPError(response.status)
    throw new ReadProblem('invalid')
  }
  return boundedJson(response, signal)
}

export function taskError(error: unknown): string {
  if (error instanceof ReadProblem && error.kind === 'login') return '登录已失效。旧数据已清除，请重新登录。'
  if (error instanceof ReadProblem && error.kind === 'forbidden') return '当前账号没有此操作的权限。请核对项目和授权范围。'
  if (error instanceof ReadProblem && error.kind === 'limited') return '请求受限，自动同步已暂停。请稍后手动刷新。'
  if (error instanceof TaskHTTPError) {
    if (error.status === 401) return '登录已失效。旧数据已清除，请重新登录。'
    if (error.status === 403) return '权限或登录上下文已变化。请刷新核对账号、实例和控制授权。'
    if (error.status === 409) return 'Hub 拒绝了冲突操作：请刷新核对在线状态、容量、活动轮次和请求记录，不要换 key 盲目重发。'
    if (error.status === 429) return '请求受限，请稍后手动查询。没有自动重试。'
    if (error.status >= 400 && error.status < 500) return '请求被拒绝。请核对目标、字段和当前服务支持情况。'
  }
  if (error instanceof ReadProblem && error.kind === 'invalid') return '请求或响应未通过格式、目标绑定或大小校验。'
  return '网络、服务或 12 秒读取窗口异常。写入结果可能未知，请先核对现场。'
}
