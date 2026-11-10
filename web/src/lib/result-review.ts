import { projectResults, type CommandRow, type EvidenceRow } from './task-results'
import { MAX_EVENTS, MAX_EVENT_BYTES, type Event } from './task-events'
import type { Tasks } from './task-state'

export type ReviewCategory = 'command' | 'validation' | 'gate' | 'artifact' | 'diagnostic' | 'turn'
export type ReviewStatus = 'failure' | 'unknown' | 'running' | 'success' | 'recorded'
export type ReviewFilter = 'attention' | 'all' | ReviewStatus
export type ReviewRecord = { key: string; category: ReviewCategory; status: ReviewStatus;
  attention: boolean; partial: boolean; title: string; source: Event | CommandRow | null; sources: readonly Event[] }
export type ResultReview = { records: ReviewRecord[]; counts: Record<ReviewFilter, number>; partial: boolean }
const encoder = new TextEncoder()
const categories = { command: '命令', validation: '验证', gate: '门禁', artifact: '产物元数据', diagnostic: '执行异常', turn: '轮次状态' }
const statuses = { failure: '报告异常', unknown: '结果未知', running: '执行中', success: '报告成功', recorded: '已记录' }
const filters: ReviewFilter[] = ['attention', 'all', 'failure', 'unknown', 'running', 'success', 'recorded']
export const REVIEW_PAGE_SIZE = 20

export const reviewStatusLabel = (status: ReviewStatus): string => statuses[status]
export const reviewCategoryLabel = (category: ReviewCategory): string => categories[category]

function commandStatus(source: Event): ReviewStatus {
  if (source.kind === 'command.started') return source.state && source.state !== 'running' ? 'unknown' : 'running'
  if (source.kind !== 'command.completed' || source.exit_code == null) return 'unknown'
  if (source.state && !['passed', 'failed', 'completed'].includes(source.state)) return 'unknown'
  if ((source.exit_code === 0 && source.state === 'failed') || (source.exit_code !== 0 && source.state === 'passed')) return 'unknown'
  return source.exit_code === 0 ? 'success' : 'failure'
}

function reportedStatus(category: ReviewCategory, source: Event): ReviewStatus {
  if (category === 'command') return commandStatus(source)
  if (category === 'diagnostic') return source.kind === 'turn.failed' ? 'failure' : 'unknown'
  if (category === 'artifact') return source.state === 'recorded' ? 'recorded' : 'unknown'
  if (source.kind === 'validation.started') return source.state === 'running' ? 'running' : 'unknown'
  if (category === 'validation' && ((source.state === 'passed' && source.exit_code != null && source.exit_code !== 0) ||
      (source.state === 'failed' && source.exit_code === 0))) return 'unknown'
  if (category === 'validation' && !['passed', 'failed'].includes(source.state ?? '')) return 'unknown'
  if (['failed', 'rejected'].includes(source.state ?? '')) return 'failure'
  if (source.state === 'passed') return 'success'
  return 'unknown'
}

function reviewRecord(category: ReviewCategory, source: Event & { key?: string }, contributors: ReadonlyMap<string, Event[]>): ReviewRecord {
  const partial = !!(source.truncated || source.evidence_partial || source.output_truncated)
  let status = reportedStatus(category, source)
  if (partial && ['success', 'recorded'].includes(status)) status = 'unknown'
  const key = `${category}:${source.key ?? `event:${source.event_id}`}`
  return { key, category, status, partial, sources: contributors.get(key) ?? [],
    attention: partial || ['failure', 'unknown'].includes(status), title: source.text.slice(0, 160) || categories[category], source }
}

