import { prepareAction, taskRequest, taskError, TaskHTTPError, type Action } from './task-api'
import { receiptRecord, type Access, type ActionKind } from './task-models'
import { selection, type Execute, type Patch, type StatePort } from './task-state'
import type { Fetcher } from './api'

export function createTaskActions(port: StatePort, execute: Execute, fetcher: Fetcher,
  access: (signal: AbortSignal) => Promise<Access>, sync: () => Promise<void>) {
  async function send(action: Action, signal: AbortSignal): Promise<Patch> {
    const receipt = receiptRecord(await taskRequest(action.path, signal, fetcher, action), action.kind, action.target)
    const track = (action.kind === 'create' || action.kind === 'close') && receipt.status === 'queued'
    const picked = receipt.session_id && (action.kind === 'create' || action.kind === 'send')
    return { outcome: 'accepted', pending: null, receipt,
      tracking: track ? { kind: action.kind as 'create' | 'close', id: receipt.id, target: action.target } : null,
      ...(picked ? { ...selection(), selectedSession: receipt.session_id, turnMode: 'live',
        selectedTurn: action.kind === 'send' ? receipt.id : '' } : {}) }
  }
  async function act(kind: ActionKind, target: string, text = '') {
    const state = port.get()
    if (state.busy || state.outcome === 'unknown' || !state.access) return
    let action: Action
    try { action = prepareAction(kind, target, text, state.access) }
    catch (error) { port.set({ problem: taskError(error), outcome: 'rejected', receipt: null, pending: null }); return }
    port.set({ pending: action, receipt: null, outcome: 'none' })
    if (await execute((signal) => send(action, signal), { writing: true })) await sync()
  }
  async function retry() {
    const state = port.get(), action = state.pending
    if (state.busy || !action || state.outcome !== 'unknown') return
    const accepted = await execute(async (signal) => {
      const current = await access(signal)
      const scope = action.kind === 'cancel' ? 'agent.turns:cancel' : 'agent.sessions:write'
      if (current.context !== action.context || !current.task_access || !current.scopes.includes(scope)) throw new TaskHTTPError(403)
      return send(action, signal)
    }, { writing: true, retry: true })
    if (accepted) await sync()
  }
  return { act, retry }
}
