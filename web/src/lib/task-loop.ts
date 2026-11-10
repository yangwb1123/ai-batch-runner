import { fatalRead, type StatePort } from './task-state'

export function createTaskLoop(port: StatePort, run: () => Promise<void>, interval: number) {
  let active = false, closed = false, failures = 0, timer: ReturnType<typeof setTimeout> | null = null
  function clear() { if (timer !== null) clearTimeout(timer); timer = null }
  function schedule(immediate = false) {
    clear()
    const watching = active && !closed && !port.get().syncStopped
    port.set({ watching })
    if (!watching) return
    timer = setTimeout(() => { timer = null; void run() }, immediate ? 0 : interval * 2 ** failures)
  }
  function completed(error?: unknown, writing = false) {
    if (!writing) {
      failures = error ? failures + 1 : 0
      port.set({ syncStopped: !!error && (fatalRead(error) || failures >= 3) })
    }
    schedule()
  }
  return { completed, schedule, clear,
    setActive(value: boolean) { active = value; schedule(value) },
    reset() { failures = 0; port.set({ syncStopped: false }); schedule() },
    dispose() { closed = true; active = false; clear(); port.set({ watching: false }) } }
}
