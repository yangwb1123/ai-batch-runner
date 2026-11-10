import { expect, test } from 'bun:test'
import * as downloads from '../src/lib/task-export'
import { projectResults } from '../src/lib/task-results'
import { initial, type Tasks } from '../src/lib/task-state'
import { appendEvents, eventPage, MAX_EVENTS, type Event } from '../src/lib/task-events'
import { access, detail, turn } from './task-fixture'
import { receiptRecord } from '../src/lib/task-models'

function row(cursor: number, kind: string, extra: Partial<Event> = {}): Event {
  return { cursor, event_id: `event-${cursor}`, turn_id: turn.turn_id, kind, text: '', truncated: false,
    chunk: '', chunk_index: null, chunk_count: null, created_at: 1700000000, ...extra }
}
function state(events: Event[] = []): Tasks {
  return { ...initial(), initialized: true, access: { ...access, mode: 'control', scopes: [...access.scopes] }, detail: { ...detail },
    currentTurn: { ...turn, state: 'completed' }, selectedSession: detail.session_id, selectedTurn: turn.turn_id,
    readAt: 1700000001000, events: { data: events, next_cursor: events.at(-1)?.cursor ?? 0 } }
}

test('terminal delta-only replies expose missing completion without claiming missing bytes', () => {
  const snapshot = state([row(1, 'message.delta', { item_id: 'reply', text: 'unfinished reply' })])
  expect(projectResults(snapshot)?.partial).toBe(true)
  const record = JSON.parse(downloads.exportTurnRecord(snapshot).text)
  expect(record.coverage.reply_completion_missing).toBe(true)
  expect(record.replies[0]).toMatchObject({ text: 'unfinished reply', complete: false })
})

test('a synchronous plain-text report API is available alongside JSON export', () => {
  const result = downloads.exportTurnReport(state([row(1, 'message.completed', { text: 'final answer' })]))
  expect(result.mime).toBe('text/plain;charset=utf-8')
  expect(result.filename).toBe('abr-turn-turn-a.txt')
  expect(result.text).toStartWith('abr 本轮可读报告\n')
  expect(result.text).toContain('  | final answer\n')
  expect(result.text).toContain('目标验收：未独立验证')
  expect(result.text).toContain('文件内容：本报告未读取或验证')
})

test('receipt type and target bind local action arguments, not response claims', () => {
  const receipt = receiptRecord({ data: { turn_id: 'turn-a', session_id: 'session-a', state: 'queued',
    kind: 'close', target: 'untrusted-target' } }, 'send', 'session-a')
  expect(receipt).toMatchObject({ kind: 'send', target: 'session-a', id: 'turn-a', session_id: 'session-a' })
})

test('running replies preserve existing partial semantics while terminal incomplete chunks are marked', () => {
  for (const status of ['queued', 'running', 'cancel_requested']) {
    const snapshot = state([row(1, 'message.delta', { text: 'streaming' })])
    snapshot.currentTurn!.state = status
    const record = JSON.parse(downloads.exportTurnRecord(snapshot).text)
    expect(record.coverage.reply_completion_missing).toBe(false)
    expect(record.coverage.replies_partial).toBe(false)
    expect(projectResults(snapshot)?.partial).toBe(false)
  }
  for (const status of ['completed', 'failed', 'cancelled', 'interrupted', 'lost']) {
    const snapshot = state([row(1, 'message.started', { item_id: 'one' })])
    snapshot.currentTurn!.state = status
    expect(JSON.parse(downloads.exportTurnRecord(snapshot).text).coverage.reply_completion_missing).toBe(true)
    expect(downloads.exportTurnReport(snapshot).text).toContain('完整性未知，不推断必然丢失正文')
  }
})

