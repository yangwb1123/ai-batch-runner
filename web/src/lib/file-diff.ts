/** Pure, bounded line comparison. Callers must separately authorize and verify snapshot bytes. */
export const MAX_DIFF_BYTES = 65536
export const MAX_DIFF_LINES = 2000
export const MAX_DIFF_ROWS = MAX_DIFF_LINES * 2
export const MAX_DIFF_WORK = 2000000
export const MAX_DIFF_TRACE_CELLS = 2000000
export const DIFF_CONTEXT = 3

export type LineEnding = 'LF' | 'CRLF' | 'CR' | 'none'
export type TextMetadata = { bytes: number; lines: number; bom: boolean;
  endings: { LF: number; CRLF: number; CR: number }; finalNewline: boolean }
export type DiffRow = { kind: 'context' | 'remove' | 'add'; text: string; ending: LineEnding;
  beforeLine: number | null; afterLine: number | null }
/** start is inclusive and end is exclusive in the complete rows array. */
export type DiffHunk = { start: number; end: number; beforeStart: number; beforeCount: number;
  afterStart: number; afterCount: number }
export type DiffLimit = 'bytes' | 'lines' | 'work' | 'invalid_text'
type DiffSummary = { before: TextMetadata | null; after: TextMetadata | null; work: number;
  rows: DiffRow[]; hunks: DiffHunk[] }
export type DiffResult = DiffSummary & (
  { state: 'equal'; added: 0; removed: 0 } |
  { state: 'changed'; added: number; removed: number } |
  { state: 'limited'; reason: DiffLimit; added: null; removed: null })

type Line = { text: string; ending: LineEnding; raw: string; id: number }
type Parsed = { ok: true; lines: Line[]; metadata: TextMetadata } | { ok: false; reason: DiffLimit }
const exhausted = Symbol('diff budget exhausted')

class WorkBudget {
  used = 0
  step(): void {
    if (this.used === MAX_DIFF_WORK) throw exhausted
    this.used++
  }
}

function utf8Size(text: string): number | DiffLimit {
  if (typeof text !== 'string') return 'invalid_text'
  // UTF8 is never shorter than valid UTF16 code units; reject huge input before scanning/allocation.
  if (text.length > MAX_DIFF_BYTES) return 'bytes'
  let bytes = 0
  for (let index = 0; index < text.length; index++) {
    const point = text.codePointAt(index)!
    if (point >= 0xd800 && point <= 0xdfff) return 'invalid_text'
    bytes += point < 0x80 ? 1 : point < 0x800 ? 2 : point < 0x10000 ? 3 : 4
    if (bytes > MAX_DIFF_BYTES) return 'bytes'
    if (point > 0xffff) index++
  }
  return bytes
}

function parse(text: string): Parsed {
  const bytes = utf8Size(text)
  if (typeof bytes !== 'number') return { ok: false, reason: bytes }
  const lines: Line[] = [], endings = { LF: 0, CRLF: 0, CR: 0 }
  let start = 0
  for (let index = 0; index < text.length; index++) {
    const char = text[index]
    if (char !== '\r' && char !== '\n') continue
    const ending = char === '\n' ? 'LF' : text[index + 1] === '\n' ? 'CRLF' : 'CR'
    const end = index + (ending === 'CRLF' ? 2 : 1)
    lines.push({ text: text.slice(start, index), raw: text.slice(start, end), ending, id: 0 })
    if (lines.length > MAX_DIFF_LINES) return { ok: false, reason: 'lines' }
    endings[ending]++
    start = end
    index = end - 1
  }
  if (start < text.length) lines.push({ text: text.slice(start), raw: text.slice(start), ending: 'none', id: 0 })
  if (lines.length > MAX_DIFF_LINES) return { ok: false, reason: 'lines' }
  return { ok: true, lines, metadata: { bytes, lines: lines.length, bom: text.startsWith('\ufeff'), endings,
    finalNewline: lines.length > 0 && lines[lines.length - 1]!.ending !== 'none' } }
}

function identify(before: Line[], after: Line[]): void {
  // Intern complete raw lines once so each alignment comparison is between bounded integers.
  const ids = new Map<string, number>()
  for (const lines of [before, after]) for (const line of lines) {
    let id = ids.get(line.raw)
    if (id === undefined) { id = ids.size; ids.set(line.raw, id) }
    line.id = id
  }
}

function row(kind: DiffRow['kind'], line: Line, beforeLine: number | null, afterLine: number | null): DiffRow {
  // text is deliberately unescaped. Render as text and expose formatting controls in the UI.
  return { kind, text: line.text, ending: line.ending, beforeLine, afterLine }
}

function backtrack(trace: Int32Array[], before: Line[], after: Line[], offset: number): DiffRow[] {
  const rows: DiffRow[] = []
  let x = before.length, y = after.length
  for (let distance = trace.length - 1; distance > 0; distance--) {
    const diagonal = x - y, index = (diagonal + distance) / 2, previous = trace[distance - 1]!
    const down = index === 0 || (index !== distance && previous[index - 1]! < previous[index]!)
    const previousDiagonal = diagonal + (down ? 1 : -1)
    const previousX = previous[down ? index : index - 1]!, previousY = previousX - previousDiagonal
    while (x > previousX && y > previousY) {
      x--; y--
      rows.push(row('context', before[x]!, offset + x + 1, offset + y + 1))
    }
    if (down) { y--; rows.push(row('add', after[y]!, null, offset + y + 1)) }
    else { x--; rows.push(row('remove', before[x]!, offset + x + 1, null)) }
  }
  while (x > 0 && y > 0) {
    x--; y--
    rows.push(row('context', before[x]!, offset + x + 1, offset + y + 1))
  }
  return rows.reverse()
}

