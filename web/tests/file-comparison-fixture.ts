import { createComparisonReader } from '../src/lib/file-comparison-reader'
import { emptyComparison, type Comparison } from '../src/lib/file-comparison'
import { FILE_SCHEMA, type FileTarget } from '../src/lib/file-snapshots'
import { FILE_READ_SCOPE } from '../src/lib/task-models'
import { access, turn, json } from './task-fixture'
import type { Fetcher } from '../src/lib/api'

export const target: FileTarget = { key: 'target', context: access.context, instance: 'alpha', session: 'session-a', turn: 'turn-a' }
export const olderTurn = { ...turn, turn_id: 'turn-before', created_at: turn.created_at - 1 }
export const fileAccess = { ...access, file_snapshots: true, scopes: [...access.scopes, FILE_READ_SCOPE] }
export async function captured(id: string, text: string, key: string, path = 'report.txt', kind = 'text') {
  const bytes = new TextEncoder().encode(text)
  const sha256 = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), (byte) => byte.toString(16).padStart(2, '0')).join('')
  const identity = { schema: FILE_SCHEMA, verification: 'content_integrity_only', instance_id: target.instance,
    session_id: target.session, turn_id: id, snapshot_set_id: key.repeat(32), created_at: 1700000002 }
  const metadata = { snapshot_id: key.repeat(32), path, size_bytes: bytes.length, sha256, content_kind: kind }
  return { list: { data: { ...identity, state: 'ready', files: [metadata], unavailable: [] as { path: string; code: string }[] } },
    content: { data: { ...identity, ...metadata, content_b64: btoa(String.fromCharCode(...bytes)) } } }
}
export async function harness(before = 'before\n', after = 'after\n', override: (path: string, init?: RequestInit) => Response | Promise<Response> | undefined = () => undefined) {
  const left = await captured(olderTurn.turn_id, before, 'b'), right = await captured(target.turn, after, 'a')
  const state = { current: target as FileTarget | null, view: emptyComparison(), saves: [] as Comparison[], requests: [] as string[] }
  const fetcher: Fetcher = async (url, init) => {
    const path = String(url)
    state.requests.push(path)
    const custom = override(path, init)
    if (custom) return custom
    if (path === '/web/api/access') return json({ data: fileAccess })
    if (path.includes('/turns?')) return json({ data: [turn, olderTurn], next_cursor: '' })
    const data = path.includes(`/turns/${olderTurn.turn_id}/`) ? left : right
    return json(path.endsWith('/file-snapshots') ? data.list : data.content)
  }
  const reader = createComparisonReader(() => state.current, (view) => { state.view = view }, (data) => { state.saves.push(data) }, () => {}, fetcher)
  const ready = async () => { await reader.history(); await reader.choose(olderTurn.turn_id) }
  return { reader, state, left, right, ready }
}
