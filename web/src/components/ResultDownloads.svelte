<script module lang="ts">
  import type { ExportTarget } from '../lib/task-export'
  export type ResultFormat = 'json' | 'text'
  export type DownloadTurn = (target: ExportTarget, format: ResultFormat) => { text: string; filename: string; mime: string }
</script>

<script lang="ts">
  import { onDestroy, onMount } from 'svelte'
  import { IrisButton } from '../lib/iris'
  import { exportTarget } from '../lib/task-export'
  import type { Tasks } from '../lib/task-state'
  let { snapshot, download }: { snapshot: Tasks; download: DownloadTurn } = $props()
  const target = $derived(exportTarget(snapshot))
  const scope = $derived(JSON.stringify([snapshot.contextEpoch, snapshot.access?.context,
    snapshot.selectedSession, snapshot.detail?.instance_id, snapshot.selectedTurn]))
  let feedback = $state(''), problem = $state(''), identity = '', objectUrl = ''
  let revokeTimer: ReturnType<typeof setTimeout> | undefined
  function release() {
    if (revokeTimer) clearTimeout(revokeTimer)
    revokeTimer = undefined
    if (objectUrl) URL.revokeObjectURL(objectUrl)
    objectUrl = ''
  }
  function clear() { release(); feedback = ''; problem = '' }
  $effect(() => {
    if (identity !== scope || !target) { identity = scope; clear() }
  })
  onMount(() => {
    const visibility = () => { if (document.hidden) clear() }
    window.addEventListener('pagehide', clear); window.addEventListener('hashchange', clear)
    document.addEventListener('visibilitychange', visibility)
    return () => {
      window.removeEventListener('pagehide', clear); window.removeEventListener('hashchange', clear)
      document.removeEventListener('visibilitychange', visibility)
    }
  })
  onDestroy(release)
  function save(format: ResultFormat) {
    const selected = target, label = format === 'text' ? '本轮可读报告（TXT）' : '当前轮次记录（JSON）'
    clear()
    try {
      if (!selected) throw Error('请先刷新并核对当前任务。')
      const output = download(selected, format)
      objectUrl = URL.createObjectURL(new Blob([output.text], { type: output.mime }))
      const anchor = document.createElement('a')
      anchor.href = objectUrl; anchor.download = output.filename
      document.body.append(anchor)
      try { anchor.click() } finally { anchor.remove() }
      revokeTimer = setTimeout(release, 1000)
      feedback = `已发起${label}下载。内容限于当前已读取窗口，请核对浏览器的保存结果。`
    } catch (error) { release(); problem = `${label}未生成：${error instanceof Error ? error.message : '请刷新后重试。'}` }
  }
</script>

<div class="result-downloads">
  <div class="result-download-actions" role="group" aria-label="本轮结果下载">
    <IrisButton type="button" size="sm" variant="outline" disabled={!target} onclick={() => save('text')}>下载本轮可读报告</IrisButton>
    <IrisButton type="button" size="sm" variant="outline" disabled={!target} onclick={() => save('json')}>下载当前轮次记录</IrisButton>
  </div>
  {#if feedback}<p class="copy-feedback" role="status">{feedback}</p>{/if}
  {#if problem}<p class="task-notice" role="alert">{problem}</p>{/if}
</div>