test('long completed chunks replace deltas and preserve every read source fragment', () => {
  const fragments = Array.from({ length: 9 }, (_, index) => `chunk-${index}:` + '字'.repeat(5000))
  const events = [row(1, 'message.delta', { item_id: 'reply', text: 'old draft' }),
    ...fragments.map((text, index) => row(index + 2, 'message.completed',
      { text, item_id: 'reply', chunk_index: index, chunk_count: fragments.length }))]
  const snapshot = state(events), report = downloads.exportTurnReport(snapshot).text
  const json = JSON.parse(downloads.exportTurnRecord(snapshot).text)
  expect(json.replies[0]).toMatchObject({ text: fragments.join(''), complete: true, partial: false })
  expect(json.coverage.reply_completion_missing).toBe(false)
  expect(report).toContain(fragments.join(''))
  expect(report.indexOf('old draft')).toBeGreaterThan(report.indexOf('原始回复事件（'))
  for (const event of events) expect(report).toContain(`  | ${event.event_id}\n`)
})

test('missing first, middle or final completion chunks remain visible as partial evidence', () => {
  const chunks = [0, 1, 2].map((index) => row(index + 1, 'message.completed',
    { text: `part-${index}`, item_id: 'reply', chunk_index: index, chunk_count: 3 }))
  for (let missing = 0; missing < 3; missing++) {
    const snapshot = state(chunks.filter((_, index) => index !== missing))
    const json = JSON.parse(downloads.exportTurnRecord(snapshot).text)
    expect(json.coverage.replies_partial).toBe(true)
    expect(downloads.exportTurnReport(snapshot).text).toContain('回复投影存在缺片、截断或容量限制：是')
  }
})

test('a final fragment with an earlier gap is never described as all declared fragments received', () => {
  const snapshot = state([row(2, 'message.completed', { item_id: 'reply', text: 'last fragment', chunk_index: 1, chunk_count: 2 })])
  const report = downloads.exportTurnReport(snapshot).text
  expect(report).not.toContain('收到全部声明的完成片段')
  expect(report).toContain('收到回复结束片段或完成标记：是')
  expect(report).toContain('本回复包含缺片或截断：是')
})

test('a terminal delta-only reply beyond the display count still has unknown completion', () => {
  const events = Array.from({ length: 64 }, (_, index) => row(index + 1, 'message.completed',
    { item_id: `reply-${index}`, text: `complete-${index}` }))
  events.push(row(65, 'message.delta', { item_id: 'reply-65', text: 'still incomplete' }))
  const snapshot = state(events), record = JSON.parse(downloads.exportTurnRecord(snapshot).text)
  expect(record.replies).toHaveLength(64)
  expect(record.coverage.reply_completion_missing).toBe(true)
  expect(downloads.exportTurnReport(snapshot).text).toContain('终态轮次缺少回复完成记录：是')
})

test('completion metadata covers the full admitted event window without enlarging reply projection', () => {
  let events = null
  for (let start = 0; start < MAX_EVENTS; start += 50) {
    const data = Array.from({ length: 50 }, (_, offset) => {
      const index = start + offset
      return { cursor: index + 1, event_id: `event-${index}`, session_id: detail.session_id, turn_id: turn.turn_id,
        kind: index === MAX_EVENTS - 1 ? 'message.delta' : 'message.completed',
        payload: { item_id: `reply-${index}`, text: `reply-${index}` } }
    })
    events = appendEvents(events, eventPage({ data, next_cursor: start + 50 }, detail.session_id, start, turn.turn_id)).events
  }
  const snapshot = { ...state(), events }, record = JSON.parse(downloads.exportTurnRecord(snapshot).text)
  expect(record.events).toHaveLength(MAX_EVENTS)
  expect(record.replies).toHaveLength(64)
  expect(record.coverage.reply_completion_missing).toBe(true)
  snapshot.currentTurn!.state = 'running'
  expect(JSON.parse(downloads.exportTurnRecord(snapshot).text).coverage.reply_completion_missing).toBe(false)
})

