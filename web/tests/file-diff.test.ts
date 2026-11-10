import { expect, test } from 'bun:test'
import { compareText, DIFF_CONTEXT, MAX_DIFF_BYTES, MAX_DIFF_LINES, MAX_DIFF_ROWS,
  MAX_DIFF_TRACE_CELLS, MAX_DIFF_WORK, type DiffResult, type DiffRow } from '../src/lib/file-diff'

const endings = { LF: '\n', CRLF: '\r\n', CR: '\r', none: '' }
const rebuild = (rows: DiffRow[], side: 'before' | 'after') => rows
  .filter((row) => row.kind !== (side === 'before' ? 'add' : 'remove'))
  .map((row) => row.text + endings[row.ending]).join('')

function complete(before: string, after: string) {
  const result = compareText(before, after)
  expect(result.state).toBe(before === after ? 'equal' : 'changed')
  if (result.state !== 'changed') throw new Error('Expected changed text')
  expect(rebuild(result.rows, 'before')).toBe(before)
  expect(rebuild(result.rows, 'after')).toBe(after)
  let beforeLine = 0, afterLine = 0
  for (const row of result.rows) {
    expect(row.beforeLine).toBe(row.kind === 'add' ? null : ++beforeLine)
    expect(row.afterLine).toBe(row.kind === 'remove' ? null : ++afterLine)
  }
  expect(result.before!.lines).toBe(beforeLine)
  expect(result.after!.lines).toBe(afterLine)
  expect(result.added).toBe(result.rows.filter((row) => row.kind === 'add').length)
  expect(result.removed).toBe(result.rows.filter((row) => row.kind === 'remove').length)
  expect(result.rows.length).toBeLessThanOrEqual(MAX_DIFF_ROWS)
  expect(result.work).toBeLessThanOrEqual(MAX_DIFF_WORK)
  return result
}

function limited(result: DiffResult, reason: 'bytes' | 'lines' | 'work' | 'invalid_text') {
  expect(result.state).toBe('limited')
  if (result.state !== 'limited') throw new Error('Expected an explicit limit')
  expect(result.reason).toBe(reason)
  expect(result.rows).toEqual([])
  expect(result.hunks).toEqual([])
  expect(result.added).toBeNull()
  expect(result.removed).toBeNull()
  expect(result.work).toBeLessThanOrEqual(MAX_DIFF_WORK)
}

test('identical text is exact equality with metadata and no alignment work', () => {
  for (const raw of ['', 'hello', '\ufeffhello\r\n', 'a\rb\n', 'repeat\n'.repeat(MAX_DIFF_LINES)]) {
    const result = compareText(raw, raw)
    expect(result.state).toBe('equal')
    expect(result.rows).toEqual([])
    expect(result.hunks).toEqual([])
    expect(result.added).toBe(0)
    expect(result.removed).toBe(0)
    expect(result.work).toBe(0)
    expect(result.before).toEqual(result.after)
    expect(result.before!.bytes).toBe(new TextEncoder().encode(raw).length)
  }
})

test('empty files have zero lines and are distinct from a newline or BOM', () => {
  expect(compareText('', '').before).toEqual({ bytes: 0, lines: 0, bom: false,
    endings: { LF: 0, CRLF: 0, CR: 0 }, finalNewline: false })
  for (const raw of ['\n', '\r\n', '\r', '\ufeff', 'hello', 'hello\n']) {
    expect(complete('', raw).rows.every((row) => row.kind === 'add')).toBe(true)
    expect(complete(raw, '').rows.every((row) => row.kind === 'remove')).toBe(true)
  }
})

test('insertions, deletions and replacements retain line numbers and exact bytes', () => {
  for (const [before, after] of [
    ['one\ntwo\n', 'zero\none\ntwo\n'], ['one\ntwo\n', 'one\nthree\ntwo\n'],
    ['one\ntwo\n', 'one\ntwo\nthree\n'], ['one\ntwo\nthree\n', 'one\nthree\n'],
    ['old\n', 'new\n'], ['a\nb\nc', 'b\nc\na'], ['a\na\nb\na\n', 'a\nb\na\na\n'],
  ]) complete(before!, after!)
})

