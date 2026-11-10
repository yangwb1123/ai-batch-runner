import { expect, test } from 'bun:test'
import { projectConversation } from '../src/lib/conversation'
import { createDrafts } from '../src/lib/drafts'

const event = (kind: string, text: string, extra = {}) => ({ kind, text, cursor: 1, event_id: 'event', turn_id: 'turn', truncated: false, chunk: '', ...extra })

test('completed messages replace their own deltas and preserve other message items', () => {
  const view = projectConversation([
    event('message.delta', 'draft', { item_id: 'one' }),
    event('message.delta', 'second', { item_id: 'two' }),
    event('message.completed', 'final first', { item_id: 'one' }),
    event('message.completed', 'final second', { item_id: 'two' }),
  ])
  expect(view.messages.map((row) => row.text)).toEqual(['final first', 'final second'])
  expect(view.partial).toBeFalse()
})

test('chunked completions assemble across pages and missing chunks remain partial', () => {
  const first = event('message.completed', 'hello ', { item_id: 'one', chunk_index: 0, chunk_count: 2 })
  const last = event('message.completed', 'world', { item_id: 'one', chunk_index: 1, chunk_count: 2 })
  expect(projectConversation([first, last]).messages[0].text).toBe('hello world')
  expect(projectConversation([last]).partial).toBeTrue()
})

test('old event projections use completed text without duplicating delta text', () => {
  const view = projectConversation([event('message.delta', 'hello'), event('message.completed', 'hello world')])
  expect(view.messages[0].text).toBe('hello world')
  expect(view.legacy).toBeTrue()
})

test('untrusted markup stays text and projection has an explicit byte bound', () => {
  const view = projectConversation([event('message.completed', '<script>unsafe</script>' + '字'.repeat(120000), { item_id: 'one' })])
  expect(view.messages[0].text).toContain('<script>unsafe</script>')
  expect(new TextEncoder().encode(view.messages[0].text).length).toBeLessThanOrEqual(262144)
  expect(view.partial).toBeTrue()
})

test('drafts survive reads and session selection but are bounded and clear with account context', () => {
  const drafts = createDrafts()
  drafts.context(1)
  drafts.set('session-a', 'unsent goal')
  drafts.set('session-b', 'other goal')
  expect(drafts.get('session-a')).toBe('unsent goal')
  expect(drafts.consume('session-a', 'different')).toBeFalse()
  expect(drafts.consume('session-a', 'unsent goal')).toBeTrue()
  expect(drafts.get('session-b')).toBe('other goal')
  drafts.context(2)
  expect(drafts.get('session-b')).toBe('')
  expect(() => drafts.set('session-a', '字'.repeat(30000))).toThrow()
})

test('draft capacity rejects new input without evicting another unsent task', () => {
  const drafts = createDrafts()
  for (let index = 0; index < 16; index++) drafts.set(`session-${index}`, `draft-${index}`)
  expect(() => drafts.set('seventeenth', 'unaccepted')).toThrow()
  expect(['session-0', 'seventeenth'].map((id) => drafts.get(id))).toEqual(['draft-0', ''])
  drafts.set('session-0', '')
  drafts.set('seventeenth', 'accepted')
  expect(drafts.get('seventeenth')).toBe('accepted')
})
