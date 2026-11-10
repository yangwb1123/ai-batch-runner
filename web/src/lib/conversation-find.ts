export type FindSource = Readonly<{ key: string; kind: 'prompt' | 'reply'; label: string; text: string }>
export type FindMatch = Readonly<{ key: string; start: number; end: number; index: number }>
export type FindResult = { query: string; matches: FindMatch[]; limited: boolean }
export type FindPart = { text: string; match: number | null; selected: boolean }

const MAX_MATCHES = 200
const MAX_PROMPT_BYTES = 16384
const MAX_REPLY_BYTES = 262144
const CONTROL = /[\u0000-\u001f\u007f-\u009f\u061c\u200e\u200f\u2028-\u202e\u2066-\u2069]/u
const encoder = new TextEncoder()

function textBytes(value: string, maximum: number): number {
  if (typeof value !== 'string' || value.length > maximum) throw Error('对话查找内容超过读取范围。')
  const bytes = encoder.encode(value).length
  if (bytes > maximum) throw Error('对话查找内容超过读取范围。')
  return bytes
}

export function normalizeFindQuery(query: string): string {
  if (typeof query !== 'string' || query.length > 512) throw Error('查找文字超过输入长度上限。')
  if (CONTROL.test(query)) throw Error('查找文字不能包含控制或方向字符。')
  const normalized = query.trim()
  if (normalized.length > 256 || [...normalized].length > 128 || encoder.encode(normalized).length > 512) {
    throw Error('查找文字最多 128 个字符 / 512 UTF-8 字节。')
  }
  return normalized
}

function sourceBytes(source: FindSource): number {
  if (!source || !['prompt', 'reply'].includes(source.kind) || !source.key) {
    throw Error('对话查找来源无效。')
  }
  textBytes(source.key, 1024)
  textBytes(source.label, 512)
  if ([...source.label].length > 128 || CONTROL.test(source.label)) throw Error('对话查找来源标签无效。')
  return textBytes(source.text, source.kind === 'prompt' ? MAX_PROMPT_BYTES : MAX_REPLY_BYTES)
}

function validateSources(sources: readonly FindSource[]): void {
  if (!Array.isArray(sources) || sources.length > 65) throw Error('对话查找来源超过读取范围。')
  const keys = new Set<string>()
  let prompts = 0, replies = 0, replyBytes = 0
  for (const source of sources) {
    const bytes = sourceBytes(source)
    if (keys.has(source.key)) throw Error('对话查找来源重复。')
    keys.add(source.key)
    if (source.kind === 'prompt') prompts++
    else { replies++; replyBytes += bytes }
  }
  if (prompts > 1 || replies > 64 || replyBytes > MAX_REPLY_BYTES) throw Error('对话查找内容超过读取范围。')
}

/** Literal Unicode simple case matching; offsets always refer to the original UTF-16 text. */
export function findConversation(sources: readonly FindSource[], query: string): FindResult {
  const normalized = normalizeFindQuery(query)
  validateSources(sources)
  const matches: FindMatch[] = []
  if (!normalized) return { query: normalized, matches, limited: false }
  const pattern = new RegExp(normalized.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'giu')
  for (const source of sources) {
    pattern.lastIndex = 0
    let match: RegExpExecArray | null
    while ((match = pattern.exec(source.text)) !== null) {
      if (matches.length === MAX_MATCHES) return { query: normalized, matches, limited: true }
      matches.push({ key: source.key, start: match.index, end: match.index + match[0].length, index: matches.length })
    }
  }
  return { query: normalized, matches, limited: false }
}

function splitSurrogate(text: string, offset: number): boolean {
  return offset > 0 && offset < text.length && /[\ud800-\udbff]/.test(text[offset - 1]!) && /[\udc00-\udfff]/.test(text[offset]!)
}

function validateMatch(text: string, match: FindMatch, position: number, previousIndex: number, key: string): void {
  if (!match || match.key !== key || !key || !Number.isSafeInteger(match.index) || match.index <= previousIndex ||
      match.index >= MAX_MATCHES || !Number.isSafeInteger(match.start) || !Number.isSafeInteger(match.end) ||
      match.start < position || match.end <= match.start || match.end > text.length ||
      splitSurrogate(text, match.start) || splitSurrogate(text, match.end)) throw Error('对话查找位置已变化，请重新查找。')
}

/** Plain text fragments only; callers render text nodes and never interpret source markup. */
export function findParts(text: string, matches: readonly FindMatch[], selectedIndex: number): FindPart[] {
  textBytes(text, MAX_REPLY_BYTES)
  if (!Array.isArray(matches) || matches.length > MAX_MATCHES || !Number.isSafeInteger(selectedIndex) ||
      selectedIndex < -1 || selectedIndex >= MAX_MATCHES) throw Error('对话查找位置无效。')
  const parts: FindPart[] = []
  let position = 0, previousIndex = -1
  for (const match of matches) {
    validateMatch(text, match, position, previousIndex, matches[0]?.key ?? '')
    if (match.start > position) parts.push({ text: text.slice(position, match.start), match: null, selected: false })
    parts.push({ text: text.slice(match.start, match.end), match: match.index, selected: match.index === selectedIndex })
    position = match.end; previousIndex = match.index
  }
  if (position < text.length || !parts.length) parts.push({ text: text.slice(position), match: null, selected: false })
  return parts
}
