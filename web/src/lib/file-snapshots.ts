import { ReadProblem } from './api'
import { member, record, taskId, timestamp } from './task-values'

export const FILE_SCHEMA = 'abr.file-snapshots.v1'
export type FileTarget = { key: string; context: string; instance: string; session: string; turn: string }
export type FileMetadata = { snapshot_id: string; path: string; sha256: string; size_bytes: number; content_kind: 'text' | 'binary' }
export type FileSet = { snapshot_set_id: string | null; state: string; created_at: number | null;
  files: FileMetadata[]; unavailable: { path: string; code: string }[] }
export const unavailableLabels: Record<string, string> = {
  missing: '文件不存在', unsafe_path: '路径不安全', not_regular_file: '不是可共享的普通文件', too_large: '超过大小限制',
  read_failed: '读取失败', read_timeout: '读取超时', changed: '读取期间文件变化', secret_detected: '检测到凭据，已拒绝共享',
  turn_not_completed: '本轮没有成功结束', cancel_requested: '已请求取消', capture_unavailable: '采集不可用', capacity_exceeded: '快照容量已满',
}
const invalid = (): never => { throw new ReadProblem('invalid') }
const hex = (value: unknown, size: number): string => typeof value === 'string' && new RegExp(`^[a-f0-9]{${size}}$`).test(value) ? value : invalid()
function filePath(value: unknown): string {
  if (typeof value !== 'string' || !value || value !== value.normalize('NFC') || new TextEncoder().encode(value).length > 512 ||
      /[\p{Cc}\p{Cf}\\:]/u.test(value) || value.split('/').some((part) => !part || ['.', '..'].includes(part) || /[. ]$/.test(part))) invalid()
  return value as string
}
function binding(row: Record<string, unknown>, target: FileTarget): void {
  if (row.schema !== FILE_SCHEMA || row.verification !== 'content_integrity_only' ||
      taskId(row.instance_id) !== target.instance || taskId(row.session_id) !== target.session || taskId(row.turn_id) !== target.turn) invalid()
}
function metadata(value: unknown): FileMetadata {
  const row = record(value)
  if (typeof row.size_bytes !== 'number' || !Number.isInteger(row.size_bytes) || row.size_bytes < 0 || row.size_bytes > 65536) invalid()
  return { snapshot_id: hex(row.snapshot_id, 32), path: filePath(row.path), sha256: hex(row.sha256, 64), size_bytes: row.size_bytes as number,
    content_kind: member(row.content_kind, ['text', 'binary']) as FileMetadata['content_kind'] }
}
export function fileSet(value: unknown, target: FileTarget): FileSet {
  const row = record(record(value).data)
  binding(row, target)
  const state = member(row.state, ['absent', 'ready', 'partial', 'unavailable'])
  if (!Array.isArray(row.files) || !Array.isArray(row.unavailable) || row.files.length + row.unavailable.length > 8) invalid()
  const files = (row.files as unknown[]).map(metadata)
  const unavailable = (row.unavailable as unknown[]).map((item) => {
    const entry = record(item)
    return { path: filePath(entry.path), code: member(entry.code, Object.keys(unavailableLabels)) }
  })
  const paths = [...files, ...unavailable].map((file) => file.path.toLowerCase())
  if (new Set(paths).size !== paths.length || paths.some((path) => paths.some((other) => other.startsWith(path + '/'))) ||
      new Set(files.map((file) => file.snapshot_id)).size !== files.length || files.reduce((sum, file) => sum + file.size_bytes, 0) > 262144) invalid()
  const expected = !paths.length ? 'absent' : files.length ? unavailable.length ? 'partial' : 'ready' : 'unavailable'
  if (state !== expected) invalid()
  if (state === 'absent') {
    if (row.snapshot_set_id !== null || row.created_at !== null) invalid()
    return { snapshot_set_id: null, created_at: null, state, files, unavailable }
  }
  return { snapshot_set_id: hex(row.snapshot_set_id, 32), created_at: timestamp(row.created_at), state, files, unavailable }
}
export async function fileBytes(value: unknown, target: FileTarget, set: FileSet, file: FileMetadata): Promise<Uint8Array> {
  const row = record(record(value).data)
  binding(row, target)
  if (row.snapshot_set_id !== set.snapshot_set_id || row.created_at !== set.created_at ||
      JSON.stringify(metadata(row)) !== JSON.stringify(file)) invalid()
  if (typeof row.content_b64 !== 'string' || row.content_b64.length > 87384 || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(row.content_b64)) invalid()
  const encoded = row.content_b64 as string
  const raw = atob(encoded)
  if (btoa(raw) !== encoded || raw.length !== file.size_bytes) invalid()
  const bytes = Uint8Array.from(raw, (char) => char.charCodeAt(0))
  const digest = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), (byte) => byte.toString(16).padStart(2, '0')).join('')
  if (digest !== file.sha256) invalid()
  return bytes
}
export function rawFileText(bytes: Uint8Array, file: FileMetadata): string | null {
  if (file.content_kind === 'binary') return null
  let text: string
  try { text = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(bytes) } catch { return invalid() }
  if (/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f]/.test(text)) invalid()
  return text
}
export function visibleFileText(text: string): string {
  // Escape literal backslashes too: a control and its textual spelling stay distinct.
  return text.replace(/\\|\p{Cf}/gu, (char) => char === '\\' ? '\\\\' : `\\u{${char.codePointAt(0)!.toString(16)}}`)
}
export function previewText(bytes: Uint8Array, file: FileMetadata): string | null {
  const text = rawFileText(bytes, file)
  return text === null ? null : visibleFileText(text)
}
