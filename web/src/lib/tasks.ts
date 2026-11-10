import { writable } from 'svelte/store'
import type { Fetcher } from './api'
import { taskId } from './task-models'
import { initial, selection, turnHistoryReset, type Patch, type Tasks } from './task-state'
import { createTaskReader } from './task-reader'
import { createTaskActions } from './task-actions'
import { createTaskLoop } from './task-loop'
import { createTaskExecution } from './task-execution'
import { createCatalogActions } from './task-catalog-actions'
import { createTurnActions } from './turn-actions'
import { createRecapActions } from './recap-actions'
import { recapReset, type RecapDirection } from './task-recap'
export type { Tasks } from './task-state'

export function createTasks({ fetcher = fetch, timeoutMs = 12000, pollIntervalMs = 2000 }:
  { fetcher?: Fetcher; timeoutMs?: number; pollIntervalMs?: number } = {}) {
  if (!Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 12000 ||
      !Number.isInteger(pollIntervalMs) || pollIntervalMs < 10 || pollIntervalMs > 60000) throw new RangeError('invalid task request budget')
  let state: Tasks = initial(), closed = false
  const store = writable<Tasks>(state)
  const set = (patch: Patch) => { state = { ...state, ...patch }; store.set(state) }
  const port = { get: () => state, set }
  const reader = createTaskReader(port, fetcher)
  const loop = createTaskLoop(port, async () => { await sync() }, pollIntervalMs)
  const execution = createTaskExecution(port, timeoutMs, loop.completed)
  const { execute } = execution
  const catalog = createCatalogActions(port, execute, reader.refresh, execution.cancelRead)
  const turns = createTurnActions(port, execute, reader.turnHistory, execution.cancelRead)
  const readRecap = createRecapActions(port, execute, reader.readRecap, execution.cancelRead)
  const actions = createTaskActions(port, execute, fetcher, reader.access, sync)

  async function sync() { if (!closed) await execute(reader.refresh, { background: true }) }
  async function refresh() { if (!closed) await execute(reader.refresh) }
  async function open(id: string, after?: number) {
    if (closed || state.busy || !state.access?.task_access) return
    execution.cancelRead()
    const same = state.selectedSession === taskId(id)
    if (same && after !== undefined && after === state.events?.next_cursor) { await execute(reader.load); return }
    await openTarget(id)
  }
  async function openTarget(id: string, turn = '') {
    if (closed || state.busy || !state.access?.task_access) return
    const session = taskId(id), selectedTurn = turn ? taskId(turn) : ''
    execution.cancelRead()
    set({ ...selection(), selectedSession: session, selectedTurn, turnMode: selectedTurn ? 'fixed' : 'live' })
    await execute(reader.select)
  }
  async function selectTurn(id: string) {
    if (closed || state.busy || !state.selectedSession || !state.access?.task_access) return
    execution.cancelRead()
    set({ selectedTurn: id ? taskId(id) : '', turnMode: id ? 'fixed' : 'live', liveTurn: null, events: null, eventsMore: false, eventsTrimmed: false,
      currentTurn: state.turns?.data.find((turn) => turn.turn_id === id) ?? null })
    await execute(reader.select)
  }
  async function loadEvents() { if (!closed && state.selectedSession) await execute(reader.load) }
  async function recapRead(direction: RecapDirection) { if (!closed) await readRecap(direction) }
  async function history(before = '') { if (!closed) await execute(reader.history(before)) }
  async function turnHistory(before = '') {
    if (closed || state.busy) return
    execution.cancelRead()
    if (!before) set(turnHistoryReset())
    await execute(reader.turnHistory(before))
  }
  function clearSelection() {
    if (state.busy || closed) return
    execution.cancelRead(); set(selection())
  }
  function forget() {
    execution.forget(); loop.setActive(false); reader.forget()
    state = initial(state.contextEpoch + 1); store.set(state)
  }
  function setActive(active: boolean) {
    if (closed) return
    if (!active) { execution.cancelRead(); set(recapReset()) }
    loop.setActive(active)
  }
  return { subscribe: store.subscribe, refresh, sync, open, openTarget, selectTurn, returnToLive: () => selectTurn(''), loadEvents, history, turnHistory, recapRead,
    turnSearch: turns.search, turnNavigate: turns.navigate,
    catalogSearch: catalog.search, catalogNavigate: catalog.navigate,
    act: actions.act, retry: actions.retry, forget, clearSelection, setActive,
    dispose() { closed = true; execution.dispose(); loop.dispose(); forget() } }
}
