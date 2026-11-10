import type { TurnRecord } from './task-export'

type ReportEvent = TurnRecord['events'][number]
const CONTROL = /[\u0000-\u0009\u000b-\u001f\u007f-\u009f\u2028-\u202e\u2066-\u2069]/g

/** Every source line remains visibly separate from report-authored labels. */
export function reportField(label: string, value: string | number | boolean | null | undefined): string {
  if (value == null) return `${label}：未提供\n`
  if (value === '') return `${label}：（空文本）\n`
  const escaped = String(value).replace(CONTROL, (char) => `\\u${char.charCodeAt(0).toString(16).padStart(4, '0')}`)
  return `${label}：\n${escaped.split('\n').map((line) => `  | ${line}`).join('\n')}\n`
}

function paths(event: ReportEvent): string {
  if (event.paths === undefined) return reportField('文件路径', undefined)
  if (!event.paths.length) return '文件路径：未提供路径条目。\n'
  return event.paths.map((path, index) => reportField(`文件路径 ${index + 1}`, path)).join('')
}

function commandFields(event: ReportEvent): string {
  if (!event.kind.startsWith('command.')) return ''
  const status = event.kind === 'command.completed' ? '已收到命令结束记录，退出码不代表目标验收。'
    : event.kind === 'command.started' ? '命令开始事件，不单独证明当前仍在执行。' : '输出快照，不单独证明命令结束。'
  return status + '\n' + reportField('退出码（未提供时结果未知）', event.exit_code) +
    reportField('命令输出（未提供不等于空输出）', event.output_text) +
    reportField('输出截断标记', event.output_truncated)
}

function metadataFields(event: ReportEvent): string {
  if (event.kind !== 'artifact.created' && event.sha256 === undefined && event.size_bytes === undefined && event.media_type === undefined) return ''
  return reportField('SHA-256（执行端报告）', event.sha256) + reportField('大小（字节）', event.size_bytes) +
    reportField('媒体类型', event.media_type) + '以上仅为产物元数据，未读取或验证文件内容。\n'
}

function eventRecord(event: ReportEvent, index: number): string {
  const lines = [`记录 ${index + 1}\n`, reportField('游标', event.cursor), reportField('事件 ID', event.event_id),
    reportField('轮次 ID', event.turn_id), reportField('事件种类', event.kind), reportField('条目标识', event.item_id),
    reportField('记录时间（Unix 秒）', event.created_at), reportField('正文', event.text)]
  if (event.state !== undefined || ['validation.started', 'validation.completed', 'gate.verdict', 'artifact.created'].includes(event.kind)) {
    lines.push(reportField('报告状态（不等于独立验收）', event.state))
  }
  if (event.reason !== undefined) lines.push(reportField('原因', event.reason))
  if (!event.kind.startsWith('command.') && (event.exit_code !== undefined || event.kind.startsWith('validation.'))) {
    lines.push(reportField('退出码（未提供时结果未知）', event.exit_code))
  }
  lines.push(commandFields(event), metadataFields(event))
  if (event.paths !== undefined || ['file.changed', 'artifact.created'].includes(event.kind)) lines.push(paths(event))
  if (event.chunk_index !== null || event.chunk_count !== null) {
    lines.push(reportField('片段索引（从 0 起）', event.chunk_index), reportField('片段总数', event.chunk_count))
  }
  lines.push(reportField('正文截断标记', event.truncated), reportField('证据不完整标记', event.evidence_partial), '\n')
  return lines.join('')
}

const groups = [
  { title: '命令事件', kinds: ['command.started', 'command.completed', 'command.output'] },
  { title: '验证事件', kinds: ['validation.started', 'validation.completed'] },
  { title: '门禁事件', kinds: ['gate.verdict'] },
  { title: '文件变更元数据', kinds: ['file.changed'] },
  { title: '产物元数据', kinds: ['artifact.created'] },
  { title: '执行异常', kinds: ['turn.failed', 'turn.interrupted', 'turn.lost'] },
  { title: '原始回复事件', kinds: ['message.started', 'message.delta', 'message.completed'] },
]

/** Preserve the loaded records, including output beyond the UI display budget. */
export function reportEvents(events: readonly ReportEvent[]): string {
  const known = new Set(groups.flatMap((group) => group.kinds))
  const sections = groups.map((group) => ({ title: group.title, rows: events.filter((event) => group.kinds.includes(event.kind)) }))
  sections.push({ title: '其他已读取事件', rows: events.filter((event) => !known.has(event.kind)) })
  return sections.map(({ title, rows }) => `\n${title}（${rows.length} 条已读事件）\n` +
    (rows.length ? rows.map(eventRecord).join('') : '当前读取窗口未提供此类事件，不能据此认定未发生。\n')).join('')
}
