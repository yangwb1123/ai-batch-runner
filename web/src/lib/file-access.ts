import { type Fetcher, ReadProblem } from './api'
import { taskRequest, TaskHTTPError } from './task-api'
import { accessRecord, record, FILE_READ_SCOPE } from './task-models'
import type { FileTarget } from './file-snapshots'

export function fileSnapshotPath(target: FileTarget): string {
  return `/web/api/sessions/${encodeURIComponent(target.session)}/turns/${encodeURIComponent(target.turn)}/file-snapshots`
}
export async function authorizeFileRead(target: FileTarget, signal: AbortSignal, fetcher: Fetcher) {
  const access = accessRecord(record(await taskRequest('/web/api/access', signal, fetcher)).data)
  if (access.context !== target.context || !access.task_access || !access.file_snapshots || !access.scopes.includes(FILE_READ_SCOPE)) throw new TaskHTTPError(403)
}
export const fileAccessDenied = (error: unknown): boolean => error instanceof TaskHTTPError && [401, 403].includes(error.status)
export function fileReadProblem(error: unknown): string {
  if (fileAccessDenied(error)) return '文件读取权限或登录已变化，已清除文件内容。请刷新任务访问权限。'
  if (error instanceof ReadProblem && error.kind === 'invalid') return '文件响应或 SHA-256 校验失败，已拒绝预览和下载。'
  if (error instanceof ReadProblem && error.kind === 'limited') return '差异记录超过下载容量上限，未生成文件。'
  if (error instanceof TaskHTTPError && error.status === 429) return '文件读取受限，请稍后手动重试。'
  if (error instanceof TaskHTTPError && error.status === 503) return '读取超出服务预算或服务暂不可用。请缩小搜索条件或稍后手动重试。'
  return '未能在读取窗口内取得文件快照。请刷新快照后重试。'
}