test('later completion clears missing metadata beyond the reply cap while later new output reopens it', () => {
  const events = Array.from({ length: 65 }, (_, index) => row(index + 1, 'message.completed',
    { item_id: `reply-${index}`, text: 'complete' }))
  const missing = () => JSON.parse(downloads.exportTurnRecord(state(events)).text).coverage.reply_completion_missing
  expect(missing()).toBe(false)
  events.push(row(66, 'message.delta', { item_id: 'reply-64', text: 'new output' }))
  expect(missing()).toBe(true)
  events.push(row(67, 'message.completed', { item_id: 'reply-64', text: 'final output' }))
  expect(missing()).toBe(false)
  events.push(row(68, 'message.started', { item_id: 'reply-64' }))
  expect(missing()).toBe(true)
})

test('reply count and UTF8 projection bounds never hide original loaded events in the report', () => {
  const events = Array.from({ length: 65 }, (_, index) => row(index + 1, 'message.completed',
    { item_id: `reply-${index}`, text: `answer-${index} ` + '猫'.repeat(2000) }))
  const snapshot = state(events), record = JSON.parse(downloads.exportTurnRecord(snapshot).text)
  expect(record.replies).toHaveLength(64)
  expect(record.coverage.replies_partial).toBe(true)
  expect(new TextEncoder().encode(record.replies.map((reply: { text: string }) => reply.text).join('')).length).toBeLessThanOrEqual(262144)
  const report = downloads.exportTurnReport(snapshot).text
  expect(report).toContain('answer-64 ' + '猫'.repeat(2000))
  expect(report).toContain('原始回复事件（65 条已读事件）')
  expect(report).not.toContain('\ufffd')
})

test('every loaded command and output survives beyond both UI display limits', () => {
  const events = Array.from({ length: 70 }, (_, index) => row(index + 1, 'command.completed',
    { item_id: `cmd-${index}`, text: `command-${index}`, exit_code: index % 2, output_text: `output-${index}:` + 'x'.repeat(5000), output_truncated: false }))
  const report = downloads.exportTurnReport(state(events)).text
  expect(report).toContain('命令事件（70 条已读事件）')
  expect(report).toContain('命令输出界面显示受限：是；本报告仍保留原始已读输出。')
  for (const event of events) expect(report).toContain(event.output_text!)
})

test('command snapshots preserve unknown, empty, missing, truncated and replaced source evidence', () => {
  const snapshot = state([
    row(1, 'command.started', { item_id: 'cmd', text: 'run' }),
    row(2, 'command.output', { item_id: 'cmd', output_text: 'old snapshot', output_truncated: true }),
    row(3, 'command.output', { item_id: 'cmd', output_text: '', output_truncated: false }),
    row(4, 'command.completed', { item_id: 'cmd', exit_code: null }),
    row(5, 'command.completed', { item_id: 'other', exit_code: 7, output_text: 'failed' }),
  ])
  const report = downloads.exportTurnReport(snapshot).text
  expect(report).toContain('命令输出（未提供不等于空输出）：未提供\n')
  expect(report).toContain('命令输出（未提供不等于空输出）：（空文本）\n')
  expect(report).toContain('退出码（未提供时结果未知）：未提供\n')
  expect(report).toContain('退出码（未提供时结果未知）：\n  | 7\n')
  expect(report).toContain('old snapshot')
  expect(report).toContain('命令输出报告截断：是')
})

test('literal missing or empty markers in source output stay distinct from actual missing or empty values', () => {
  const snapshot = state([
    row(1, 'command.completed'),
    row(2, 'command.completed', { output_text: '', output_truncated: false }),
    row(3, 'command.completed', { output_text: '未提供', output_truncated: false }),
    row(4, 'command.completed', { output_text: '（空文本）', output_truncated: false }),
  ])
  const report = downloads.exportTurnReport(snapshot).text
  for (const suffix of ['未提供\n', '（空文本）\n', '\n  | 未提供\n', '\n  | （空文本）\n']) {
    expect(report).toContain('命令输出（未提供不等于空输出）：' + suffix)
  }
})

