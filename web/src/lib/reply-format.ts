/** A bounded block-format preview, not a CommonMark parser. Unrecognized syntax stays literal. */
export const MAX_FORMAT_BYTES = 65536
export const MAX_FORMAT_LINES = 1024
export const MAX_FORMAT_PARTS = 512
const MAX_BLOCKS = 256
type TextKind = 'paragraph' | 'heading' | 'quote' | 'spacing'
type ListItem = { marker: string; text: string }
type TextBlock = { kind: TextKind; text: string; level: number; source: string }
type ListBlock = { kind: 'list'; ordered: boolean; items: ListItem[]; source: string }
type CodeBlock = { kind: 'code'; language: string; text: string; closed: boolean; source: string }
export type ReplyBlock = TextBlock | ListBlock | CodeBlock
export type ReplyPreview = { blocks: ReplyBlock[]; limited: boolean }
type Fence = { marker: string; language: string }
type Line = { raw: string; text: string; kind: TextKind | 'list'; content: string; level: number;
  marker: string; ordered: boolean; fence: Fence | null }

function describe(raw: string): Line {
  const text = raw.replace(/\r?\n$/, ''), fence = /^ {0,3}(`{3,32}|~{3,32})[ \t]*([A-Za-z0-9_+#.-]{0,32})[ \t]*$/.exec(text)
  const heading = /^(#{1,6})[ \t]+(.+)$/.exec(text), quote = /^>[ \t]?(.*)$/.exec(text)
  const list = /^([-+*]|\d{1,9}\.)[ \t]+(.*)$/.exec(text)
  const kind = heading ? 'heading' : quote ? 'quote' : list ? 'list' : text.trim() ? 'paragraph' : 'spacing'
  return { raw, text, kind, content: heading?.[2] ?? quote?.[1] ?? list?.[2] ?? raw,
    level: heading?.[1].length ?? 0, marker: list?.[1] ?? '', ordered: !!list?.[1].endsWith('.'),
    fence: fence ? { marker: fence[1], language: fence[2] } : null }
}

function closes(line: Line, fence: Fence): boolean {
  const marker = /^ {0,3}(`{3,}|~{3,})[ \t]*$/.exec(line.text)?.[1] ?? ''
  return marker[0] === fence.marker[0] && marker.length >= fence.marker.length
}

function codeBlock(lines: Line[], start: number, fence: Fence): { block: CodeBlock; next: number } {
  let end = start + 1
  while (end < lines.length && !closes(lines[end], fence)) end++
  const closed = end < lines.length, next = end + Number(closed)
  return { block: { kind: 'code', language: fence.language, closed,
    text: lines.slice(start + 1, end).map((line) => line.raw).join(''),
    source: lines.slice(start, next).map((line) => line.raw).join('') }, next }
}

function plainBlock(lines: Line[], start: number): { block: TextBlock | ListBlock; next: number } {
  const first = lines[start]
  let next = start + 1
  while (first.kind !== 'heading' && next < lines.length && !lines[next].fence &&
    lines[next].kind === first.kind && lines[next].ordered === first.ordered) next++
  const group = lines.slice(start, next), source = group.map((line) => line.raw).join('')
  const block = first.kind === 'list'
    ? { kind: 'list' as const, ordered: first.ordered, items: group.map((line) => ({ marker: line.marker, text: line.content })), source }
    : { kind: first.kind, level: first.level, text: group.map((line) => line.content).join(first.kind === 'quote' ? '\n' : ''), source }
  return { block, next }
}

export function formatReply(source: string): ReplyPreview {
  const limited: ReplyPreview = { blocks: [], limited: true }
  if (source.length > MAX_FORMAT_BYTES || new TextEncoder().encode(source).length > MAX_FORMAT_BYTES) return limited
  const rawLines = source.match(/[^\n]*\n|[^\n]+$/g) ?? []
  if (rawLines.length > MAX_FORMAT_LINES) return limited
  const lines = rawLines.map(describe), blocks: ReplyBlock[] = []
  let index = 0, parts = 0
  while (index < lines.length) {
    const fence = lines[index].fence
    const result = fence ? codeBlock(lines, index, fence) : plainBlock(lines, index)
    parts += 1 + (result.block.kind === 'list' ? result.block.items.length : 0)
    if (blocks.length >= MAX_BLOCKS || parts > MAX_FORMAT_PARTS) return limited
    blocks.push(result.block); index = result.next
  }
  return { blocks, limited: false }
}
