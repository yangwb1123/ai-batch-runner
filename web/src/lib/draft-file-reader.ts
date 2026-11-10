import { MAX_DRAFT_BYTES } from './drafts'
import { validateDraftText } from './draft-file'

const MAX_READ_CHUNKS = 128
const READ_DEADLINE_MS = 5000
const timeout = () => new Error('草稿文件未在 5 秒内读取完成，请重新选择。')

function withinDeadline(deadline: number) {
  if (performance.now() >= deadline) throw timeout()
}

function fileSize(file: File): number {
  if (typeof file.name !== 'string' || file.name.length > 256 || !/\.txt$/i.test(file.name) ||
      /[/\\\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/.test(file.name)) {
    throw Error('请选择名称有效的 .txt 纯文本草稿文件。')
  }
  if (!Number.isSafeInteger(file.size) || file.size < 1 || file.size > MAX_DRAFT_BYTES) {
    throw Error('草稿文件必须非空且为 1 至 65536 字节；文件尚未读取。')
  }
  return file.size
}

function readWindow(reader: ReadableStreamDefaultReader<Uint8Array>, signal: AbortSignal, deadline: number) {
  let timer: ReturnType<typeof setTimeout>, rejectWindow: (error: Error) => void
  let cancelled = false
  const expired = new Promise<never>((_, reject) => { rejectWindow = reject })
  const cancel = (error?: Error) => {
    if (cancelled) return
    cancelled = true
    void reader.cancel(error).catch(() => undefined)
  }
  const stop = (error: Error) => { rejectWindow(error); cancel(error) }
  const abort = () => stop(new Error('草稿文件读取已取消。'))
  timer = setTimeout(() => stop(timeout()), Math.max(0, deadline - performance.now()))
  signal.addEventListener('abort', abort, { once: true })
  if (signal.aborted) abort()
  return { expired, close() { clearTimeout(timer); signal.removeEventListener('abort', abort); cancel() } }
}

async function bytesFrom(reader: ReadableStreamDefaultReader<Uint8Array>, expired: Promise<never>, size: number, deadline: number) {
  const bytes = new Uint8Array(size)
  let count = 0, total = 0
  while (true) {
    withinDeadline(deadline)
    const chunk = await Promise.race([reader.read(), expired])
    withinDeadline(deadline)
    if (chunk.done) break
    if (++count > MAX_READ_CHUNKS || !(chunk.value instanceof Uint8Array) || total + chunk.value.length > size) {
      throw Error('草稿文件超过读取预算或长度不符，原稿未更改。')
    }
    bytes.set(chunk.value, total); total += chunk.value.length
  }
  if (total !== size) throw Error('草稿文件未完整读取，原稿未更改。')
  return bytes
}

/** Only reads the selected local file, with a single deadline and cancellation of this reader. */
export async function readDraftText(file: File, signal: AbortSignal): Promise<string> {
  const deadline = performance.now() + READ_DEADLINE_MS
  signal.throwIfAborted()
  const size = fileSize(file), reader = file.stream().getReader(), window = readWindow(reader, signal, deadline)
  try {
    const bytes = await bytesFrom(reader, window.expired, size, deadline)
    signal.throwIfAborted()
    let text: string
    try { text = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(bytes) }
    catch { throw Error('草稿文件不是有效 UTF-8 文本，原稿未更改。') }
    text = validateDraftText(text)
    withinDeadline(deadline)
    return text
  } finally {
    window.close()
    reader.releaseLock()
  }
}
