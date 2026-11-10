/** Tab-memory only: no prompts, credentials or pending writes in browser storage. */
export const MAX_DRAFT_BYTES = 65536

export function createDrafts() {
  const drafts = new Map<string, string>()
  let epoch = -1
  return {
    context(next: number) { if (epoch !== next) { drafts.clear(); epoch = next } },
    get(id: string) { return drafts.get(id) ?? '' },
    set(id: string, text: string) {
      if (new TextEncoder().encode(text).length > MAX_DRAFT_BYTES) throw Error('draft size exceeded')
      if (!id || !text) { drafts.delete(id); return }
      if (!drafts.has(id) && drafts.size >= 16) throw Error('draft capacity reached')
      drafts.set(id, text)
    },
    consume(id: string, expected: string) {
      if (drafts.get(id) !== expected) return false
      drafts.delete(id)
      return true
    },
    hasAny() { return drafts.size > 0 },
    clear() { drafts.clear() },
  }
}
