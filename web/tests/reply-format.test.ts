import { expect, test } from 'bun:test'
import { formatReply, MAX_FORMAT_BYTES, MAX_FORMAT_LINES, MAX_FORMAT_PARTS } from '../src/lib/reply-format'

test('basic preview keeps every source byte while rendering explicit block kinds', () => {
  const source = '# Result\n\nA paragraph\ncontinues here.\n\n> quoted evidence\n> second line\n\n- first\n- second\n\n```python\nprint("hello")\n```\n'
  const result = formatReply(source)
  expect(result.limited).toBeFalse()
  expect(result.blocks.map((block) => block.source).join('')).toBe(source)
  expect(result.blocks.filter((block) => block.kind !== 'spacing').map((block) => block.kind)).toEqual(['heading', 'paragraph', 'quote', 'list', 'code'])
  expect(result.blocks.find((block) => block.kind === 'code')).toMatchObject({ text: 'print("hello")\n', language: 'python', closed: true })
})

test('a code fence directly following prose is recognized without placeholder substitution', () => {
  const source = 'Intro\n```js\nconst value = "IRIS_CODE_BLOCK_0_END"\n```\nIRIS_CODE_BLOCK_0_END'
  const result = formatReply(source)
  expect(result.blocks.map((block) => block.kind)).toEqual(['paragraph', 'code', 'paragraph'])
  expect(result.blocks.at(-1)).toMatchObject({ text: 'IRIS_CODE_BLOCK_0_END' })
  expect(result.blocks.map((block) => block.source).join('')).toBe(source)
})

test('code copying preserves exact CRLF, tabs, Unicode and trailing newline bytes', () => {
  const code = '\tprint("界")\r\n\r\n'
  const source = '```python\r\n' + code + '```\r\n'
  expect(formatReply(source).blocks[0]).toMatchObject({ kind: 'code', text: code, closed: true })
})

test('unclosed fences stay visible and are explicitly incomplete', () => {
  const source = '~~~sh\nprintf "unfinished"\n'
  const result = formatReply(source)
  expect(result.blocks[0]).toMatchObject({ kind: 'code', text: 'printf "unfinished"\n', closed: false })
  expect(result.blocks[0].source).toBe(source)
})

test('fence terminators must match the opening character and minimum length', () => {
  const source = '````txt\n```\n~~~\nkept\n`````\n'
  expect(formatReply(source).blocks[0]).toMatchObject({ kind: 'code', text: '```\n~~~\nkept\n', closed: true })
})

test('raw markup, URLs and unsupported syntax remain literal data', () => {
  const source = '<img src=x onerror="bad()">\n<script>keep this proposal</script>\n\n[link](javascript:bad())\n\n| a | b |\n| - | - |\n\n  - nested list stays literal\n'
  const result = formatReply(source)
  expect(result.blocks.every((block) => ['paragraph', 'spacing'].includes(block.kind))).toBeTrue()
  expect(result.blocks.map((block) => block.source).join('')).toBe(source)
  expect(result.blocks.map((block) => block.kind === 'paragraph' ? block.text : '').join('')).toContain('<script>keep this proposal</script>')
})

test('list continuation lines and original ordered markers are not discarded or renumbered', () => {
  const source = '- first\ncontinuation remains visible\n- second\n\n3. third\n8. eighth\n'
  const result = formatReply(source)
  expect(result.blocks.map((block) => block.source).join('')).toBe(source)
  expect(result.blocks[1]).toMatchObject({ kind: 'paragraph', text: 'continuation remains visible\n' })
  const last = result.blocks.at(-1)
  expect(last).toMatchObject({ kind: 'list', ordered: true, items: [{ marker: '3.', text: 'third' }, { marker: '8.', text: 'eighth' }] })
})

test('oversized UTF-8 inputs fall back to original text without a partial preview', () => {
  expect(formatReply('x'.repeat(MAX_FORMAT_BYTES + 1))).toEqual({ blocks: [], limited: true })
  expect(formatReply('界'.repeat(Math.ceil(MAX_FORMAT_BYTES / 3)))).toEqual({ blocks: [], limited: true })
})

test('line and format-part budgets also fall back without dropping output', () => {
  expect(formatReply('line\n'.repeat(MAX_FORMAT_LINES + 1))).toEqual({ blocks: [], limited: true })
  expect(formatReply('- item\n'.repeat(MAX_FORMAT_PARTS + 1))).toEqual({ blocks: [], limited: true })
  expect(formatReply('# title\n'.repeat(257))).toEqual({ blocks: [], limited: true })
})

test('empty and whitespace-only replies have lossless source representation', () => {
  expect(formatReply('')).toEqual({ blocks: [], limited: false })
  const source = '\n\n \t\r\n'
  expect(formatReply(source).blocks.map((block) => block.source).join('')).toBe(source)
})

test('mixed unsupported punctuation and adjacent blocks never lose source lines', () => {
  const lines = ['# title\n', 'paragraph <b>literal</b>\r\n', '> quote\n', '- item\n', '9. numbered\n',
    '  - nested\n', '[link](https://example.test)\n', '```ts\n', '\tconst x = "<safe>"\n', '```\n', '\n', '~~~~\n']
  for (let index = 0; index < 64; index++) {
    const source = Array.from({ length: 48 }, (_, offset) => lines[(index * 7 + offset * 5) % lines.length]).join('')
    const preview = formatReply(source)
    expect(preview.limited).toBeFalse()
    expect(preview.blocks.map((block) => block.source).join('')).toBe(source)
  }
})
