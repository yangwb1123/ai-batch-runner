import { ReadProblem, type Fetcher } from './api'
import { authorizeFileRead, fileReadProblem, fileSnapshotPath, fileAccessDenied } from './file-access'
import { compareFiles, emptyComparison, filePairs, terminalTurn, type Comparison, type CompareView } from './file-comparison'
import { fileSet, fileBytes, type FileTarget } from './file-snapshots'
import { taskRequest } from './task-api'
import type { TurnStatus } from './task-history'
import { checkedTurnPage, defaultTurnQuery, turnHistoryPath, turnQuery, type TurnQuery } from './turn-query'

type Page = { cursor: string; rows: TurnStatus[] }
const invalid = (): never => { throw new ReadProblem('invalid') }
export function createComparisonReader(current: () => FileTarget | null, update: (view: CompareView) => void,
  save: (comparison: Comparison) => void, release: () => void, fetcher: Fetcher = fetch, denied: (problem: string) => void = () => {}) {
  let view = emptyComparison(), key = '', pages: Page[] = []
  let active: AbortController | null = null, timer: ReturnType<typeof setTimeout> | undefined
  const publish = (next: CompareView) => { view = next; update(next) }
  function clear() {
    active?.abort(); active = null; clearTimeout(timer); release(); pages = []; publish(emptyComparison())
  }
  function bind() {
    const next = current()?.key ?? ''
    if (next !== key) { key = next; clear() }
  }
  async function run(work: (target: FileTarget, signal: AbortSignal) => Promise<Partial<CompareView>>, patch: Partial<CompareView>, download = false) {
    bind()
    const target = current()
    if (!target) return
    active?.abort(); clearTimeout(timer); release()
    const controller = new AbortController()
    active = controller; timer = setTimeout(() => controller.abort(), 12000)
    const live = () => active === controller && current()?.key === target.key
    publish({ ...view, ...patch, busy: true, problem: '', feedback: '' })
    try {
      await authorizeFileRead(target, controller.signal, fetcher)
      if (!live()) return
      const result = await work(target, controller.signal)
      if (!live()) return
      await authorizeFileRead(target, controller.signal, fetcher)
      controller.signal.throwIfAborted()
      if (!live()) return
      if (download && result.comparison) save(result.comparison)
      publish({ ...view, ...result, busy: false, feedback: download ? '已重新核对双方权限与摘要并下载差异记录。' : '' })
    } catch (error) {
      if (live()) {
        pages = []; release(); publish({ ...emptyComparison(), problem: fileReadProblem(error) })
        if (fileAccessDenied(error)) denied(fileReadProblem(error))
      }
    } finally { controller.abort(); if (active === controller) { clearTimeout(timer); active = null } }
  }
  async function history(direction: 'reset' | 'next' | 'previous' = 'reset', query?: TurnQuery) {
    bind()
    let applied: TurnQuery
    try { applied = turnQuery(direction === 'reset' ? query ?? defaultTurnQuery() : view.query) }
    catch { clear(); publish({ ...emptyComparison(), problem: '基准轮次搜索条件无效，请核对长度和状态。' }); return }
    const index = direction === 'reset' ? 0 : view.page + (direction === 'next' ? 1 : -1)
    if (index < 0 || index >= 32 || (direction === 'next' && !view.next)) return
    const cursor = direction === 'reset' ? '' : direction === 'next' ? view.next : pages[index]?.cursor
    if (cursor === undefined) return
    const previous = direction === 'reset' ? [] : pages.slice(0, index)
    await run(async (target, signal) => {
      const allPrevious = previous.flatMap((item) => item.rows)
      const path = '/web/api' + turnHistoryPath(target.session, cursor, applied)
      const page = checkedTurnPage(await taskRequest(path, signal, fetcher), target.session, target.instance, cursor, applied, allPrevious.at(-1) ?? null)
      signal.throwIfAborted()
      if (page.next_cursor && [...previous.map((item) => item.cursor), cursor].includes(page.next_cursor)) invalid()
      if (page.data.some((turn) => allPrevious.some((prior) => prior.turn_id === turn.turn_id))) invalid()
      const rows = page.data.map(({ prompt: _prompt, prompt_truncated: _truncated, ...turn }) => turn)
      pages = [...previous, { cursor, rows }]
      return { history: rows.filter((turn) => terminalTurn(turn) && turn.turn_id !== target.turn), next: page.next_cursor, page: index }
    }, { history: null, baseline: null, sets: null, pairs: [], comparison: null, next: '', query: applied })
  }
  async function choose(id: string) {
    bind()
    const baseline = view.history?.find((turn) => turn.turn_id === id)
    if (!baseline) {
      active?.abort(); active = null; clearTimeout(timer); release()
      publish({ ...view, baseline: null, sets: null, pairs: [], comparison: null, busy: false, problem: '', feedback: '' }); return
    }
    await run(async (target, signal) => {
      const beforeTarget = { ...target, turn: baseline.turn_id }
      const [left, right] = await Promise.all([taskRequest(fileSnapshotPath(beforeTarget), signal, fetcher), taskRequest(fileSnapshotPath(target), signal, fetcher)])
      const before = fileSet(left, beforeTarget), after = fileSet(right, target)
      return { baseline, sets: { before, after }, pairs: filePairs(before, after) }
    }, { baseline, sets: null, pairs: [], comparison: null })
  }
  async function compare(path: string, download = false) {
    bind()
    const { baseline, sets } = view, pair = view.pairs.find((item) => item.path === path)
    if (!baseline || !sets || !pair?.before || !pair.after) return
    await run(async (target, signal) => {
      const beforeTarget = { ...target, turn: baseline.turn_id }
      const [left, right] = await Promise.all([taskRequest(`${fileSnapshotPath(beforeTarget)}/${pair.before!.snapshot_id}`, signal, fetcher),
        taskRequest(`${fileSnapshotPath(target)}/${pair.after!.snapshot_id}`, signal, fetcher)])
      const bytes = await Promise.all([fileBytes(left, beforeTarget, sets.before, pair.before!), fileBytes(right, target, sets.after, pair.after!)])
      signal.throwIfAborted()
      return { comparison: compareFiles(target, baseline, sets, pair, bytes) }
    }, { comparison: null }, download)
  }
  return { bind, clear, history, choose, compare }
}
