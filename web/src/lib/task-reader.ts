import { ReadProblem, readList, type Fetcher } from './api'
import { accessRecord, operationPage, receiptRecord, record, type Access } from './task-models'
import { taskRequest, TaskHTTPError } from './task-api'
import { createSelectionReader } from './task-selection'
import { facts, selection, catalogReset, type Patch, type StatePort, type Work } from './task-state'
import { cursor } from './task-values'
import { CatalogReadProblem, catalogPage, catalogPath } from './task-catalog'
import { createRecapReader } from './task-recap'

export function createTaskReader(port: StatePort, fetcher: Fetcher) {
  let context = ''
  const binding = (permission: Access) => JSON.stringify([permission.context, permission.mode, permission.task_access, [...permission.scopes].sort()])
  const read = (path: string, signal: AbortSignal) => taskRequest(`/web/api${path}`, signal, fetcher)
  const access = async (signal: AbortSignal) => accessRecord(record(await read('/access', signal)).data)
  const selected = createSelectionReader(port, read)
  const recap = createRecapReader(port, read)

  function observe(permission: Access) {
    const state = port.get()
    if (context && context !== binding(permission)) {
      port.set({ ...facts(), ...selection(), ...catalogReset(), contextEpoch: state.contextEpoch + 1,
        pending: null, receipt: null, tracking: null, problem: '登录上下文已切换，请重新选择任务。先前未知写入不会重放。' })
    }
    context = binding(permission)
  }
  async function authorized(work: (permission: Access) => Promise<Patch>, signal: AbortSignal, stamp = true): Promise<Patch> {
    const permission = await access(signal)
    signal.throwIfAborted(); observe(permission)
    const patch = await work(permission), current = await access(signal)
    if (binding(current) !== binding(permission)) throw new TaskHTTPError(403)
    return { ...patch, access: current, initialized: true, ...(stamp ? { readAt: Date.now() } : {}) }
  }
  async function track(signal: AbortSignal): Promise<Patch> {
    const pending = port.get().tracking
    if (!pending) return {}
    const route = pending.kind === 'create' ? 'session-requests' : 'session-close-requests'
    const receipt = receiptRecord(await read(`/${route}/${encodeURIComponent(pending.id)}`, signal), pending.kind, pending.target)
    if (receipt.id !== pending.id) throw new TaskHTTPError(409)
    const opening = pending.kind === 'create' && receipt.status === 'created'
    return { receipt, tracking: receipt.status === 'queued' ? pending : null,
      ...(opening ? { ...selection(), selectedSession: receipt.session_id } : {}) }
  }
  async function catalog(signal: AbortSignal): Promise<Patch> {
    const state = port.get(), query = state.catalogQuery
    try {
      const raw = await read(catalogPath(query, state.catalogAfter), signal)
      const page = catalogPage(raw, query, state.catalogAfter, state.catalogLower)
      if (page.next_cursor && state.catalogTrail.some((item) => item.after === page.next_cursor)) throw new TaskHTTPError(409)
      return { catalog: page, sessions: page.data }
    } catch (error) {
      if ((error instanceof TaskHTTPError && [400, 409].includes(error.status)) ||
          (error instanceof ReadProblem && error.kind === 'invalid')) throw new CatalogReadProblem()
      throw error
    }
  }
  const refresh: Work = (signal) => authorized(async (permission) => {
    const [instances, directory] = await Promise.all([
      readList('instances', signal, fetcher), permission.task_access ? catalog(signal)
        : readList('sessions', signal, fetcher).then((sessions) => ({ sessions, catalog: null })),
    ])
    if (!permission.task_access) return { ...selection(), ...catalogReset(), instances, ...directory, operations: null }
    const tracked = await track(signal)
    signal.throwIfAborted()
    // Read with confirmed request facts, but publish only after the closing identity check.
    const effective = { ...port.get(), ...tracked }
    const [operations, detail] = await Promise.all([
      read('/session-operations?limit=20', signal).then((value) => operationPage(value)), selected.selected(signal, true, effective),
    ])
    return { instances, ...directory, operations, ...tracked, ...detail }
  }, signal)
  const select: Work = (signal) => authorized((permission) => permission.task_access
    ? selected.selected(signal, false) : Promise.reject(new TaskHTTPError(403)), signal)
  const load: Work = (signal) => authorized((permission) => permission.task_access
    ? selected.selected(signal) : Promise.reject(new TaskHTTPError(403)), signal)
  const history = (before: string): Work => (signal) => authorized(async (permission) => {
    if (!permission.task_access) throw new TaskHTTPError(403)
    const raw = await read(`/session-operations?limit=20&before=${encodeURIComponent(cursor(before))}`, signal)
    return { operations: operationPage(raw, before) }
  }, signal)
  const turnHistory = (before: string): Work => (signal) => authorized((permission) => permission.task_access
    ? selected.history(before, signal) : Promise.reject(new TaskHTTPError(403)), signal)
  const readRecap: Work = (signal) => authorized((permission) => permission.task_access
    ? recap(signal) : Promise.reject(new TaskHTTPError(403)), signal, false)
  return { refresh, select, load, history, turnHistory, readRecap, access, forget() { context = '' } }
}
