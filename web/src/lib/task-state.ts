import { ReadProblem, type Instance, type Session } from './api'
import { TaskHTTPError, taskError, type Action } from './task-api'
import type { Access, Detail, Events, Operations, Receipt, Turns, Turn } from './task-models'
import { CatalogReadProblem, defaultCatalogQuery, type CatalogPage, type CatalogPosition, type CatalogQuery } from './task-catalog'
import { defaultTurnQuery, TurnReadProblem, type TurnQuery, type TurnPosition, type TurnBoundary } from './turn-query'
import { recapReset, type RecapPage } from './task-recap'

export type TrackedRequest = { kind: 'create' | 'close'; id: string; target: string }
export type TurnMode = 'live' | 'fixed'
export type Tasks = { busy: boolean; refreshing: boolean; initialized: boolean; access: Access | null;
  instances: Instance[]; sessions: Session[]; detail: Detail | null; events: Events | null;
  catalog: CatalogPage | null; catalogQuery: CatalogQuery; catalogAfter: string; catalogLower: string;
  catalogTrail: CatalogPosition[]; catalogPage: number;
  turns: Turns | null; currentTurn: Turn | null; liveTurn: Turn | null; turnMode: TurnMode; operations: Operations | null; readAt: number | null;
  selectedSession: string; selectedTurn: string; turnHistoryCursor: string; contextEpoch: number;
  turnQuery: TurnQuery; turnHistoryTrail: TurnPosition[]; turnHistoryLower: TurnBoundary | null; turnHistoryPage: number; turnHistoryReadAt: number | null;
  recap: RecapPage | null; recapCursor: string; recapLower: TurnBoundary | null; recapTrail: TurnPosition[]; recapPage: number;
  eventsTrimmed: boolean; eventsMore: boolean; watching: boolean; syncStopped: boolean;
  problem: string; outcome: 'none' | 'accepted' | 'unknown' | 'rejected'; pending: Action | null;
  receipt: Receipt | null; tracking: TrackedRequest | null }
export type Patch = Partial<Tasks>
export type StatePort = { get: () => Tasks; set: (patch: Patch) => void }
export type Work = (signal: AbortSignal) => Promise<Patch>
export type RequestOptions = { writing?: boolean; retry?: boolean; background?: boolean }
export type Execute = (work: Work, options?: RequestOptions) => Promise<boolean>

export const facts = (): Patch => ({ ...recapReset(), access: null, instances: [], sessions: [], catalog: null, detail: null, events: null,
  turns: null, currentTurn: null, liveTurn: null, operations: null, readAt: null, turnHistoryReadAt: null, eventsTrimmed: false, eventsMore: false })
export const turnHistoryReset = () => ({ turnQuery: defaultTurnQuery(), turnHistoryCursor: '', turnHistoryTrail: [] as TurnPosition[],
  turnHistoryLower: null as TurnBoundary | null, turnHistoryPage: 1, turnHistoryReadAt: null as number | null })
export const selection = (): Patch => ({ ...recapReset(), selectedSession: '', selectedTurn: '', ...turnHistoryReset(),
  detail: null, events: null, turns: null, currentTurn: null, liveTurn: null, turnMode: 'live', eventsTrimmed: false, eventsMore: false })
export const catalogReset = () => ({ catalog: null, catalogQuery: defaultCatalogQuery(), catalogAfter: '',
  catalogLower: '', catalogTrail: [] as CatalogPosition[], catalogPage: 1 })
export const initial = (contextEpoch = 0): Tasks => ({ busy: false, refreshing: false, initialized: false,
  ...recapReset(),
  ...catalogReset(),
  ...turnHistoryReset(),
  access: null, instances: [], sessions: [], detail: null, events: null, turns: null, currentTurn: null, liveTurn: null, turnMode: 'live',
  operations: null, readAt: null, selectedSession: '', selectedTurn: '', turnHistoryCursor: '', contextEpoch,
  eventsTrimmed: false, eventsMore: false, watching: false, syncStopped: false,
  problem: '', outcome: 'none', pending: null, receipt: null, tracking: null })

export function identityError(error: unknown): boolean {
  return (error instanceof TaskHTTPError && [401, 403].includes(error.status)) ||
    (error instanceof ReadProblem && ['login', 'forbidden'].includes(error.kind))
}
export function fatalRead(error: unknown): boolean {
  return identityError(error) || (error instanceof ReadProblem && ['invalid', 'limited'].includes(error.kind)) ||
    (error instanceof TaskHTTPError && error.status >= 400 && error.status < 500)
}
export function failure(state: Tasks, error: unknown, options: RequestOptions): Patch {
  const rejected = error instanceof TaskHTTPError && error.status >= 400 && error.status < 500 && error.status !== 408
  const changed = identityError(error), uncertain = options.writing && (!rejected || options.retry)
  const historyProblem = error instanceof TurnReadProblem ? (error.kind === 'limited'
    ? '轮次查询超出读取预算或服务暂不可用。' : '轮次响应或分页条件未通过校验。') + '搜索条件已重置，请刷新任务现场后缩小查询范围重试。' : ''
  return { ...facts(), problem: historyProblem || taskError(error), initialized: true,
    outcome: options.writing ? uncertain ? 'unknown' : 'rejected' : state.outcome,
    pending: changed || (options.writing && !uncertain) ? null : state.pending,
    receipt: changed ? null : state.receipt, tracking: changed ? null : state.tracking,
    ...(error instanceof CatalogReadProblem ? { ...catalogReset(), catalogQuery: state.catalogQuery } : {}),
    ...(error instanceof TurnReadProblem ? turnHistoryReset() : {}),
    ...(changed ? { ...selection(), ...catalogReset(), contextEpoch: state.contextEpoch + 1 } : {}) }
}
