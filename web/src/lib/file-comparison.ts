import { compareText, type DiffResult } from './file-diff'
import { rawFileText, unavailableLabels, type FileMetadata, type FileSet, type FileTarget } from './file-snapshots'
import type { TurnStatus } from './task-history'
import { ReadProblem } from './api'
import { defaultTurnQuery, type TurnQuery } from './turn-query'

export type FilePair = { path: string; before: FileMetadata | null; after: FileMetadata | null; beforeReason: string; afterReason: string }
export type Comparison = { schema: 'abr.file-comparison.v1'; verification: 'content_integrity_only';
  instance_id: string; session_id: string; path: string; byte_equal: boolean;
  before: FileMetadata & { turn_id: string; snapshot_set_id: string; hub_received_at: number };
  after: FileMetadata & { turn_id: string; snapshot_set_id: string; hub_received_at: number };
  result: DiffResult | { state: 'binary'; added: null; removed: null } }
export type CompareView = { history: TurnStatus[] | null; next: string; page: number; baseline: TurnStatus | null;
  query: TurnQuery;
  sets: { before: FileSet; after: FileSet } | null; pairs: FilePair[]; comparison: Comparison | null;
  busy: boolean; problem: string; feedback: string }
export const emptyComparison = (): CompareView => ({ history: null, next: '', page: 0, baseline: null, query: defaultTurnQuery(),
  sets: null, pairs: [], comparison: null, busy: false, problem: '', feedback: '' })
export const terminalTurn = (turn: TurnStatus): boolean => ['completed', 'failed', 'cancelled', 'interrupted', 'lost'].includes(turn.state)
function missingReason(set: FileSet, path: string): string {
  const missing = set.unavailable.find((file) => file.path === path)
  return missing ? unavailableLabels[missing.code] : set.state === 'absent' ? '尚无文件快照' : '此路径未共享'
}
export function filePairs(before: FileSet, after: FileSet): FilePair[] {
  const paths = new Set([...before.files, ...before.unavailable, ...after.files, ...after.unavailable].map((file) => file.path))
  return [...paths].sort().map((path) => ({ path, before: before.files.find((file) => file.path === path) ?? null,
    after: after.files.find((file) => file.path === path) ?? null, beforeReason: missingReason(before, path), afterReason: missingReason(after, path) }))
}
export function compareFiles(target: FileTarget, baseline: TurnStatus, sets: NonNullable<CompareView['sets']>,
  pair: FilePair, bytes: [Uint8Array, Uint8Array]): Comparison {
  const before = pair.before!, after = pair.after!
  const left = rawFileText(bytes[0], before), right = rawFileText(bytes[1], after)
  return { schema: 'abr.file-comparison.v1', verification: 'content_integrity_only', instance_id: target.instance,
    session_id: target.session, path: pair.path, byte_equal: before.sha256 === after.sha256 && before.size_bytes === after.size_bytes,
    before: { ...before, turn_id: baseline.turn_id, snapshot_set_id: sets.before.snapshot_set_id!, hub_received_at: sets.before.created_at! },
    after: { ...after, turn_id: target.turn, snapshot_set_id: sets.after.snapshot_set_id!, hub_received_at: sets.after.created_at! },
    result: left === null || right === null ? { state: 'binary', added: null, removed: null } : compareText(left, right) }
}
export function comparisonJson(comparison: Comparison): string {
  const text = JSON.stringify(comparison, null, 2) + '\n'
  if (new TextEncoder().encode(text).length > 2 * 1024 * 1024) throw new ReadProblem('limited')
  return text
}
