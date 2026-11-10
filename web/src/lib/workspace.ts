import { writable } from 'svelte/store'
import { readList, ReadProblem, type Instance, type Session, type ProblemKind, type Fetcher } from './api'
export { readList, displayText } from './api'
export type { Instance, Session, ProblemKind } from './api'

export type Snapshot = {
  phase: 'idle' | 'loading' | 'ready' | 'error'; instances: Instance[]; sessions: Session[];
  problem: ProblemKind | null; readAt: number | null
}
const empty = (phase: Snapshot['phase'] = 'idle', problem: ProblemKind | null = null): Snapshot => ({
  phase, problem, instances: [], sessions: [], readAt: null,
})

export function createWorkspace({ fetcher = fetch, timeoutMs = 12000 }: { fetcher?: Fetcher; timeoutMs?: number } = {}) {
  const view = writable<Snapshot>(empty())
  let controller: AbortController | null = null
  let generation = 0
  let closed = false

  async function refresh() {
    if (closed || controller) return
    const current = ++generation
    const request = new AbortController()
    controller = request
    view.set(empty('loading'))
    const timer = setTimeout(() => request.abort(), timeoutMs)
    try {
      const [instances, sessions] = await Promise.all([
        readList('instances', request.signal, fetcher), readList('sessions', request.signal, fetcher),
      ])
      if (current === generation && !closed) view.set({ phase: 'ready', instances, sessions, problem: null, readAt: Date.now() })
    } catch (error) {
      const problem = request.signal.aborted ? 'timeout' : error instanceof ReadProblem ? error.kind : 'unavailable'
      request.abort() // do not leave the other read active after a partial failure
      if (current === generation && !closed) view.set(empty('error', problem))
    } finally {
      clearTimeout(timer)
      if (current === generation) controller = null
    }
  }

  function forget() {
    generation++
    controller?.abort()
    controller = null
    view.set(empty())
  }

  return { subscribe: view.subscribe, refresh, forget, dispose() { closed = true; forget() } }
}
