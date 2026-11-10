import { expect, spyOn, test } from 'bun:test'
import * as conversation from '../src/lib/conversation'
import { findConversation, findParts, normalizeFindQuery, type FindMatch, type FindSource } from '../src/lib/conversation-find'
import { appendEvents, eventPage, MAX_EVENTS, type Events } from '../src/lib/task-events'

const source = (text: string, key = 'reply', kind: FindSource['kind'] = 'reply'): FindSource => ({ key, kind, label: key, text })
const event = (kind: string, text = '', item_id = 'one', extra = {}) => ({ kind, text, item_id, truncated: false, ...extra })

test('terminal reply completion is a shared projection for the conversation and results', () => {
  expect(typeof conversation.replyCompletionMissing).toBe('function')
  expect(conversation.replyCompletionMissing([{ kind: 'message.delta', text: 'unfinished', truncated: false }], 'completed')).toBe(true)
})

test('literal conversation find exposes safe source offsets and text parts', () => {
  const result = findConversation([{ key: 'prompt', kind: 'prompt', label: '你的指令', text: 'Find needle here' }], 'NEEDLE')
  expect(result.matches).toEqual([{ key: 'prompt', start: 5, end: 11, index: 0 }])
  expect(findParts('Find needle here', result.matches, 0)).toEqual([
    { text: 'Find ', match: null, selected: false }, { text: 'needle', match: 0, selected: true },
    { text: ' here', match: null, selected: false },
  ])
})

test('query normalization trims spaces while retaining literal spelling and accepting empty queries', () => {
  expect(normalizeFindQuery('  NeedLE  ')).toBe('NeedLE')
  expect(normalizeFindQuery('　 ')).toBe('')
  expect(findConversation([source('body')], '')).toEqual({ query: '', matches: [], limited: false })
  expect(findConversation([], 'query')).toEqual({ query: 'query', matches: [], limited: false })
  expect(findConversation([source('', 'prompt', 'prompt')], 'x').matches).toEqual([])
})

test('query budget counts Unicode code points and rejects control or bidi input rather than trimming it away', () => {
  expect(normalizeFindQuery('😀'.repeat(128))).toBe('😀'.repeat(128))
  for (const value of ['a'.repeat(129), '😀'.repeat(129), '\nneedle', 'needle\t', '\u0000', '\u007f', '\u0085',
    '\u061c', '\u200e', '\u200f', '\u2028', '\u2029', '\u202a', '\u202e', '\u2066', '\u2069']) {
    expect(() => normalizeFindQuery(value)).toThrow()
  }
  expect(() => normalizeFindQuery(null as unknown as string)).toThrow()
})

test('raw query length is bounded before trimming or inspecting its contents', () => {
  expect(normalizeFindQuery(' '.repeat(512))).toBe('')
  expect(() => normalizeFindQuery(' '.repeat(513))).toThrow()
  expect(() => normalizeFindQuery(' '.repeat(1000000) + 'x')).toThrow()
})

test('Unicode simple case matches retain exact original UTF16 offsets and do not perform full folding', () => {
  const text = '😀K K k | Σ σ ς | Straße STRASSE | İ i | e\u0301 é'
  const search = (query: string) => findConversation([source(text)], query).matches.map((match) => text.slice(match.start, match.end))
  expect(search('k')).toEqual(['K', 'K', 'k'])
  expect(search('σ')).toEqual(['Σ', 'σ', 'ς'])
  expect(search('straße')).toEqual(['Straße'])
  expect(search('i')).toEqual(['i'])
  expect(search('é')).toEqual(['é'])
  expect(findConversation([source(text)], 'K').matches[0]).toMatchObject({ start: 2, end: 3 })
  const astral = findConversation([source('a😀b😀c')], '😀').matches
  expect(astral.map((match) => [match.start, match.end])).toEqual([[1, 3], [4, 6]])
  expect(findParts('a😀b😀c', astral, 1).map((part) => part.text).join('')).toBe('a😀b😀c')
})

test('every regex metacharacter remains literal and can never introduce a regex program', () => {
  for (const literal of ['.*', '(a+)+$', '[a-z]', '\\d+', '^', '$', '{2}', 'a|b', 'x?y', 'a/b', 'a-b', ']']) {
    const text = `before ${literal} after`
    const result = findConversation([source(text)], literal)
    expect(result.matches).toHaveLength(1)
    expect(text.slice(result.matches[0]!.start, result.matches[0]!.end)).toBe(literal)
  }
  expect(findConversation([source('aaaaaaaaaaaaaaaa')], '(a+)+$').matches).toEqual([])
})

test('source order and global indexes remain stable without matching across message boundaries', () => {
  const sources = [source('Needle', 'prompt', 'prompt'), source('NEEDLE and needle', 'reply-1'), source('needle', 'reply-2')]
  expect(findConversation(sources, 'needle').matches.map((match) => [match.key, match.index])).toEqual([
    ['prompt', 0], ['reply-1', 1], ['reply-1', 2], ['reply-2', 3],
  ])
  expect(findConversation([source('cross', 'a'), source('boundary', 'b')], 'crossboundary').matches).toEqual([])
  expect(findConversation([source('aaaa')], 'aa').matches.map((match) => match.start)).toEqual([0, 2])
})

