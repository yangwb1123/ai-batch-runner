import { ReadProblem } from './api'
import { boolean, boundedText } from './task-values'

/** Optional, normalized execution evidence. No field grants filesystem access. */
export type EvidenceFields = { reason?: string; evidence_partial?: boolean; sha256?: string;
  size_bytes?: number; media_type?: string }
export const EVIDENCE_KINDS = ['validation.started', 'validation.completed', 'gate.verdict', 'artifact.created']
const STATES: Record<string, string[]> = { 'validation.started': ['running'],
  'validation.completed': ['passed', 'failed', 'cancelled', 'unknown'],
  'gate.verdict': ['passed', 'failed', 'rejected', 'unknown'], 'artifact.created': ['recorded', 'unknown'] }

export function evidenceFields(payload: Record<string, unknown>, kind: string): EvidenceFields & { state: string; truncated: boolean } {
  let state = typeof payload.state === 'string' && STATES[kind]?.includes(payload.state) ? payload.state : 'unknown'
  if (kind === 'validation.completed' && ((state === 'passed' && payload.exit_code != null && payload.exit_code !== 0) ||
      (state === 'failed' && payload.exit_code === 0))) state = 'unknown'
  const fields: EvidenceFields & { state: string; truncated: boolean } = { state,
    truncated: payload.truncated === undefined ? false : boolean(payload.truncated) }
  if (payload.reason !== undefined) fields.reason = boundedText(payload.reason, 1024)
  if (payload.evidence_partial !== undefined) fields.evidence_partial = boolean(payload.evidence_partial)
  if (payload.sha256 !== undefined) {
    if (typeof payload.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(payload.sha256)) throw new ReadProblem('invalid')
    fields.sha256 = payload.sha256
  }
  if (payload.size_bytes !== undefined) {
    if (!Number.isSafeInteger(payload.size_bytes) || Number(payload.size_bytes) < 0) throw new ReadProblem('invalid')
    fields.size_bytes = Number(payload.size_bytes)
  }
  if (payload.media_type !== undefined) fields.media_type = boundedText(payload.media_type, 128)
  if (state === 'unknown') fields.evidence_partial = true
  return fields
}
