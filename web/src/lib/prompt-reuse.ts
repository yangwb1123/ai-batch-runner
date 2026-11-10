import { appendDraft, draftAvailable, draftMatches, draftTarget, validateDraftText, type DraftTarget } from './draft-file'
import type { Tasks } from './task-state'

export type PromptReference = Readonly<{ target: DraftTarget; turn: string; prompt: string }>
const encoder = new TextEncoder()
const MAX_PROMPT_BYTES = 16384

/** Only the already authorized, displayed instruction; never recover original submitted secrets. */
function openedInstruction(snapshot: Tasks) {
  const source = snapshot.currentTurn
  if (!source || !snapshot.selectedTurn || source.turn_id !== snapshot.selectedTurn ||
      source.session_id !== snapshot.selectedSession || source.instance_id !== snapshot.detail?.instance_id ||
      source.prompt_truncated !== false) {
    throw Error('当前轮次指令缺失、已截断或目标不一致，请重新打开并核对。')
  }
  if (typeof source.prompt !== 'string' || source.prompt.length > MAX_PROMPT_BYTES ||
      encoder.encode(source.prompt).length > MAX_PROMPT_BYTES) {
    throw Error('当前可见指令超过 16384 字节，不能复用；原草稿未更改。')
  }
  validateDraftText(source.prompt)
  return source
}

export function promptReuseAvailable(snapshot: Tasks): boolean {
  if (!draftAvailable(snapshot)) return false
  try { openedInstruction(snapshot); return true }
  catch { return false }
}

export function preparePromptReuse(snapshot: Tasks): { reference: PromptReference; text: string } {
  if (!draftAvailable(snapshot)) throw Error('当前不能复用指令，请先核对访问权限、会话和未决提交。')
  const source = openedInstruction(snapshot), target = draftTarget(snapshot)!
  const reference = Object.freeze({ target, turn: source.turn_id, prompt: source.prompt })
  return { reference, text: source.prompt }
}

/** Temporary action locks do not expire an otherwise unchanged visible-source reference. */
export function promptReuseMatches(snapshot: Tasks, reference: PromptReference): boolean {
  try {
    if (!draftMatches(snapshot, reference.target)) return false
    const source = openedInstruction(snapshot)
    return source.turn_id === reference.turn && source.prompt === reference.prompt
  } catch { return false }
}

/** The caller alone commits this text through its draft owner; no request, mutation or automatic send. */
export function appendPromptReuse(snapshot: Tasks, reference: PromptReference, latestExisting: string, edited: string): string {
  if (!promptReuseMatches(snapshot, reference)) throw Error('指令来源或任务上下文已变化，请重新打开预览；原草稿未更改。')
  return appendDraft(snapshot, reference.target, latestExisting, edited)
}
