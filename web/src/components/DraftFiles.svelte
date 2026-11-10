<script lang="ts">
  import { onDestroy, onMount, tick } from 'svelte'
  import { IrisButton } from '../lib/iris'
  import { displayText } from '../lib/api'
  import { draftAvailable, draftBackupAvailable, draftMatches, draftTarget, type DraftTarget } from '../lib/draft-file'
  import { readDraftText } from '../lib/draft-file-reader'
  import type { Tasks } from '../lib/task-state'
  import '../draft-files.css'
  let { snapshot, prompt, backup, append }: { snapshot: Tasks; prompt: string;
    backup: (target: DraftTarget) => { text: string; filename: string };
    append: (target: DraftTarget, text: string) => string } = $props()
  let preview = $state<{ target: DraftTarget; text: string; name: string } | null>(null)
  let reading = $state(false), problem = $state(''), feedback = $state(''), applying = $state(false)
  let input = $state<HTMLInputElement>(), active: AbortController | null = null, alive = true, key = '', downloadUrl = ''
  const target = $derived(draftTarget(snapshot))
  const scope = $derived(target ? JSON.stringify([target.epoch, target.context, target.session, target.instance]) : '')
  const available = $derived(draftAvailable(snapshot))
  const backupAvailable = $derived(draftBackupAvailable(snapshot))
  function releaseDownload() { if (downloadUrl) URL.revokeObjectURL(downloadUrl); downloadUrl = '' }
  function clear() {
    active?.abort(); active = null; reading = false; preview = null; problem = ''; feedback = ''
    if (input) input.value = ''
    releaseDownload()
  }
  $effect(() => {
    if (key !== scope) { key = scope; clear() }
    else if ((snapshot.outcome === 'unknown' || snapshot.detail?.status === 'closed') && (preview || reading)) clear()
  })
  onMount(() => { window.addEventListener('pagehide', clear); return () => window.removeEventListener('pagehide', clear) })
  onDestroy(() => { alive = false; clear() })
  async function choose(event: Event) {
    const field = event.currentTarget as HTMLInputElement, file = field.files?.[0]
    field.value = ''; clear()
    if (!file || !available || !target) return
    const selected = { ...target }, controller = new AbortController()
    active = controller; reading = true
    const live = () => alive && active === controller && draftMatches(snapshot, selected) && snapshot.outcome !== 'unknown'
    try {
      const text = await readDraftText(file, controller.signal)
      if (live() && snapshot.detail?.status !== 'closed') preview = { target: selected, text, name: displayText(file.name, '草稿文本') }
    } catch (error) { if (live()) problem = error instanceof Error ? error.message : '未能读取草稿文本，当前草稿未更改。' }
    finally { if (active === controller) { active = null; reading = false } }
  }
  function save() {
    if (!target || !backupAvailable || !prompt.trim()) return
    problem = ''; feedback = ''; releaseDownload()
    try {
      const file = backup(target)
      downloadUrl = URL.createObjectURL(new Blob([file.text], { type: 'text/plain;charset=utf-8' }))
      const link = document.createElement('a')
      link.href = downloadUrl; link.download = file.filename; link.click()
      feedback = '已发起草稿文本下载，请核对浏览器中的保存结果。'
    } catch (error) { problem = error instanceof Error ? error.message : '未能保存草稿文本，当前草稿未更改。' }
  }
  async function apply() {
    if (!preview || applying || !available) return
    applying = true
    const selected = preview
    try {
      const error = append(selected.target, selected.text)
      if (error) { problem = error; return }
      preview = null; problem = ''; feedback = '草稿文本已加入当前草稿；尚未发送。'
      await tick()
      if (alive && draftMatches(snapshot, selected.target)) document.getElementById('task-prompt')?.focus()
    } catch { if (alive) problem = '未能加入草稿，原稿已保留。请核对当前任务后重试。' }
    finally { if (alive) applying = false }
  }
</script>

<details class="draft-files">
  <summary>草稿文件</summary>
  <p class="secondary-copy">离开前可将编辑框文本保存为 .txt；回来后手动选择文件并核对，再加入当前草稿。文件包含原文，可能包含你写入的敏感内容。</p>
  <div class="draft-file-actions"><IrisButton type="button" size="sm" variant="outline" disabled={!backupAvailable || !prompt.trim()} onclick={save}>保存草稿文本</IrisButton>
    <label class="task-field">选择草稿文本文件<input bind:this={input} type="file" accept=".txt,text/plain" aria-label="选择草稿文本文件" disabled={!available || reading || applying} onchange={(event) => void choose(event)} /></label></div>
  <p class="secondary-copy">仅在本页读取 UTF-8 文本，最多 65536 字节，不上传。文件不证明指令尚未执行，也不会恢复授权或请求状态。</p>
  {#if snapshot.detail?.status === 'closed'}<p class="secondary-copy">会话已关闭，仍可保存已有草稿文本；不能向此会话导入或发送。</p>
  {:else if !available}<p class="secondary-copy">当前不能导入草稿，请先核对权限、同步状态和未决请求。</p>{/if}
  {#if reading}<p role="status" class="secondary-copy">正在读取草稿文本…</p><IrisButton type="button" size="sm" variant="outline" onclick={clear}>取消读取</IrisButton>{/if}
  {#if preview}<section class="draft-file-preview" aria-label="导入草稿预览"><h4>核对后加入当前任务</h4>
    <p class="secondary-copy">{preview.name} · {new TextEncoder().encode(preview.text).length} 字节<br />目标：{snapshot.detail?.name || preview.target.session}</p>
    <pre aria-label="待加入的草稿文本">{preview.text}</pre>
    <p class="secondary-copy">加入会保留点击时的完整原稿。请检查文本与目标；内容不会自动发送，单次发送仍限 16384 字节。</p>
    <div class="draft-file-actions"><IrisButton type="button" size="sm" disabled={!available || applying} onclick={() => void apply()}>加入当前草稿</IrisButton>
      <IrisButton type="button" size="sm" variant="outline" onclick={clear}>放弃导入</IrisButton></div>
  </section>{/if}
  {#if problem}<p class="task-notice" role="alert">{problem}</p>{/if}
  {#if feedback}<p class="copy-feedback" role="status">{feedback}</p>{/if}
</details>
