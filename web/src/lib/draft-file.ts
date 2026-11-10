import { MAX_DRAFT_BYTES } from './drafts'
import type { Tasks } from './task-state'

export type DraftTarget = Readonly<{ epoch: number; context: string; session: string; instance: string }>
const encoder = new TextEncoder()
const decoder = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true })
const prohibited = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/

/** This is an in-memory target, never file metadata or authority restored from a file. */
export function draftTarget(snapshot: Tasks): DraftTarget | null {
  const { access, detail } = snapshot
  if (!access?.task_access || access.mode !== 'control' || !detail || !snapshot.selectedSession ||
      !['agent.instances:read', 'agent.sessions:read', 'agent.sessions:write'].every((scope) => access.scopes.includes(scope)) ||
      detail.session_id !== snapshot.selectedSession || !detail.instance_id || !access.context ||
      snapshot.syncStopped || snapshot.readAt === null) return null
  return Object.freeze({ epoch: snapshot.contextEpoch, context: access.context,
    session: detail.session_id, instance: detail.instance_id })
}

export function draftMatches(snapshot: Tasks, target: DraftTarget): boolean {
  const current = draftTarget(snapshot)
  return !!current && current.epoch === target.epoch && current.context === target.context &&
    current.session === target.session && current.instance === target.instance
}

export function draftBackupAvailable(snapshot: Tasks): boolean {
  return !!draftTarget(snapshot) && !snapshot.busy && !snapshot.refreshing && snapshot.pending === null &&
    snapshot.outcome !== 'unknown'
}

export function draftAvailable(snapshot: Tasks): boolean {
  return draftBackupAvailable(snapshot) && snapshot.detail!.status !== 'closed'
}

/** Stricter file-entry validation; existing prompt APIs retain their own input contract. */
export function validateDraftText(value: unknown, allowEmpty = false): string {
  if (typeof value !== 'string' || (!allowEmpty && !value.trim()) || prohibited.test(value)) {
    throw Error('草稿文件需包含非空文本，不能包含控制字符或双向控制字符。')
  }
  const bytes = encoder.encode(value)
  if (bytes.length > MAX_DRAFT_BYTES) throw Error('草稿超过 65536 字节，原稿未更改；请精简后重试。')
  if (decoder.decode(bytes) !== value) throw Error('草稿包含无效 Unicode，不能保存或追加。')
  return value
}

export function exportDraft(snapshot: Tasks, prompt: string): { text: string; filename: string } {
  if (!draftBackupAvailable(snapshot)) throw Error('当前不能保存草稿，请先核对访问权限和未决提交。')
  const text = validateDraftText(prompt)
  const id = /^[A-Za-z0-9._-]{1,128}$/.test(snapshot.selectedSession) ? snapshot.selectedSession : 'selected'
  return { text, filename: `abr-draft-${id}.txt` }
}

/** Caller commits this result through the existing draft owner; this function never writes or sends. */
export function appendDraft(snapshot: Tasks, target: DraftTarget, latestExisting: string, imported: string): string {
  if (!draftAvailable(snapshot) || !draftMatches(snapshot, target)) {
    throw Error('任务或草稿上下文已变化，或有未决提交；请重新核对后选择文件。')
  }
  const existing = validateDraftText(latestExisting, true), addition = validateDraftText(imported)
  return validateDraftText(existing ? `${existing}\n\n${addition}` : addition)
}
