export type Fetcher = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>
export type ProblemKind = 'login' | 'forbidden' | 'limited' | 'unavailable' | 'invalid' | 'timeout'
export type Instance = {
  instance_id: string; name: string; project_id: string; online: boolean | null; last_seen: number | null;
  session_capacity?: number | null; session_close_supported?: boolean
}
export type Session = {
  session_id: string; instance_id: string; name: string; project_id: string;
  status: string; online: boolean | null; updated_at: number | null
}
export type Lists = { instances: Instance; sessions: Session }
const MAX_BYTES = 2 * 1024 * 1024
const CONTROLS = /[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/g

export class ReadProblem extends Error {
  constructor(public readonly kind: ProblemKind) { super(kind) }
}

export function displayText(value: unknown, missing = '未声明'): string {
  if (typeof value !== 'string' && typeof value !== 'number') return missing
  return String(value).replace(CONTROLS, ' ').slice(0, 256) || missing
}

function identifier(value: unknown): string {
  if (typeof value !== 'string' || !value || value.length > 256 || value !== value.replace(CONTROLS, ' ')) {
    throw new ReadProblem('invalid')
  }
  return value
}

const text = (value: unknown) => displayText(value, '')
const epoch = (value: unknown) => typeof value === 'number' && Number.isFinite(value) && value > 0 && value < 1e11 ? value : null
const online = (value: unknown) => typeof value === 'boolean' ? value : null

function project(kind: keyof Lists, value: unknown): Instance | Session {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new ReadProblem('invalid')
  const row = value as Record<string, unknown>
  const common = { instance_id: identifier(row.instance_id), name: text(row.name),
    project_id: text(row.project_id), online: online(row.online) }
  return kind === 'instances' ? { ...common, last_seen: epoch(row.last_seen),
    session_capacity: Number.isInteger(row.session_capacity) && Number(row.session_capacity) >= 0 && Number(row.session_capacity) <= 8 ? Number(row.session_capacity) : null,
    session_close_supported: row.session_close_supported === true } : {
    ...common, session_id: identifier(row.session_id), status: text(row.status), updated_at: epoch(row.updated_at),
  }
}

export async function boundedJson(response: Response, signal: AbortSignal): Promise<unknown> {
  const length = response.headers.get('Content-Length')
  if ((length && (!/^\d{1,9}$/.test(length) || Number(length) > MAX_BYTES)) ||
      !response.headers.get('Content-Type')?.toLowerCase().startsWith('application/json')) throw new ReadProblem('invalid')
  if (!response.body) throw new ReadProblem('invalid')
  const reader = response.body.getReader()
  const chunks: Uint8Array[] = []
  let size = 0
  try {
    while (true) {
      signal.throwIfAborted()
      const { done, value } = await reader.read()
      if (done) break
      size += value.byteLength
      if (size > MAX_BYTES || chunks.length >= 1024) throw new ReadProblem('invalid')
      chunks.push(value)
    }
    signal.throwIfAborted()
    const bytes = new Uint8Array(size)
    let offset = 0
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength }
    try { return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes)) }
    catch { throw new ReadProblem('invalid') }
  } finally {
    await reader.cancel().catch(() => undefined)
    reader.releaseLock()
  }
}

export async function readList<K extends keyof Lists>(kind: K, signal: AbortSignal, fetcher: Fetcher = fetch): Promise<Lists[K][]> {
  const response = await fetcher(`/web/api/${kind}?limit=100`, {
    credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal,
  })
  signal.throwIfAborted()
  if (!response.ok) {
    const kinds: Record<number, ProblemKind> = { 401: 'login', 403: 'forbidden', 429: 'limited' }
    throw new ReadProblem(kinds[response.status] ?? 'unavailable')
  }
  const result = await boundedJson(response, signal)
  const data = result && typeof result === 'object' && !Array.isArray(result) && (result as { data?: unknown }).data
  if (!Array.isArray(data) || data.length > 100) throw new ReadProblem('invalid')
  const rows = data.map((value) => project(kind, value))
  const ids = rows.map((row) => 'session_id' in row ? row.session_id : row.instance_id)
  if (new Set(ids).size !== ids.length) throw new ReadProblem('invalid')
  return rows as Lists[K][]
}
