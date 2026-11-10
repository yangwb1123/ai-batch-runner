import { catalogQuery, CATALOG_HISTORY_LIMIT, type CatalogQuery } from './task-catalog'
import { catalogReset, type Execute, type Patch, type StatePort, type Work } from './task-state'

export function createCatalogActions(port: StatePort, execute: Execute, refresh: Work, cancelRead: () => void) {
  async function change(patch: Patch) {
    if (port.get().busy || !port.get().access?.task_access) return
    cancelRead()
    port.set({ ...patch, catalog: null, sessions: [] })
    await execute(refresh)
  }
  async function search(value: CatalogQuery) {
    if (port.get().busy || !port.get().access?.task_access) return
    try { await change({ ...catalogReset(), catalogQuery: catalogQuery(value) }) }
    catch { port.set({ problem: '搜索条件无效。请输入最多128个字符的任务名称、项目或会话ID。' }) }
  }
  async function navigate(direction: 'first' | 'previous' | 'next') {
    const state = port.get()
    if (direction === 'first') { await change({ ...catalogReset(), catalogQuery: state.catalogQuery }); return }
    if (direction === 'previous') {
      const previous = state.catalogTrail.at(-1)
      if (previous) await change({ catalogAfter: previous.after, catalogLower: previous.lower,
        catalogTrail: state.catalogTrail.slice(0, -1), catalogPage: state.catalogPage - 1 })
      return
    }
    if (direction !== 'next' || !state.catalog?.next_cursor || !state.catalog.data.length) return
    const next = state.catalog.next_cursor
    if (next === state.catalogAfter || state.catalogTrail.some((item) => item.after === next) || !Number.isSafeInteger(state.catalogPage + 1)) {
      port.set({ catalog: null, sessions: [], problem: '任务分页没有推进，请回到首页重新核对。' }); return
    }
    await change({ catalogAfter: next, catalogLower: state.catalog.data.at(-1)!.session_id,
      catalogTrail: [...state.catalogTrail, { after: state.catalogAfter, lower: state.catalogLower }].slice(-CATALOG_HISTORY_LIMIT),
      catalogPage: state.catalogPage + 1 })
  }
  return { search, navigate }
}
