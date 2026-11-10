import { expect, test } from 'bun:test'
import { harness, target, olderTurn, fileAccess } from './file-comparison-fixture'
import { filePairs, comparisonJson } from '../src/lib/file-comparison'
import { fileSet, visibleFileText } from '../src/lib/file-snapshots'
import { json, turn, pause } from './task-fixture'

test('explicit baseline preserves both verified identities, raw controls and line endings in export', async () => {
  const data = await harness('\ufeffa\r\n\u202e\n', 'a\n\\u{202e}')
  await data.ready()
  expect(data.state.saves).toHaveLength(0)
  expect(data.state.requests.filter((path) => /file-snapshots\//.test(path))).toHaveLength(0)
  await data.reader.compare('report.txt')
  const compared = data.state.view.comparison!
  expect(compared.byte_equal).toBe(false)
  expect(compared.before.turn_id).toBe(olderTurn.turn_id)
  expect(compared.after.turn_id).toBe(target.turn)
  expect(compared.before.hub_received_at).toBe(data.left.list.data.created_at)
  expect(compared.before).not.toHaveProperty('captured_at')
  expect(compared.result.state).toBe('changed')
  if (compared.result.state !== 'changed') throw new Error('expected changed')
  expect(compared.result.before!.bom).toBe(true)
  expect(compared.result.after!.finalNewline).toBe(false)
  expect(compared.result.rows.some((row) => row.text === '\u202e')).toBe(true)
  expect(visibleFileText('\u202e')).not.toBe(visibleFileText('\\u{202e}'))
  await data.reader.compare('report.txt', true)
  expect(data.state.saves).toEqual([compared])
  expect(data.state.requests.filter((path) => /file-snapshots\//.test(path))).toHaveLength(4)
  data.reader.clear()
})

test('maximum-row and escaped-text exports stay bounded; excessive serialized records are refused', async () => {
  for (const text of ['\n'.repeat(2000), '\t'.repeat(65536)]) {
    const data = await harness('', text); await data.ready(); await data.reader.compare('report.txt')
    const value = comparisonJson(data.state.view.comparison!)
    expect(new TextEncoder().encode(value).length).toBeLessThanOrEqual(2 * 1024 * 1024)
    expect(JSON.parse(value).result.state).toBe('changed')
    expect(() => comparisonJson({ ...data.state.view.comparison!, path: 'x'.repeat(2 * 1024 * 1024) })).toThrow()
  }
})

test('zero bytes, unavailable, absent and case-different paths never imply creation or deletion', async () => {
  const data = await harness('', '')
  const before = fileSet(data.left.list, { ...target, turn: olderTurn.turn_id }), after = fileSet(data.right.list, target)
  const pairs = filePairs(before, { ...after, files: [], unavailable: [{ path: 'report.txt', code: 'missing' }] })
  expect(pairs[0].before!.size_bytes).toBe(0); expect(pairs[0].after).toBeNull(); expect(pairs[0].afterReason).toBe('文件不存在')
  expect(filePairs(before, { ...after, files: [{ ...after.files[0], path: 'Report.txt' }] })).toHaveLength(2)
  expect(filePairs(before, { ...after, files: [], state: 'absent' })[0].afterReason).toBe('尚无文件快照')
  await data.ready(); await data.reader.compare('report.txt')
  expect(data.state.view.comparison?.result.state).toBe('equal')
  expect(data.state.view.comparison?.byte_equal).toBe(true)
})

test('binary files verify bytes but do not manufacture a text diff', async () => {
  const data = await harness('\0x', '\0y')
  for (const side of [data.left, data.right]) { side.list.data.files[0].content_kind = 'binary'; side.content.data.content_kind = 'binary' }
  await data.ready(); await data.reader.compare('report.txt')
  expect(data.state.view.comparison?.result).toEqual({ state: 'binary', added: null, removed: null })
  expect(data.state.view.comparison?.byte_equal).toBe(false)
})

test('every export rereads both instance-authorized opaque endpoints; one denial clears everything', async () => {
  let deny = false
  const data = await harness('before', 'after', (path, init) => {
    expect(init?.method).toBeUndefined(); expect(init?.body).toBeUndefined()
    expect(init?.credentials).toBe('same-origin'); expect(init?.cache).toBe('no-store'); expect(init?.redirect).toBe('error')
    if (deny && path.includes(`/turns/${olderTurn.turn_id}/file-snapshots/`)) return json({}, 403)
  })
  await data.ready(); await data.reader.compare('report.txt'); expect(data.state.view.comparison).not.toBeNull()
  deny = true; await data.reader.compare('report.txt', true)
  expect(data.state.saves).toHaveLength(0); expect(data.state.view.comparison).toBeNull()
  expect(data.state.view.history).toBeNull(); expect(data.state.view.problem).toContain('权限')
})

test('one failed side aborts its still-pending sibling before releasing the read deadline', async () => {
  let sibling: AbortSignal | undefined
  const data = await harness('a', 'b', (path, init) => {
    if (!/file-snapshots\//.test(path)) return
    if (path.includes(`/turns/${olderTurn.turn_id}/`)) return json({}, 403)
    sibling = init?.signal as AbortSignal
    return new Promise<Response>((_resolve, reject) => sibling!.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), { once: true }))
  })
  await data.ready(); await data.reader.compare('report.txt', true)
  expect(sibling?.aborted).toBe(true)
  expect(data.state.view.comparison).toBeNull(); expect(data.state.saves).toHaveLength(0)
})

test('digest tampering on either side and cross-tab Cookie context changes reject publication', async () => {
  for (const side of ['left', 'right'] as const) {
    const data = await harness(); await data.ready()
    data[side].content.data.content_b64 = 'Zm9yZ2Vk'
    await data.reader.compare('report.txt', true)
    expect(data.state.saves).toHaveLength(0); expect(data.state.view.problem).toContain('校验失败')
    expect(data.state.view.comparison).toBeNull()
  }
  let context = target.context
  const data = await harness('a', 'b', (path) => {
    if (path === '/web/api/access') return json({ data: { ...fileAccess, context } })
    if (/file-snapshots\//.test(path)) context = 'c'.repeat(64)
  })
  await data.ready(); await data.reader.compare('report.txt', true)
  expect(data.state.view.problem).toContain('登录'); expect(data.state.saves).toHaveLength(0)
})

test('switching the task, clearing the baseline, and pagehide reject delayed content and downloads', async () => {
  for (const change of ['task', 'baseline', 'pagehide']) {
    let complete!: (response: Response) => void, pendingSignal: AbortSignal | undefined
    const data = await harness('a', 'b', (path, init) => {
      if (path.includes(`/turns/${olderTurn.turn_id}/file-snapshots/`)) {
        pendingSignal = init?.signal as AbortSignal
        return new Promise((resolve) => { complete = resolve })
      }
    })
    await data.ready(); const pending = data.reader.compare('report.txt', true); await pause()
    if (change === 'task') { data.state.current = { ...target, key: 'other' }; data.reader.bind() }
    else if (change === 'baseline') await data.reader.choose('')
    else data.reader.clear()
    expect(pendingSignal!.aborted).toBe(true)
    complete(json(data.left.content)); await pending
    expect(data.state.view.comparison).toBeNull(); expect(data.state.saves).toHaveLength(0)
  }
})

test('history pagination rejects cycle, overlap, wrong instance, unordered equal timestamps and excessive trail', async () => {
  for (const kind of ['cycle', 'overlap', 'instance', 'order', 'valid']) {
    let calls = 0
    const data = await harness('a', 'b', (path) => {
      if (!path.includes('/turns?')) return
      calls++
      const item = { ...olderTurn, turn_id: `history-${calls}`, created_at: turn.created_at - calls }
      if (kind === 'overlap' && calls === 2) item.turn_id = 'history-1'
      if (kind === 'instance') item.instance_id = 'other'
      return json({ data: kind === 'order' ? [{ ...item, turn_id: 'a' }, { ...item, turn_id: 'z' }] : [item],
        next_cursor: kind === 'cycle' && calls === 3 ? 'cursor-1' : `cursor-${calls}` })
    })
    await data.reader.history()
    for (let i = 0; i < 34 && !data.state.view.problem; i++) await data.reader.history('next')
    if (kind === 'valid') { expect(calls).toBe(32); expect(data.state.view.page).toBe(31) }
    else { expect(data.state.view.problem).toContain('校验失败'); expect(data.state.view.history).toBeNull() }
    data.reader.clear()
  }
})

test('previous history page and new baseline clear the prior comparison without altering task target', async () => {
  let page = 0
  const data = await harness('a', 'b', (path) => {
    if (!path.includes('/turns?')) return
    page = new URL(path, 'https://abr.test').searchParams.get('before') ? 1 : 0
    return json({ data: [{ ...olderTurn, turn_id: page ? 'oldest' : olderTurn.turn_id, created_at: olderTurn.created_at - page }], next_cursor: page ? '' : 'next' })
  })
  await data.ready(); await data.reader.compare('report.txt'); expect(data.state.view.comparison).not.toBeNull()
  await data.reader.history('next'); expect(data.state.view.baseline).toBeNull(); expect(data.state.view.comparison).toBeNull()
  await data.reader.history('previous'); expect(data.state.view.page).toBe(0); expect(data.state.view.history?.[0].turn_id).toBe(olderTurn.turn_id)
  expect(data.state.current).toEqual(target)
})

test('baseline searches bind every page to submitted filters and reset the old comparison', async () => {
  const queries: URLSearchParams[] = []
  const data = await harness('a', 'b', (path) => {
    if (!path.includes('/turns?')) return
    const query = new URL(path, 'https://abr.test').searchParams
    queries.push(query)
    const next = !!query.get('before')
    return json({ data: [{ ...olderTurn, state: 'completed', turn_id: next ? 'oldest' : olderTurn.turn_id,
      created_at: olderTurn.created_at - Number(next) }], next_cursor: next ? '' : 'filtered-next' })
  })
  await data.ready(); await data.reader.compare('report.txt')
  expect(data.state.view.comparison).not.toBeNull()
  await data.reader.history('reset', { q: ' literal_%_target ', state: 'completed' })
  expect(data.state.view.baseline).toBeNull(); expect(data.state.view.comparison).toBeNull()
  await data.reader.history('next'); await data.reader.history('previous')
  expect(queries.slice(1).map((query) => [query.get('q'), query.get('state'), query.get('before')]))
    .toEqual([['literal_%_target', 'completed', ''], ['literal_%_target', 'completed', 'filtered-next'], ['literal_%_target', 'completed', '']])
  await data.reader.history()
  expect(queries.at(-1)!.has('q')).toBe(false); expect(queries.at(-1)!.has('state')).toBe(false)
  expect(data.state.view.page).toBe(0); expect(data.state.current).toEqual(target)
  const requests = data.state.requests.length
  await data.reader.history('reset', { q: '\u202e', state: 'completed' })
  expect(data.state.requests).toHaveLength(requests); expect(data.state.view.history).toBeNull()
  expect(data.state.view.problem).toContain('无效'); expect(data.state.saves).toHaveLength(0)
})
