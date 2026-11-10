import { ReadProblem } from './api'
import { recapReset, type RecapDirection } from './task-recap'
import { turnBoundary } from './turn-query'
import type { Execute, Patch, StatePort, Work } from './task-state'

function position(port: StatePort, direction: RecapDirection): Patch | null {
  const state = port.get()
  if (direction === 'latest') return recapReset()
  if (direction === 'refresh') return {}
  if (direction === 'newer') {
    const previous = state.recapTrail.at(-1)
    return previous ? { recapCursor: previous.before, recapLower: previous.lower,
      recapTrail: state.recapTrail.slice(0, -1), recapPage: state.recapPage - 1 } : null
  }
  if (direction !== 'older') throw new ReadProblem('invalid')
  const next = state.recap?.next_cursor, last = state.recap?.rows.at(-1)?.turn
  if (!next || !last) return null
  if (next === state.recapCursor || state.recapTrail.some((item) => item.before === next) || !Number.isSafeInteger(state.recapPage + 1)) throw new ReadProblem('invalid')
  return { recapCursor: next, recapLower: turnBoundary(last), recapPage: state.recapPage + 1,
    recapTrail: [...state.recapTrail, { before: state.recapCursor, lower: state.recapLower }].slice(-32) }
}

export function createRecapActions(port: StatePort, execute: Execute, read: Work, cancelRead: () => void) {
  return async (direction: RecapDirection) => {
    const state = port.get()
    if (state.busy || !state.access?.task_access || !state.selectedSession) return
    const patch = position(port, direction)
    if (!patch) return
    cancelRead(); port.set({ ...patch, recap: null })
    await execute(read, { background: true })
  }
}
