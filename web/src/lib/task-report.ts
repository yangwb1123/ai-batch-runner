import type { TurnRecord } from './task-export'
import { reportEvents, reportField } from './task-report-records'

function identity(record: TurnRecord): string {
  return [reportField('导出时间（UTC）', record.exported_at), reportField('观察时间（Unix 毫秒）', record.coverage.observed_at),
    reportField('会话 ID', record.session.session_id), reportField('实例 ID', record.session.instance_id),
    reportField('项目', record.session.project_id), reportField('任务名称', record.session.name),
    reportField('轮次 ID', record.turn.turn_id), reportField('执行状态（Hub 记录）', record.turn.state),
    reportField('轮次创建时间（Unix 秒）', record.turn.created_at), reportField('轮次更新时间（Unix 秒）', record.turn.updated_at)].join('')
}

function coverage(record: TurnRecord): string {
  const value = record.coverage
  return ['\n读取范围与完整性\n范围：仅当前已读取事件；下载不会额外读取、自动翻页或执行任务。\n',
    reportField('已读取事件数', record.events.length), reportField('首个保留游标', value.first_cursor), reportField('下一游标', value.next_cursor),
    `较早事件已移出保留窗口：${value.earlier_events_dropped ? '是' : '否'}\n`,
    `尚有未读取事件：${value.more_events_available ? '是' : '否'}\n`,
    `指令截断：${value.prompt_truncated ? '是' : '否'}\n`,
    `回复投影存在缺片、截断或容量限制：${value.replies_partial ? '是' : '否'}\n`,
    `终态轮次缺少回复完成记录：${value.reply_completion_missing ? '是；完整性未知，不推断必然丢失正文。' : '未发现'}\n`,
    `终态轮次缺少活动结束记录：${value.activity_completion_missing ? '是' : '未发现'}\n`,
    `事件报告正文或证据不完整：${value.event_payload_partial ? '是' : '否'}\n`,
    `命令输出报告截断：${value.command_output_partial ? '是' : '否'}\n`,
    `命令输出界面显示受限：${value.command_output_display_limited ? '是；本报告仍保留原始已读输出。' : '否'}\n`,
    '以上均只描述本次读取窗口；没有相应标记不代表任务已完整验收。\n'].join('')
}

function replies(record: TurnRecord): string {
  const rows = record.replies.map((reply, index) => `\n回复 ${index + 1}\n` + reportField('回复标识', reply.id) +
    `收到回复结束片段或完成标记：${reply.complete ? '是（仍须核对缺片与截断）' : '否；完整性未知'}\n` +
    `本回复包含缺片或截断：${reply.partial ? '是' : '未标记'}\n` + reportField('回复正文', reply.text))
  return '\nAgent 回复（有界投影）\n最多 64 条 / 256 KiB；完整的当前已读原始片段另列于“原始回复事件”。\n' +
    (rows.length ? rows.join('') : '当前已读取片段没有可显示的回复，不能据此判断本轮没有回复。\n')
}

/** Plain text only: source material cannot become report-authored headings. */
export function renderTurnReport(record: TurnRecord): string {
  return ['abr 本轮可读报告\n', '目标验收：未独立验证\n文件内容：本报告未读取或验证\n',
    '来源：执行端报告事件与 Hub 轮次记录。Agent 回复、命令退出码 0 均不代替验证或门禁。\n',
    '以“  | ”开头的行是来源内容；控制字符转义。来源内容不代表本报告的裁决。\n\n',
    identity(record), coverage(record), '\n你的指令\n', reportField('指令正文', record.turn.prompt), replies(record),
    '\n已读取事件记录\n各类内保持游标顺序；相同条目标识用于关联，输出快照不自动拼接或宣告完成。\n',
    reportEvents(record.events)].join('')
}