test('chunked replies are searched after assembly and completed text replaces obsolete deltas', () => {
  const view = conversation.projectConversation([
    event('message.delta', 'obsolete draft'),
    event('message.completed', 'across-', 'one', { chunk_index: 0, chunk_count: 2 }),
    event('message.completed', 'chunks', 'one', { chunk_index: 1, chunk_count: 2 }),
  ])
  const sources = view.messages.map((message) => source(message.text, message.id))
  expect(findConversation(sources, 'ACROSS-CHUNKS').matches).toHaveLength(1)
  expect(findConversation(sources, 'obsolete').matches).toEqual([])
})

test('exactly 200 hits is complete and the 201st confirms a limited result across sources', () => {
  for (const total of [199, 200, 201, 5000]) {
    const result = findConversation([source('x '.repeat(total))], 'x')
    expect(result.matches).toHaveLength(Math.min(total, 200))
    expect(result.limited).toBe(total > 200)
    expect(result.matches.at(-1)?.index).toBe(Math.min(total, 200) - 1)
  }
  expect(findConversation([source('x '.repeat(200), 'prompt', 'prompt'), source('x')], 'x').limited).toBe(true)
  expect(findConversation([source('x '.repeat(200)), source('nothing', 'other')], 'x').limited).toBe(false)
})

test('full permitted prompt and reply windows remain searchable without raising their budgets', () => {
  const prompt = source('p'.repeat(16380) + 'last', 'prompt', 'prompt')
  const replies = Array.from({ length: 64 }, (_, index) => source('r'.repeat(4092) + (index === 63 ? 'last' : 'xxxx'), `reply-${index}`))
  const result = findConversation([prompt, ...replies], 'last')
  expect(result.matches.map((match) => [match.key, match.start])).toEqual([['prompt', 16380], ['reply-63', 4092]])
  expect(result.limited).toBe(false)
  expect(findConversation([source('😀'.repeat(65535) + 'last')], 'last').matches[0]?.start).toBe(131070)
})

test('invalid, duplicate and oversized sources fail closed without evicting or merging content', () => {
  const invalid = [
    [source('x'.repeat(16385), 'prompt', 'prompt')], [source('猫'.repeat(87382))],
    [source('x'.repeat(131073), 'a'), source('x'.repeat(131073), 'b')],
    Array.from({ length: 65 }, (_, index) => source('x', `reply-${index}`)),
    [source('a', 'p1', 'prompt'), source('b', 'p2', 'prompt')],
    [source('a', 'same'), source('b', 'same')], [source('a', '')],
    [{ ...source('x'), label: 'x'.repeat(129) }], [{ ...source('x'), key: 'x'.repeat(1025) }],
    [{ ...source('x'), kind: 'other' }], [{ ...source('x'), text: 42 }],
  ]
  for (const sources of invalid) expect(() => findConversation(sources as FindSource[], 'x')).toThrow()
})

test('existing legal message IDs retain their identity through event parsing and local find keys', () => {
  const id = 'item\tline\n\u061c\u200e'
  const events = eventPage({ data: [{ cursor: 1, event_id: 'event-1', session_id: 's', turn_id: 't',
    kind: 'message.completed', payload: { item_id: id, text: 'needle' } }], next_cursor: 1 }, 's', 0, 't')
  const reply = conversation.projectConversation(events.data).messages[0]!
  expect(reply.id).toBe(id)
  for (const key of [reply.id, JSON.stringify(['reply', reply.id])]) {
    const result = findConversation([{ key, kind: 'reply', label: 'Agent 回复 1', text: reply.text }], 'needle')
    expect(result.matches[0]?.key).toBe(key)
    expect(findParts(reply.text, result.matches, 0)).toEqual([{ text: 'needle', match: 0, selected: true }])
  }
})

test('text parts preserve markup, line breaks, adjacent matches and global selected indexes', () => {
  const text = '<script>alert(1)</script>\n猫猫 & "quotes"'
  const result = findConversation([source('猫', 'prompt', 'prompt'), source(text)], '猫')
  const parts = findParts(text, result.matches.filter((match) => match.key === 'reply'), 2)
  expect(parts.map((part) => part.text).join('')).toBe(text)
  expect(parts.filter((part) => part.match !== null)).toEqual([
    { text: '猫', match: 1, selected: false }, { text: '猫', match: 2, selected: true },
  ])
  expect(parts[0]).toEqual({ text: '<script>alert(1)</script>\n', match: null, selected: false })
  expect(findParts('', [], -1)).toEqual([{ text: '', match: null, selected: false }])
  expect(findParts('plain', [], 0)).toEqual([{ text: 'plain', match: null, selected: false }])
})

