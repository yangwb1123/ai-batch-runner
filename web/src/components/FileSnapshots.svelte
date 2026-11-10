<script lang="ts">
  import { onMount, onDestroy } from 'svelte'
  import { IrisButton } from '../lib/iris'
  import { createFileReader, fileTarget, type FileView } from '../lib/file-reader'
  import { unavailableLabels, type FileMetadata } from '../lib/file-snapshots'
  import { localTime } from '../lib/presentation'
  import type { Tasks } from '../lib/task-state'
  let { snapshot, fileEpoch = 0, notice = '', denied = () => {} }: { snapshot: Tasks; fileEpoch?: number; notice?: string; denied?: (problem: string) => void } = $props()
  let view = $state<FileView>({ list: null, preview: null, busy: false, problem: '', feedback: '' })
  let objectUrl = '', revokeTimer: ReturnType<typeof setTimeout> | undefined
  function release() {
    clearTimeout(revokeTimer)
    if (objectUrl) URL.revokeObjectURL(objectUrl)
    objectUrl = ''
  }
  function save(file: FileMetadata, bytes: Uint8Array) {
    release()
    objectUrl = URL.createObjectURL(new Blob([new Uint8Array(bytes)], { type: 'application/octet-stream' }))
    const anchor = document.createElement('a')
    anchor.href = objectUrl; anchor.download = file.path.split('/').at(-1)!
    document.body.append(anchor)
    try { anchor.click() } finally { anchor.remove() }
    revokeTimer = setTimeout(release, 1000)
  }
  function currentTarget() {
    const selected = fileTarget(snapshot)
    return selected ? { ...selected, key: `${selected.key}/${fileEpoch}` } : null
  }
  const reader = createFileReader(currentTarget, (next) => { view = next }, save, release, fetch, (problem) => denied(problem))
  const target = $derived(currentTarget())
  $effect(() => { target; reader.bind() })
  onMount(() => { window.addEventListener('pagehide', reader.clear); return () => window.removeEventListener('pagehide', reader.clear) })
  onDestroy(reader.clear)
</script>

<section class="results-section file-snapshots" aria-label="文件检查快照">
  <div class="result-row-heading"><h4>文件检查快照</h4>
    <IrisButton size="sm" variant="outline" disabled={!target || view.busy} onclick={() => void reader.read()}>刷新文件快照</IrisButton></div>
  <p class="secondary-copy">执行端显式共享的工作区文件。摘要仅核对内容完整性；不证明文件由本轮生成，也不代表目标验收。各文件可能采集于不同时间。</p>
  {#if !snapshot.access?.file_snapshots}<p class="result-empty">文件检查未启用或当前账号没有文件读取权限。</p>
  {:else if !target}<p class="result-empty">本轮结束且任务访问状态有效后，可读取文件快照。</p>{/if}
  {#if view.busy}<p class="secondary-copy" role="status">正在读取并核对文件快照…</p>{/if}
  {#if view.problem}<p class="task-notice" role="alert">{view.problem}</p>{/if}
  {#if notice}<p class="task-notice" role="alert">{notice}</p>{/if}
  {#if view.feedback}<p class="copy-feedback" role="status">{view.feedback}</p>{/if}
  {#if view.list}
    {#if view.list.state === 'absent'}<p class="result-empty">尚无文件快照。执行端可能未配置共享文件或仍在上传；可稍后刷新。</p>
    {:else}<p class="secondary-copy">Hub 接收时间：{localTime(view.list.created_at)} · {view.list.files.length} 个可读取文件 · {view.list.unavailable.length} 个不可用文件</p>{/if}
    {#if view.list.files.length}<ul>{#each view.list.files as file (file.snapshot_id)}<li>
      <div class="result-row-heading"><strong><code>{file.path}</code></strong><span>{file.size_bytes} 字节 · {file.content_kind === 'text' ? '文本' : '二进制'}</span></div>
      <p class="secondary-copy">SHA-256：<code>{file.sha256}</code></p>
      <div class="file-actions"><IrisButton size="sm" variant="outline" aria-label={`检查 ${file.path}`} disabled={!target || view.busy} onclick={() => void reader.read(file)}>检查内容</IrisButton>
        <IrisButton size="sm" variant="outline" aria-label={`下载 ${file.path}`} disabled={!target || view.busy} onclick={() => void reader.read(file, true)}>下载文件</IrisButton></div>
    </li>{/each}</ul>{/if}
    {#if view.list.unavailable.length}<ul class="file-unavailable">{#each view.list.unavailable as file}<li><code>{file.path}</code>：{unavailableLabels[file.code]}</li>{/each}</ul>{/if}
  {/if}
  {#if view.preview}<section class="file-preview" aria-label="文件内容预览"><h5>{view.preview.file.path}</h5>
    <p class="secondary-copy">SHA-256 与字节数已核对。目标验收仍未验证。</p>
    {#if view.preview.text === null}<p class="result-empty">二进制文件仅提供原始字节下载。</p>
    {:else if view.preview.text === ''}<p class="result-empty">这是一个空文件（0 字节）。</p>
    {:else}<pre>{view.preview.text}</pre><p class="secondary-copy">格式控制字符与反斜杠以转义形式显示；下载保留原始字节。</p>{/if}
  </section>{/if}
</section>