function myers(before: Line[], after: Line[], offset: number, budget: WorkBudget): DiffRow[] {
  if (!before.length) return after.map((line, index) => row('add', line, null, offset + index + 1))
  if (!after.length) return before.map((line, index) => row('remove', line, offset + index + 1, null))
  const trace: Int32Array[] = []
  let cells = 0
  for (let distance = 0; distance <= before.length + after.length; distance++) {
    cells += distance + 1
    // Only reachable diagonals are stored: at most 8 MiB of trace, never an unbounded matrix.
    if (cells > MAX_DIFF_TRACE_CELLS) throw exhausted
    const frontier = new Int32Array(distance + 1), previous = trace[distance - 1]
    trace.push(frontier)
    for (let index = 0; index <= distance; index++) {
      budget.step()
      const diagonal = 2 * index - distance
      const down = index === 0 || (index !== distance && previous![index - 1]! < previous![index]!)
      let x = distance === 0 ? 0 : down ? previous![index]! : previous![index - 1]! + 1
      let y = x - diagonal
      while (x < before.length && y < after.length) {
        budget.step()
        if (before[x]!.id !== after[y]!.id) break
        x++; y++
      }
      frontier[index] = x
      if (x === before.length && y === after.length) return backtrack(trace, before, after, offset)
    }
  }
  throw exhausted
}

function align(before: Line[], after: Line[], budget: WorkBudget): DiffRow[] {
  identify(before, after)
  let start = 0, suffix = 0
  while (start < before.length && start < after.length) {
    budget.step()
    if (before[start]!.id !== after[start]!.id) break
    start++
  }
  while (suffix < before.length - start && suffix < after.length - start) {
    budget.step()
    if (before[before.length - suffix - 1]!.id !== after[after.length - suffix - 1]!.id) break
    suffix++
  }
  const rows = before.slice(0, start).map((line, index) => row('context', line, index + 1, index + 1))
  rows.push(...myers(before.slice(start, before.length - suffix), after.slice(start, after.length - suffix), start, budget))
  for (let index = suffix; index > 0; index--) {
    rows.push(row('context', before[before.length - index]!, before.length - index + 1, after.length - index + 1))
  }
  return rows
}

function hunks(rows: DiffRow[]): DiffHunk[] {
  const ranges: { start: number; end: number }[] = []
  const before = new Int32Array(rows.length + 1), after = new Int32Array(rows.length + 1)
  rows.forEach((item, index) => {
    before[index + 1] = before[index]! + (item.kind === 'add' ? 0 : 1)
    after[index + 1] = after[index]! + (item.kind === 'remove' ? 0 : 1)
    if (item.kind === 'context') return
    const start = Math.max(0, index - DIFF_CONTEXT), end = Math.min(rows.length, index + DIFF_CONTEXT + 1)
    const previous = ranges[ranges.length - 1]
    if (previous && start <= previous.end) previous.end = end
    else ranges.push({ start, end })
  })
  return ranges.map(({ start, end }) => {
    const beforeCount = before[end]! - before[start]!, afterCount = after[end]! - after[start]!
    return { start, end, beforeStart: before[start]! + (beforeCount ? 1 : 0), beforeCount,
      afterStart: after[start]! + (afterCount ? 1 : 0), afterCount }
  })
}

/**
 * Compare original UTF8 text, including BOM and exact endings, never display-normalized text.
 * Input scans and output are bounded independently; work counts alignment comparisons/frontier cells.
 * Equality still obeys input limits but needs no alignment. A limited result contains no partial diff.
 */
export function compareText(before: string, after: string): DiffResult {
  const oldText = parse(before), newText = before === after ? oldText : parse(after)
  const summary: DiffSummary = { before: oldText.ok ? oldText.metadata : null,
    after: newText.ok ? newText.metadata : null, work: 0, rows: [], hunks: [] }
  if (!oldText.ok) return { ...summary, state: 'limited', reason: oldText.reason, added: null, removed: null }
  if (!newText.ok) return { ...summary, state: 'limited', reason: newText.reason, added: null, removed: null }
  if (before === after) return { ...summary, state: 'equal', added: 0, removed: 0 }
  const budget = new WorkBudget()
  try {
    const rows = align(oldText.lines, newText.lines, budget)
    if (rows.length > MAX_DIFF_ROWS) throw exhausted
    return { ...summary, state: 'changed', work: budget.used, rows, hunks: hunks(rows),
      added: rows.filter((item) => item.kind === 'add').length, removed: rows.filter((item) => item.kind === 'remove').length }
  } catch (error) {
    if (error !== exhausted) throw error
    return { ...summary, state: 'limited', reason: 'work', work: budget.used, added: null, removed: null }
  }
}