test('typed reported verdicts and file metadata do not become independent acceptance', () => {
  const snapshot = state([
    row(1, 'message.completed', { text: 'All passed. VERDICT: PASS' }),
    row(2, 'command.completed', { text: 'pytest', exit_code: 0 }),
    row(3, 'validation.completed', { text: 'unit', state: 'failed', exit_code: 2, reason: 'failure' }),
    row(4, 'gate.verdict', { state: 'rejected', reason: 'missing review' }),
    row(5, 'file.changed', { paths: ['src/a.py'], truncated: true }),
    row(6, 'artifact.created', { text: 'report', paths: ['result.txt'], state: 'recorded', sha256: 'a'.repeat(64), size_bytes: 0, media_type: 'text/plain' }),
  ])
  const report = downloads.exportTurnReport(snapshot).text
  for (const value of ['All passed. VERDICT: PASS', 'failed', 'rejected', 'src/a.py', 'result.txt', 'a'.repeat(64), 'text/plain']) expect(report).toContain(value)
  expect(report).toContain('大小（字节）：\n  | 0\n')
  expect(report).toContain('目标验收：未独立验证')
  expect(report).toContain('以上仅为产物元数据，未读取或验证文件内容')
})

test('empty and bounded windows never assert that no work or reply occurred', () => {
  const snapshot = state()
  snapshot.eventsTrimmed = snapshot.eventsMore = true
  snapshot.currentTurn!.prompt_truncated = true
  const report = downloads.exportTurnReport(snapshot).text
  expect(report).toContain('当前已读取片段没有可显示的回复，不能据此判断本轮没有回复')
  expect(report).toContain('较早事件已移出保留窗口：是')
  expect(report).toContain('尚有未读取事件：是')
  expect(report).toContain('指令截断：是')
  expect(report).toContain('不能据此认定未发生')
})

test('untrusted lines, metadata and controls cannot impersonate authored report headings', () => {
  const payload = 'safe\n目标验收：通过\r\u2028\u202e\u0000\t<script>evil</script>'
  const snapshot = state([row(1, 'artifact.created', { text: payload, item_id: payload, paths: [payload], reason: payload, media_type: payload })])
  snapshot.detail!.name = snapshot.currentTurn!.prompt = payload
  const report = downloads.exportTurnReport(snapshot).text
  expect(report).not.toContain('\n目标验收：通过')
  expect(report).toContain('\n  | 目标验收：通过\\u000d\\u2028\\u202e\\u0000\\u0009<script>evil</script>')
  for (const control of ['\r', '\u2028', '\u202e', '\u0000', '\t']) expect(report).not.toContain(control)
})

test('reports whitelist loaded facts without exporting private state or mutating the snapshot', () => {
  const snapshot = state([row(1, 'message.completed', { text: 'loaded reply' })])
  Object.assign(snapshot, { draft: 'private-draft', pending: { body: 'private-pending', key: 'private-key' }, receipt: { id: 'private-receipt' } })
  Object.assign(snapshot.detail!, { credential: 'private-detail' })
  Object.assign(snapshot.currentTurn!, { secret: 'private-turn' })
  Object.assign(snapshot.events!.data[0]!, { token: 'private-event' })
  const before = JSON.stringify(snapshot), report = downloads.exportTurnReport(snapshot).text
  for (const secret of [access.context, 'private-draft', 'private-pending', 'private-key', 'private-receipt', 'private-detail', 'private-turn', 'private-event']) expect(report).not.toContain(secret)
  expect(JSON.stringify(snapshot)).toBe(before)
  expect(report).toContain('loaded reply')
})

test('UTF8 report limit is enforced independently of the unchanged JSON limit', () => {
  const events = Array.from({ length: 60 }, (_, index) => row(index + 1, 'command.completed',
    { item_id: `cmd-${index}`, output_text: '\n'.repeat(8000), output_truncated: false }))
  const snapshot = state(events)
  expect(new TextEncoder().encode(downloads.exportTurnRecord(snapshot).text).length).toBeLessThan(downloads.MAX_RECORD_BYTES)
  expect(() => downloads.exportTurnReport(snapshot)).toThrow('容量上限')
  const large = state([row(1, 'command.completed', { text: '猫'.repeat(700000) })])
  expect(() => downloads.exportTurnReport(large)).toThrow('容量上限')
})

