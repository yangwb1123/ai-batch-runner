import { afterEach, describe, expect, test } from 'bun:test'
import { get } from 'svelte/store'
import type { Fetcher } from '../src/lib/api'
import { createWorkspace, readList, displayText } from '../src/lib/workspace'

const active = new AbortController().signal
const json = (data: unknown, status = 200) => new Response(JSON.stringify({ data }), {
  status, headers: { 'Content-Type': 'application/json' },
})
const instances = [{ instance_id: 'alpha', name: '<script>untrusted</script>', online: true, last_seen: 1700000000 }]
const sessions = [{ session_id: 'session-a', instance_id: 'alpha', name: 'session', status: 'idle' }]
const disposables: Array<() => void> = []
afterEach(() => disposables.splice(0).forEach((dispose) => dispose()))

function workspace(fetcher: Fetcher, timeoutMs = 2000) {
  const value = createWorkspace({ fetcher, timeoutMs })
  disposables.push(value.dispose)
  return value
}

describe('bounded, same-origin read projection', () => {
  test('uses only the existing cookie facade, not bearer APIs or browser tokens', async () => {
    const calls: Array<[string, RequestInit | undefined]> = []
    const fetcher = (async (url, options) => {
      calls.push([String(url), options])
      return json(instances)
    }) as Fetcher
    const result = await readList('instances', active, fetcher)
    expect(calls[0][0]).toBe('/web/api/instances?limit=100')
    expect(calls[0][1]).toMatchObject({ credentials: 'same-origin', cache: 'no-store', redirect: 'error' })
    expect(calls[0][1]?.headers).toBeUndefined()
    expect(result[0]).toMatchObject({ instance_id: 'alpha', name: '<script>untrusted</script>', online: true })
  })

  test('keeps zero, unknown heartbeat and untracked fields distinct', async () => {
    const result = await readList('instances', active, (async () => json([{ instance_id: 'alpha', online: 'yes' }])) as Fetcher)
    expect(result[0].online).toBeNull()
    expect(result[0].last_seen).toBeNull()
    expect('cost' in result[0]).toBeFalse()
    expect(displayText(0)).toBe('0')
    expect(displayText(null)).toBe('未声明')
  })

  test.each([
    ['object', {}], ['over-capacity', Array(101).fill(instances[0])], ['duplicate', [instances[0], instances[0]]],
    ['empty ID', [{ instance_id: '' }]], ['long ID', [{ instance_id: 'a'.repeat(257) }]], ['null row', [null]],
  ])('rejects malformed data: %s', async (_name, data) => {
    await expect(readList('instances', active, (async () => json(data)) as Fetcher)).rejects.toMatchObject({ kind: 'invalid' })
  })

  test.each([
    ['HTML', () => new Response('<html>not an API</html>')],
    ['invalid UTF8', () => new Response(new Uint8Array([255]), { headers: { 'Content-Type': 'application/json' } })],
    ['oversized body', () => new Response('x'.repeat(2 * 1024 * 1024 + 1), { headers: { 'Content-Type': 'application/json' } })],
    ['oversized length', () => new Response('{}', { headers: { 'Content-Type': 'application/json', 'Content-Length': '99999999' } })],
  ] as const)('rejects response: %s', async (_name, response) => {
    await expect(readList('instances', active, (async () => response()) as Fetcher)).rejects.toMatchObject({ kind: 'invalid' })
  })

  test.each([[401, 'login'], [403, 'forbidden'], [429, 'limited'], [500, 'unavailable']] as const)(
    'HTTP %s yields safe kind %s, never the untrusted error body', async (status, kind) => {
      await expect(readList('instances', active, (async () => json('secret-diagnostic', status)) as Fetcher))
        .rejects.toMatchObject({ kind })
    })

  test('removes control/bidi text and bounds display without interpreting markup', () => {
    expect(displayText('a\u202eb\u0000c')).toBe('a b c')
    expect(displayText('x'.repeat(300)).length).toBe(256)
    expect(displayText('<img src=x onerror=alert(1)>')).toContain('<img')
  })
})

describe('workspace lifecycle and truthful states', () => {
  test('success includes a read time, real window counts and projected session fields', async () => {
    const value = workspace((async (url) => json(String(url).includes('instances') ? instances : sessions)) as Fetcher)
    expect(get(value).phase).toBe('idle')
    await value.refresh()
    expect(get(value)).toMatchObject({ phase: 'ready', problem: null })
    expect(get(value).instances.length).toBe(1)
    expect(get(value).sessions[0]).toMatchObject({ session_id: 'session-a', instance_id: 'alpha', status: 'idle' })
    expect(get(value).readAt).toBeGreaterThan(0)
  })

  test('an authorized empty window is not a login error or proof of project integration', async () => {
    const value = workspace((async () => json([])) as Fetcher)
    await value.refresh()
    expect(get(value)).toMatchObject({ phase: 'ready', instances: [], sessions: [], problem: null })
  })

  test('refresh immediately clears old records; partial failure never leaves an old success', async () => {
    let failing = false
    const value = workspace((async (url) => String(url).includes('instances') ? json(instances) : json(failing ? [] : sessions, failing ? 403 : 200)) as Fetcher)
    await value.refresh()
    failing = true
    const pending = value.refresh()
    expect(get(value)).toMatchObject({ phase: 'loading', instances: [], sessions: [], readAt: null })
    await pending
    expect(get(value)).toMatchObject({ phase: 'error', instances: [], sessions: [], readAt: null, problem: 'forbidden' })
  })

  test('duplicate refresh is coalesced, with no implicit retries or polling', async () => {
    let calls = 0
    const value = workspace((async () => { calls++; await new Promise((r) => setTimeout(r, 5)); return json([]) }) as Fetcher)
    const first = value.refresh()
    await value.refresh()
    await first
    expect(calls).toBe(2)
  })

  test('dispose aborts owned requests and rejects late response publication', async () => {
    const value = workspace((async () => { await new Promise((r) => setTimeout(r, 20)); return json(instances) }) as Fetcher)
    const pending = value.refresh()
    value.dispose()
    await pending
    expect(get(value)).toMatchObject({ phase: 'idle', instances: [], sessions: [], readAt: null })
  })

  test('deadline covers continuous streamed output and restores the retry control', async () => {
    let stopped = false
    const fetcher = (async (_url, init) => new Response(new ReadableStream({
      async pull(controller) {
        await new Promise((r) => setTimeout(r, 2))
        if (init?.signal?.aborted) { stopped = true; controller.error(new DOMException('aborted', 'AbortError')); return }
        controller.enqueue(new TextEncoder().encode(' '))
      },
    }), { headers: { 'Content-Type': 'application/json' } })) as Fetcher
    const value = workspace(fetcher, 15)
    await value.refresh()
    expect(stopped).toBeTrue()
    expect(get(value)).toMatchObject({ phase: 'error', problem: 'timeout', instances: [], sessions: [] })
  })
})