test('equal text with changed LF, CRLF, CR or final newline is a real change', () => {
  for (const [before, after] of [
    ['same\n', 'same\r\n'], ['same\n', 'same\r'], ['same', 'same\n'],
    ['same\r\n', 'same'], ['a\r\nb\nc\r', 'a\nb\r\nc'], ['\n\n', '\n'],
  ]) {
    const result = complete(before!, after!)
    expect(result.added + result.removed).toBeGreaterThan(0)
  }
  const result = complete('a\r\nb\nc\r', 'a\nb\r\nc')
  expect(result.before!.endings).toEqual({ LF: 1, CRLF: 1, CR: 1 })
  expect(result.before!.finalNewline).toBe(true)
  expect(result.after!.finalNewline).toBe(false)
})

test('BOM and format controls remain literal data rather than normalized equality', () => {
  for (const [before, after] of [['\ufeffhello', 'hello'], ['a\u202eb', 'ab'], ['é', 'e\u0301']]) {
    const result = complete(before!, after!)
    expect(result.rows[0]!.text).toBe(before!)
  }
  const result = complete('\ufeffhello\n', 'hello\n')
  expect(result.before!.bom).toBe(true)
  expect(result.after!.bom).toBe(false)
})

test('markup remains plain string data with no inserted escaping or HTML', () => {
  const before = '<script>alert("before")</script>\n<img onerror="run()">'
  const after = '<script>alert("after")</script>\n<img onerror="run()">'
  const result = complete(before, after)
  expect(result.rows.some((row) => row.text === '<script>alert("after")</script>')).toBe(true)
  expect(result.rows.some((row) => row.text.includes('&lt;'))).toBe(false)
})

test('default three-line hunks omit only unchanged context and retain full bounded rows', () => {
  const before = Array.from({ length: 24 }, (_, index) => `line ${index + 1}\n`)
  const after = [...before]
  after[4] = 'changed five\n'; after[18] = 'changed nineteen\n'
  const result = complete(before.join(''), after.join(''))
  expect(DIFF_CONTEXT).toBe(3)
  expect(result.hunks).toEqual([
    { start: 1, end: 9, beforeStart: 2, beforeCount: 7, afterStart: 2, afterCount: 7 },
    { start: 16, end: 24, beforeStart: 16, beforeCount: 7, afterStart: 16, afterCount: 7 },
  ])
  const visible = new Set(result.hunks.flatMap((hunk) => Array.from({ length: hunk.end - hunk.start }, (_, i) => hunk.start + i)))
  result.rows.forEach((row, index) => { if (!visible.has(index)) expect(row.kind).toBe('context') })
})

test('overlapping or adjacent hunk context merges without duplicated rows', () => {
  const before = Array.from({ length: 20 }, (_, index) => `line ${index + 1}\n`)
  for (const second of [7, 10]) {
    const after = [...before]
    after[3] = 'changed four\n'; after[second] = 'changed later\n'
    const result = complete(before.join(''), after.join(''))
    expect(result.hunks).toHaveLength(1)
    expect(result.hunks[0]!.start).toBe(0)
  }
})

test('empty-side hunk locations use unified-diff zero start with zero count', () => {
  expect(complete('', 'new\n').hunks).toEqual([
    { start: 0, end: 1, beforeStart: 0, beforeCount: 0, afterStart: 1, afterCount: 1 },
  ])
  expect(complete('old\n', '').hunks).toEqual([
    { start: 0, end: 1, beforeStart: 1, beforeCount: 1, afterStart: 0, afterCount: 0 },
  ])
})

function lcsLength(a: string[], b: string[]): number {
  const table = Array.from({ length: a.length + 1 }, () => Array<number>(b.length + 1).fill(0))
  for (let i = 1; i <= a.length; i++) for (let j = 1; j <= b.length; j++) {
    table[i]![j] = a[i - 1] === b[j - 1] ? table[i - 1]![j - 1]! + 1 : Math.max(table[i - 1]![j]!, table[i]![j - 1]!)
  }
  return table[a.length]![b.length]!
}

