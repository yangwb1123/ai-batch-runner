import { ReadProblem } from './api'
import { appendEvents, eventPage, type Events } from './task-events'
import { detailRecord, record, taskId, type Detail, type Turn, type Turns } from './task-models'
import { selection, type Patch, type StatePort, type Tasks } from './task-state'
import { turnStatusRecord } from './task-history'
import { TaskHTTPError } from './task-api'
import { checkedTurnPage, exactTurnPath, latestTurnPath, turnHistoryPath, TurnReadProblem } from './turn-query'

export type ReadTask = (path: string, signal: AbortSignal) => Promise<unknown>
const MISSING_RECORD = Symbol('missing selected resource')

async function eventWindow(read: ReadTask, session: string, turn: string, prior: Events | null, signal: AbortSignal, remaining = 3): Promise<{ events: Events; eventsMore: boolean; eventsTrimmed: boolean }> {
  const path = `/sessions/${encodeURIComponent(session)}/events`
  const after = prior?.next_cursor ?? 0, filter = turn ? `&turn_id=${encodeURIComponent(turn)}` : ''
  const next = eventPage(await read(`${path}?limit=50&after=${after}${filter}`, signal), session, after, turn)
  const merged = appendEvents(prior, next), more = next.data.length === 50
  if (!more || remaining === 1) return { events: merged.events, eventsMore: more, eventsTrimmed: merged.trimmed }
  // The next cursor comes from this response; these bounded pages cannot be fetched in parallel.
  const tail = await eventWindow(read, session, turn, merged.events, signal, remaining - 1)
  return { ...tail, eventsTrimmed: tail.eventsTrimmed || merged.trimmed }
}

export function createSelectionReader(port: StatePort, read: ReadTask) {
  async function selectedRecord(path: string, signal: AbortSignal) {
    try { return await read(path, signal) }
    catch (error) {
      if (error instanceof TaskHTTPError && error.status === 404) return MISSING_RECORD
      throw error
    }
  }
  const missing = (state: Tasks): Patch => ({ ...selection(), turnMode: state.turnMode,
    problem: '任务或轮次不存在，当前选择已清除。请从任务列表重新选择；待核对的提交记录仍保留。' })
  async function turns(state: Tasks, instance: string, before: string, signal: AbortSignal) {
    try {
      const raw = await read(turnHistoryPath(state.selectedSession, before, state.turnQuery), signal)
      const page = checkedTurnPage(raw, state.selectedSession, instance, before, state.turnQuery, state.turnHistoryLower)
      if (page.next_cursor && state.turnHistoryTrail.some((item) => item.before === page.next_cursor)) throw new TurnReadProblem()
      return page
    } catch (error) {
      if (error instanceof TaskHTTPError && error.status >= 500) throw new TurnReadProblem('limited')
      if ((error instanceof TaskHTTPError && [400, 409].includes(error.status)) || (error instanceof ReadProblem && error.kind === 'invalid')) throw new TurnReadProblem()
      throw error
    }
  }
  async function exact(id: string, turn: string, instance: string, signal: AbortSignal): Promise<Turn | null> {
    const raw = await selectedRecord(exactTurnPath(id, turn), signal)
    if (raw === MISSING_RECORD) return null
    const page = checkedTurnPage(raw, id, instance)
    if (page.next_cursor || page.data.length > 1 || (page.data[0] && page.data[0].turn_id !== turn)) throw new ReadProblem('invalid')
    return page.data[0] ?? null
  }
  async function live(detail: Detail, history: Turns, fixedWindow: boolean, signal: AbortSignal): Promise<Turn | null> {
    if (detail.active_turn_id) return (!fixedWindow ? history.data.find((turn) => turn.turn_id === detail.active_turn_id) : null) ??
      await exact(detail.session_id, detail.active_turn_id, detail.instance_id, signal)
    if (!fixedWindow) return history.data[0] ?? null
    const page = checkedTurnPage(await read(latestTurnPath(detail.session_id), signal), detail.session_id, detail.instance_id)
    if (page.data.length > 1) throw new ReadProblem('invalid')
    return page.data[0] ?? null
  }
  async function status(known: Turn, detail: Detail, signal: AbortSignal): Promise<Turn | null> {
    const raw = await selectedRecord(`/turns/${encodeURIComponent(known.turn_id)}`, signal)
    if (raw === MISSING_RECORD) return null
    const status = turnStatusRecord(record(raw).data, detail.session_id, known.turn_id)
    if (status.instance_id !== detail.instance_id || status.updated_at < status.created_at) throw new ReadProblem('invalid')
    return { ...known, ...status }
  }
  async function selected(signal: AbortSignal, append = true, state: Tasks = port.get()): Promise<Patch> {
    const id = state.selectedSession
    if (!id) return {}
    const raw = await selectedRecord(`/sessions/${encodeURIComponent(taskId(id))}`, signal)
    if (raw === MISSING_RECORD) return missing(state)
    const detail = detailRecord(record(raw).data, id)
    const fixedWindow = !!state.turnHistoryCursor || !!state.turnQuery.q || state.turnQuery.state !== 'all'
    const retainedHistory = fixedWindow && state.turns
    if (retainedHistory) checkedTurnPage(retainedHistory, id, detail.instance_id, state.turnHistoryCursor, state.turnQuery, state.turnHistoryLower)
    const history = retainedHistory || await turns(state, detail.instance_id, state.turnHistoryCursor, signal)
    const liveTurn = await live(detail, history, fixedWindow, signal)
    if (detail.active_turn_id && !liveTurn) return missing(state)
    const selectedTurn = state.turnMode === 'fixed' ? state.selectedTurn : liveTurn?.turn_id ?? ''
    const retained = state.currentTurn?.turn_id === selectedTurn && state.currentTurn.instance_id === detail.instance_id &&
      state.currentTurn.session_id === id ? state.currentTurn : null
    const known = (liveTurn?.turn_id === selectedTurn ? liveTurn : null) ?? history.data.find((turn) => turn.turn_id === selectedTurn) ?? retained ??
      (selectedTurn ? await exact(id, selectedTurn, detail.instance_id, signal) : null)
    if (selectedTurn && !known) return missing(state)
    const currentTurn = known ? await status(known, detail, signal) : null
    if (selectedTurn && !currentTurn) return missing(state)
    const prior = append && state.selectedTurn === selectedTurn ? state.events : null
    const window = selectedTurn ? await eventWindow(read, id, selectedTurn, prior, signal) : { events: null, eventsMore: false, eventsTrimmed: false }
    return { detail, turns: history, selectedTurn, currentTurn,
      liveTurn: currentTurn?.turn_id === liveTurn?.turn_id ? currentTurn : liveTurn,
      ...window, eventsTrimmed: window.eventsTrimmed || (!!prior && state.eventsTrimmed), readAt: Date.now(),
      turnHistoryReadAt: retainedHistory ? state.turnHistoryReadAt : Date.now() }
  }
  async function history(before: string, signal: AbortSignal): Promise<Patch> {
    const state = port.get(), id = state.selectedSession
    if (!id) return {}
    const detail = detailRecord(record(await read(`/sessions/${encodeURIComponent(id)}`, signal)).data, id)
    return { turns: await turns(state, detail.instance_id, before, signal), turnHistoryCursor: before,
      turnHistoryReadAt: Date.now(), readAt: Date.now() }
  }
  return { selected, history }
}
