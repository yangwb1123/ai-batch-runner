import { type Fetcher } from './api'
import { taskRequest } from './task-api'
import { FILE_READ_SCOPE } from './task-models'
import { authorizeFileRead as authorized, fileReadProblem as problem, fileSnapshotPath, fileAccessDenied } from './file-access'
import type { Tasks } from './task-state'
import { fileSet, fileBytes, previewText, type FileSet, type FileMetadata, type FileTarget } from './file-snapshots'

export type FileView = { list: FileSet | null; preview: { file: FileMetadata; text: string | null } | null; busy: boolean; problem: string; feedback: string }
const empty = (): FileView => ({ list: null, preview: null, busy: false, problem: '', feedback: '' })
export function fileTarget(state: Tasks): FileTarget | null {
  const { access, detail, currentTurn: turn } = state
  if (!access?.task_access || !access.file_snapshots || !access.scopes.includes(FILE_READ_SCOPE) || !detail || !turn ||
      state.syncStopped || state.readAt === null || detail.session_id !== state.selectedSession ||
      turn.session_id !== detail.session_id || turn.turn_id !== state.selectedTurn ||
      !['completed', 'failed', 'cancelled', 'interrupted', 'lost'].includes(turn.state)) return null
  return { key: `${state.contextEpoch}/${access.context}/${detail.instance_id}/${detail.session_id}/${turn.turn_id}`,
    context: access.context, instance: detail.instance_id, session: detail.session_id, turn: turn.turn_id }
}
export function createFileReader(current: () => FileTarget | null, update: (view: FileView) => void,
  save: (file: FileMetadata, bytes: Uint8Array) => void, release: () => void, fetcher: Fetcher = fetch, denied: (problem: string) => void = () => {}) {
  let view = empty(), key = '', active: AbortController | null = null, timer: ReturnType<typeof setTimeout> | undefined
  const publish = (next: FileView) => { view = next; update(next) }
  function clear() {
    active?.abort(); active = null
    clearTimeout(timer); release(); publish(empty())
  }
  function bind() {
    const next = current()?.key ?? ''
    if (next !== key) { key = next; clear() }
  }
  async function read(file?: FileMetadata, download = false) {
    bind()
    const target = current(), set = view.list
    if (!target) return
    if (file) {
      const selected = set?.files.find((item) => item.snapshot_id === file!.snapshot_id)
      if (!selected) return
      file = selected
    }
    active?.abort(); clearTimeout(timer); release()
    const controller = new AbortController()
    active = controller
    timer = setTimeout(() => controller.abort(), 12000)
    const live = () => active === controller && current()?.key === target.key
    publish({ ...view, busy: true, preview: null, problem: '', feedback: '' })
    const path = fileSnapshotPath(target)
    try {
      await authorized(target, controller.signal, fetcher)
      if (!live()) return
      const data = await taskRequest(path + (file ? `/${file.snapshot_id}` : ''), controller.signal, fetcher)
      if (!live()) return
      if (!file) {
        const list = fileSet(data, target)
        await authorized(target, controller.signal, fetcher)
        if (live()) publish({ ...empty(), list })
      }
      else {
        const bytes = await fileBytes(data, target, set!, file)
        await authorized(target, controller.signal, fetcher)
        controller.signal.throwIfAborted()
        if (!live()) return
        const text = previewText(bytes, file)
        if (download) save(file, bytes)
        publish({ ...view, busy: false, preview: { file, text }, feedback: download ? '已核对文件摘要并发起下载。' : '' })
      }
    } catch (error) {
      if (live()) { release(); publish({ ...empty(), problem: problem(error) }); if (fileAccessDenied(error)) denied(problem(error)) }
    } finally { controller.abort(); if (active === controller) { clearTimeout(timer); active = null } }
  }
  return { bind, clear, read }
}