test('all short repeated-line sequences reconstruct and have minimum insert/delete distance', () => {
  const sequences: string[][] = [[]]
  let previous: string[][] = [[]]
  for (let size = 1; size <= 3; size++) {
    previous = previous.flatMap((sequence) => ['a\n', 'b\n', '\n'].map((line) => [...sequence, line]))
    sequences.push(...previous)
  }
  for (const a of sequences) for (const b of sequences) {
    if (a.join('') === b.join('')) continue
    const result = complete(a.join(''), b.join(''))
    expect(result.added + result.removed).toBe(a.length + b.length - 2 * lcsLength(a, b))
    expect(compareText(a.join(''), b.join(''))).toEqual(result)
  }
})

test('Unicode, mixed endings and repeated lines survive deterministic generated cases', () => {
  let seed = 1417
  const next = () => { seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0; return seed }
  const tokens = ['中文\r\n', '🙂\n', '\n', 'same\r', '<b>\n', '\ufeff\n']
  for (let sample = 0; sample < 300; sample++) {
    const a = Array.from({ length: next() % 15 }, () => tokens[next() % tokens.length]!).join('')
    const b = Array.from({ length: next() % 15 }, () => tokens[next() % tokens.length]!).join('')
    if (a !== b) complete(a, b)
  }
})

test('byte limits count UTF8 and accept exactly 64 KiB without normalization', () => {
  complete('a'.repeat(MAX_DIFF_BYTES), 'b'.repeat(MAX_DIFF_BYTES))
  complete('🙂'.repeat(MAX_DIFF_BYTES / 4), 'é'.repeat(MAX_DIFF_BYTES / 2))
  for (const raw of ['a'.repeat(MAX_DIFF_BYTES + 1), '🙂'.repeat(MAX_DIFF_BYTES / 4 + 1),
    'é'.repeat(MAX_DIFF_BYTES / 2 + 1), 'x'.repeat(MAX_DIFF_BYTES * 64)]) {
    limited(compareText(raw, ''), 'bytes')
    limited(compareText('', raw), 'bytes')
    limited(compareText(raw, raw), 'bytes')
  }
})

test('line limit accepts 2000 lines and rejects overflow including equal text', () => {
  complete('a\n'.repeat(MAX_DIFF_LINES), 'a\n'.repeat(MAX_DIFF_LINES - 1) + 'b\n')
  for (const ending of ['\n', '\r\n', '\r']) {
    const raw = ending.repeat(MAX_DIFF_LINES + 1)
    limited(compareText(raw, ''), 'lines')
    limited(compareText('', raw), 'lines')
    limited(compareText(raw, raw), 'lines')
  }
})

test('sparse changes near both ends of a 2000-line file remain practical', () => {
  const before = Array.from({ length: MAX_DIFF_LINES }, (_, index) => `line ${index}\n`)
  const after = [...before]
  after[1] = 'new first\n'; after[MAX_DIFF_LINES - 2] = 'new last\n'
  const result = complete(before.join(''), after.join(''))
  expect(result.added).toBe(2)
  expect(result.removed).toBe(2)
  expect(result.work).toBeLessThan(10000)
})

test('work and trace exhaustion return no misleading partial rows or totals', () => {
  expect(MAX_DIFF_TRACE_CELLS).toBeLessThanOrEqual(MAX_DIFF_WORK)
  const before = Array.from({ length: MAX_DIFF_LINES }, (_, index) => `before ${index}\n`).join('')
  const after = Array.from({ length: MAX_DIFF_LINES }, (_, index) => `after ${index}\n`).join('')
  limited(compareText(before, after), 'work')
  limited(compareText('shared\n' + before.slice(9), 'shared\n' + after.slice(8)), 'work')
})

test('malformed UTF16 cannot be silently reencoded as a different UTF8 string', () => {
  for (const raw of ['\ud800', '\udfff', 'a\ud800b', '\udc00\ud800']) {
    limited(compareText(raw, ''), 'invalid_text')
    limited(compareText('', raw), 'invalid_text')
    limited(compareText(raw, raw), 'invalid_text')
  }
  complete('🙂', '🫧')
  limited(compareText(null as unknown as string, ''), 'invalid_text')
})