test('stale, overlapping, mixed-source or surrogate-splitting offsets are rejected', () => {
  const base: FindMatch = { key: 'reply', start: 1, end: 3, index: 0 }
  const invalid: FindMatch[][] = [
    [{ ...base, start: -1 }], [{ ...base, end: 100 }], [{ ...base, end: 1 }],
    [{ ...base, start: 1.5 }], [{ ...base, index: -1 }], [{ ...base, index: 201 }],
    [{ ...base, end: 2 }], [{ ...base, start: 2 }],
    [base, { ...base, start: 2, end: 4, index: 1 }], [base, { ...base, start: 3, end: 4 }],
    [base, { ...base, key: 'other', start: 3, end: 4, index: 1 }],
  ]
  for (const matches of invalid) expect(() => findParts('a😀b', matches, 0)).toThrow()
  for (const selected of [-2, 201, 1.5, NaN]) expect(() => findParts('a😀b', [], selected)).toThrow()
  expect(() => findParts('a'.repeat(202), Array.from({ length: 202 }, (_, index) => ({ key: 'r', start: index, end: index + 1, index })), 0)).toThrow()
})

test('fragment rendering rejects the discarded 201st match and out-of-cap selected indexes', () => {
  const text = 'x '.repeat(201)
  const matches = Array.from({ length: 201 }, (_, index) => ({ key: 'r', start: index * 2, end: index * 2 + 1, index }))
  expect(() => findParts(text, matches, 199)).toThrow()
  expect(() => findParts(text, [matches[200]!], 199)).toThrow()
  expect(() => findParts(text, [], 200)).toThrow()
  const parts = findParts(text, matches.slice(0, 200), 199)
  expect(parts.length).toBeLessThanOrEqual(401)
  expect(parts.filter((part) => part.selected)).toEqual([{ text: 'x', match: 199, selected: true }])
  expect(parts.map((part) => part.text).join('')).toBe(text)
})

test('find and text partition are synchronous, do not fetch, and leave source facts unchanged', () => {
  const sources = Object.freeze([Object.freeze(source('needle and needLE'))]), before = JSON.stringify(sources)
  const fetch = spyOn(globalThis, 'fetch').mockRejectedValue(new Error('unexpected network'))
  try {
    const result = findConversation(sources, 'needle')
    expect(result.matches).toHaveLength(2)
    expect(findParts(sources[0]!.text, result.matches, 0).map((part) => part.text).join('')).toBe(sources[0]!.text)
    expect(JSON.stringify(sources)).toBe(before)
    expect(fetch).not.toHaveBeenCalled()
    expect(result).not.toBeInstanceOf(Promise)
  } finally { fetch.mockRestore() }
})

test('shared completion metadata distinguishes running, absent, ended and restarted replies', () => {
  const events = [event('message.started'), event('message.delta', 'partial')]
  for (const state of ['queued', 'running', 'cancel_requested']) expect(conversation.replyCompletionMissing(events, state)).toBe(false)
  for (const state of ['completed', 'failed', 'cancelled', 'interrupted', 'lost']) expect(conversation.replyCompletionMissing(events, state)).toBe(true)
  expect(conversation.replyCompletionMissing([], 'completed')).toBe(false)
  events.push(event('message.completed', 'final'))
  expect(conversation.replyCompletionMissing(events, 'completed')).toBe(false)
  events.push(event('message.started'))
  expect(conversation.replyCompletionMissing(events, 'completed')).toBe(true)
})

test('completion metadata beyond reply 64 covers the original admitted 500-event window', () => {
  let events: Events | null = null
  for (let start = 0; start < MAX_EVENTS; start += 50) {
    const data = Array.from({ length: 50 }, (_, offset) => ({ cursor: start + offset + 1, event_id: `event-${start + offset}`,
      session_id: 's', turn_id: 't', kind: start + offset === MAX_EVENTS - 1 ? 'message.delta' : 'message.completed',
      payload: { item_id: `reply-${start + offset}`, text: 'text' } }))
    events = appendEvents(events, eventPage({ data, next_cursor: start + 50 }, 's', start, 't')).events
  }
  expect(events!.data).toHaveLength(MAX_EVENTS)
  expect(conversation.projectConversation(events!.data).messages).toHaveLength(64)
  expect(conversation.replyCompletionMissing(events!.data, 'completed')).toBe(true)
})

test('a final completion fragment does not erase missing chunk evidence or fabricate a missing end', () => {
  const events = [event('message.completed', 'last', 'one', { chunk_index: 2, chunk_count: 3 })]
  expect(conversation.projectConversation(events).partial).toBe(true)
  expect(conversation.replyCompletionMissing(events, 'completed')).toBe(false)
  expect(conversation.replyCompletionMissing([event('message.completed', 'first', 'one', { chunk_index: 0, chunk_count: 3 })], 'completed')).toBe(true)
  expect(conversation.projectConversation(events).messages[0]?.complete).toBe(true)
})
