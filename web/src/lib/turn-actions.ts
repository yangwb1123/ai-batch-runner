import { turnQuery, turnBoundary, type TurnQuery } from './turn-query'
import { turnHistoryReset, type Execute, type Patch, type StatePort, type Work } from './task-state'

export function createTurnActions(port: StatePort, execute: Execute, history: (before: string) => Work, cancelRead: () => void) {
  async function change(patch: Patch) {
    if (port.get().busy || !port.get().access?.task_access || !port.get().selectedSession) return
    cancelRead(); port.set({ ...patch, turns: null, turnHistoryReadAt: null })
    await execute(history(port.get().turnHistoryCursor))
  }
  async function search(value: TurnQuery) {
    try { await change({ ...turnHistoryReset(), turnQuery: turnQuery(value) }) }
    catch { port.set({ problem: '轮次搜索条件无效。请输入最多128个字符的可见指令内容或轮次ID。' }) }
  }
  async function navigate(direction: 'first' | 'previous' | 'next') {
    const state = port.get()
    if (direction === 'first') { await change({ ...turnHistoryReset(), turnQuery: state.turnQuery }); return }
    if (direction === 'previous') {
      const previous = state.turnHistoryTrail.at(-1)
      if (previous) await change({ turnHistoryCursor: previous.before, turnHistoryLower: previous.lower,
        turnHistoryTrail: state.turnHistoryTrail.slice(0, -1), turnHistoryPage: state.turnHistoryPage - 1 })
      return
    }
    const next = state.turns?.next_cursor, last = state.turns?.data.at(-1)
    if (!next || !last) return
    if (next === state.turnHistoryCursor || state.turnHistoryTrail.some((page) => page.before === next) || !Number.isSafeInteger(state.turnHistoryPage + 1)) {
      port.set({ turns: null, turnHistoryReadAt: null, problem: '轮次分页没有推进，请重新搜索或返回首页。' }); return
    }
    await change({ turnHistoryCursor: next, turnHistoryLower: turnBoundary(last), turnHistoryPage: state.turnHistoryPage + 1,
      turnHistoryTrail: [...state.turnHistoryTrail, { before: state.turnHistoryCursor, lower: state.turnHistoryLower }].slice(-32) })
  }
  return { search, navigate }
}