function sourceGroups(events: readonly Event[]): Map<string, Event[]> {
  const groups = new Map<string, Event[]>()
  const category: Record<string, ReviewCategory> = { command: 'command', validation: 'validation', gate: 'gate',
    artifact: 'artifact', turn: 'diagnostic' }
  const kinds = ['command.started', 'command.completed', 'command.output', 'validation.started', 'validation.completed',
    'gate.verdict', 'artifact.created', 'turn.failed', 'turn.interrupted', 'turn.lost']
  for (const event of events) {
    if (!kinds.includes(event.kind)) continue
    const group = category[event.kind.split('.')[0]!]!
    const identity = event.item_id && group !== 'diagnostic' ? `item:${event.item_id}` : `event:${event.event_id}`
    const key = `${group}:${identity}`, rows = groups.get(key) ?? []
    rows.push(event); groups.set(key, rows)
  }
  return groups
}

function boundedWindow(snapshot: Tasks): boolean {
  const events = snapshot.events?.data
  if (!events || events.length > MAX_EVENTS) return false
  let bytes = 0
  for (const event of events) {
    bytes += encoder.encode(JSON.stringify(event)).length
    if (bytes > MAX_EVENT_BYTES) return false
  }
  return true
}

/** Counts describe only the existing loaded evidence window; none is an acceptance verdict. */
export function reviewRecords(snapshot: Tasks): ResultReview | null {
  if (!boundedWindow(snapshot)) return null
  const view = projectResults(snapshot)
  if (!view) return null
  const contributors = sourceGroups(view.events.data)
  const groups: [ReviewCategory, (Event | EvidenceRow | CommandRow)[]][] = [
    ['command', view.commands], ['validation', view.validations], ['gate', view.gates],
    ['artifact', view.artifacts], ['diagnostic', view.diagnostics],
  ]
  const records = groups.flatMap(([category, rows]) => rows.map((source) => reviewRecord(category, source, contributors)))
  if (['failed', 'interrupted', 'lost'].includes(view.turn.state) && !view.diagnostics.length) {
    const label = ({ failed: '失败', interrupted: '中断', lost: '失联' } as Record<string, string>)[view.turn.state]
    records.push({ key: `turn:${view.turn.turn_id}`, category: 'turn', status: view.turn.state === 'failed' ? 'failure' : 'unknown',
      attention: true, partial: true, title: `轮次报告${label}，当前窗口未提供终态诊断`, source: null, sources: [] })
  }
  const counts = { all: records.length, attention: 0, failure: 0, unknown: 0, running: 0, success: 0, recorded: 0 }
  for (const row of records) { counts[row.status]++; if (row.attention) counts.attention++ }
  return { records, counts, partial: view.partial || records.some((row) => row.partial) }
}

function searchText(row: ReviewRecord): string {
  const source = row.source
  return [row.title, row.key, row.category, reviewCategoryLabel(row.category), reviewStatusLabel(row.status),
    source?.text, source?.reason, source?.output_text, source?.event_id, source?.item_id, source?.kind,
    ...(source?.paths ?? []), ...row.sources.map((event) => event.event_id)].join('\n').toLowerCase()
}

/** Filtering never fetches more events or mutates the task's source evidence. */
export function reviewPage(records: readonly ReviewRecord[], q = '', filter: ReviewFilter = 'attention', page = 1) {
  if (typeof q !== 'string' || q.length > 128 || encoder.encode(q).length > 512 ||
      /[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/.test(q) || !filters.includes(filter) ||
      !Number.isSafeInteger(page) || page < 1 || records.length > MAX_EVENTS + 1) throw Error('结果筛选条件无效，请核对搜索文字和页码。')
  const needle = q.trim().toLowerCase()
  const matches = records.filter((row) => (filter === 'all' || (filter === 'attention' ? row.attention : row.status === filter)) &&
    (!needle || searchText(row).includes(needle)))
  const pages = Math.max(1, Math.ceil(matches.length / REVIEW_PAGE_SIZE)), current = Math.min(page, pages)
  return { rows: matches.slice((current - 1) * REVIEW_PAGE_SIZE, current * REVIEW_PAGE_SIZE), page: current, pages, total: matches.length }
}