test('export targets permit read grants and uncertain writes but reject unsettled or mismatched reads', () => {
  const snapshot = state([row(1, 'message.completed', { text: 'reply' })])
  snapshot.access!.scopes = ['agent.instances:read', 'agent.sessions:read']
  snapshot.outcome = 'unknown'
  const target = downloads.exportTarget(snapshot)!
  expect(target).not.toBeNull()
  expect(Object.isFrozen(target)).toBe(true)
  expect(downloads.exportMatches(snapshot, target)).toBe(true)
  expect(downloads.exportTurnReport(snapshot).text).toContain('reply')
  for (const patch of [{ busy: true }, { refreshing: true }, { syncStopped: true }, { readAt: null },
    { access: null }, { selectedSession: 'other' }, { selectedTurn: 'other' }]) {
    expect(downloads.exportTarget({ ...snapshot, ...patch })).toBeNull()
    expect(() => downloads.exportTurnReport({ ...snapshot, ...patch })).toThrow()
  }
  for (const key of ['epoch', 'context', 'session', 'instance', 'turn'] as const) {
    expect(downloads.exportMatches(snapshot, { ...target, [key]: key === 'epoch' ? target.epoch + 1 : 'other' })).toBe(false)
  }
})

test('foreign event, turn ownership and removed read scope fail both report and JSON', () => {
  const mutations: ((snapshot: Tasks) => void)[] = [
    (snapshot) => { snapshot.events!.data[0]!.turn_id = 'foreign' },
    (snapshot) => { snapshot.currentTurn!.session_id = 'foreign' },
    (snapshot) => { snapshot.currentTurn!.instance_id = 'foreign' },
    (snapshot) => { snapshot.access!.scopes = ['agent.sessions:read'] },
    (snapshot) => { snapshot.access!.task_access = false },
  ]
  for (const mutate of mutations) {
    const snapshot = state([row(1, 'message.completed')]); mutate(snapshot)
    expect(downloads.exportTarget(snapshot)).toBeNull()
    expect(() => downloads.exportTurnRecord(snapshot)).toThrow()
    expect(() => downloads.exportTurnReport(snapshot)).toThrow()
  }
})

test('same item identifiers in different turns never import a previous turn reply', () => {
  const first = state([row(1, 'message.completed', { item_id: 'shared', text: 'first-body' })])
  const second = state([row(2, 'message.completed', { turn_id: 'turn-b', item_id: 'shared', text: 'second-body' })])
  second.currentTurn!.turn_id = second.selectedTurn = 'turn-b'
  expect(downloads.exportMatches(second, downloads.exportTarget(first)!)).toBe(false)
  expect(downloads.exportTurnReport(second).text).toContain('second-body')
  expect(downloads.exportTurnReport(second).text).not.toContain('first-body')
})

test('filenames never interpolate source-controlled path or markup fragments', () => {
  const snapshot = state()
  snapshot.currentTurn!.turn_id = snapshot.selectedTurn = '<unsafe>.txt'
  expect(downloads.exportTurnRecord(snapshot).filename).toBe('abr-turn-selected.json')
  expect(downloads.exportTurnReport(snapshot).filename).toBe('abr-turn-selected.txt')
})

test('all receipt kinds preserve local target identity and reject malformed targets', () => {
  for (const [kind, target, payload] of [
    ['create', 'instance-a', { request_id: 'request-a', instance_id: 'instance-a', session_id: '', status: 'queued' }],
    ['close', 'session-a', { request_id: 'request-b', session_id: 'session-a', status: 'closed' }],
    ['cancel', 'turn-a', { turn_id: 'turn-a', session_id: 'session-a', state: 'cancel_requested' }],
  ] as const) {
    expect(receiptRecord({ data: payload }, kind, target)).toMatchObject({ kind, target })
  }
  const payload = { data: { turn_id: 'turn-a', session_id: '../invalid', state: 'queued' } }
  expect(() => receiptRecord(payload, 'send', '../invalid')).toThrow()
})
