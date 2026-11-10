import { failure, type Execute, type RequestOptions, type StatePort, type Work } from './task-state'

export function createTaskExecution(port: StatePort, timeoutMs: number,
  completed: (error?: unknown, writing?: boolean) => void) {
  let generation = 0, closed = false, writing = false
  let controller: AbortController | null = null
  function cancelRead() {
    if (writing || !controller) return
    generation++; controller.abort(); controller = null
    port.set({ busy: false, refreshing: false })
  }
  const execute: Execute = async (work: Work, options: RequestOptions = {}) => {
    if (closed || port.get().busy || (controller && options.background)) return false
    cancelRead()
    const current = ++generation, request = new AbortController()
    controller = request; writing = !!options.writing
    port.set({ busy: !options.background, refreshing: !!options.background, problem: '' })
    const timer = setTimeout(() => request.abort(), timeoutMs)
    let issue: unknown, succeeded = false
    try {
      const patch = await work(request.signal)
      request.signal.throwIfAborted()
      if (!closed && current === generation) { port.set(patch); succeeded = true }
    } catch (error) {
      issue = error; request.abort()
      if (!closed && current === generation) port.set(failure(port.get(), error, options))
    } finally {
      clearTimeout(timer)
      if (current === generation) {
        controller = null; writing = false
        port.set({ busy: false, refreshing: false })
        if (!closed) completed(issue, options.writing)
      }
    }
    return succeeded
  }
  return { execute, cancelRead,
    forget() { generation++; controller?.abort(); controller = null; writing = false },
    dispose() { closed = true; generation++; controller?.abort(); controller = null } }
}
