<script lang="ts">
  import { onDestroy, onMount, tick } from 'svelte'
  import { IrisButton } from '../lib/iris'
  import { MAX_DRAFT_BYTES } from '../lib/drafts'
  import { preparePromptReuse, promptReuseAvailable, promptReuseMatches, type PromptReference } from '../lib/prompt-reuse'
  import { taskRouteHash } from '../lib/task-route'
  import type { Tasks } from '../lib/task-state'
  import '../prompt-reuse.css'
  let { snapshot, append, pauseFollowing }: { snapshot: Tasks; append: (reference: PromptReference, text: string) => string;
    pauseFollowing: () => void } = $props()
  let preview = $state<{ reference: PromptReference; text: string } | null>(null)
  let problem = $state(''), feedback = $state(''), applying = $state(false), visible = $state(true)
  let editor = $state<HTMLTextAreaElement>(), identity = '', navigation = 0, alive = true
  const encoder = new TextEncoder()
  const problemId = $props.id()
  const available = $derived(promptReuseAvailable(snapshot))
  const scope = $derived(JSON.stringify([snapshot.contextEpoch, snapshot.access?.context, snapshot.selectedSession,
    snapshot.detail?.instance_id, snapshot.selectedTurn, snapshot.currentTurn?.turn_id]))
  function clear() { preview = null; problem = ''; feedback = ''; navigation++ }
  function usable(reference: PromptReference) {
    const expected = taskRouteHash({ session: reference.target.session,
      turn: snapshot.turnMode === 'fixed' ? reference.turn : '' })
    return alive && visible && !document.hidden && scope === identity && location.hash === expected &&
      promptReuseAvailable(snapshot) && promptReuseMatches(snapshot, reference)
  }
  $effect(() => {
    const current = preview, currentScope = scope, currentVisible = visible
    const matches = current ? promptReuseMatches(snapshot, current.reference) : true
    const invalid = snapshot.pending !== null || snapshot.outcome === 'unknown' || snapshot.detail?.status === 'closed'
    if (identity !== currentScope || !currentVisible || !matches || invalid) { identity = currentScope; clear() }
  })
  onMount(() => {
    const visibility = () => { visible = !document.hidden; if (!visible) clear() }
    const hide = () => { visible = false; clear() }
    visibility()
    window.addEventListener('pagehide', hide); window.addEventListener('pageshow', visibility)
    window.addEventListener('hashchange', clear); document.addEventListener('visibilitychange', visibility)
    return () => {
      window.removeEventListener('pagehide', hide); window.removeEventListener('pageshow', visibility)
      window.removeEventListener('hashchange', clear); document.removeEventListener('visibilitychange', visibility)
    }
  })
  onDestroy(() => { alive = false; clear() })
  async function begin() {
    clear()
    try {
      const prepared = preparePromptReuse(snapshot)
      if (!usable(prepared.reference)) throw Error('任务或页面目标已变化，请重新核对当前指令。')
      preview = prepared; pauseFollowing()
      const request = ++navigation
      await tick()
      if (request === navigation && preview?.reference === prepared.reference && usable(prepared.reference)) editor?.focus()
    } catch (error) { if (alive) problem = error instanceof Error ? error.message : '当前指令不能复用。' }
  }
  function edit(event: Event) {
    const field = event.currentTarget as HTMLTextAreaElement
    if (!preview) return
    if (encoder.encode(field.value).length > MAX_DRAFT_BYTES) {
      field.value = preview.text; problem = '复用预览超过 65536 字节，本次修改未接受。请精简后重试。'; return
    }
    preview = { reference: preview.reference, text: field.value }; problem = ''
  }
  async function apply() {
    const selected = preview
    if (!selected || applying || !available) return
    applying = true
    try {
      if (!usable(selected.reference)) throw Error('任务或页面目标已变化，请重新核对当前指令。')
      const error = append(selected.reference, selected.text)
      if (error) { problem = error; return }
      preview = null; problem = ''; feedback = '指令已加入当前草稿；尚未发送。'
      const request = ++navigation
      await tick()
      if (request === navigation && usable(selected.reference)) document.getElementById('task-prompt')?.focus()
    } catch (error) { if (alive) problem = error instanceof Error ? error.message : '未能加入草稿，原稿与预览已保留。' }
    finally { if (alive) applying = false }
  }
</script>

<div class="prompt-reuse">
  <IrisButton type="button" size="sm" variant="outline" disabled={!available || !visible || applying || !!preview} onclick={() => void begin()}>复用本轮指令</IrisButton>
  {#if snapshot.currentTurn?.prompt_truncated}<p class="secondary-copy">本轮指令显示已截断，不能复用不完整的指令。</p>
  {:else if !available}<p class="secondary-copy">当前不能复用指令，请先核对权限、同步状态和未决请求。</p>{/if}
  {#if preview}<section class="prompt-reuse-preview" aria-label="复用指令预览">
    <h4>复用指令预览</h4>
    <p class="secondary-copy">来源会话：{preview.reference.target.session}<br />来源轮次：{preview.reference.turn}</p>
    <p class="secondary-copy">以下来自当前可见的脱敏记录，不是原始提交；不会恢复被隐藏的内容。</p>
    <label>待加入的指令<textarea bind:this={editor} value={preview.text} oninput={edit} rows="6" maxlength={MAX_DRAFT_BYTES} disabled={applying} aria-describedby={problem ? problemId : undefined}></textarea></label>
    <p class="secondary-copy">{encoder.encode(preview.text).length} / 65536 字节。加入会保留点击时的完整原稿，追加后仍需核对并手动发送；单次发送仍限 16384 字节。</p>
    {#if problem}<p id={problemId} class="task-notice" role="alert">{problem}</p>{/if}
    <div class="prompt-reuse-actions"><IrisButton type="button" size="sm" disabled={!available || !visible || applying} onclick={() => void apply()}>加入当前草稿</IrisButton>
      <IrisButton type="button" size="sm" variant="outline" onclick={clear}>放弃复用</IrisButton></div>
  </section>{/if}
  {#if problem && !preview}<p class="task-notice" role="alert">{problem}</p>{/if}
  {#if feedback}<p class="copy-feedback" role="status">{feedback}</p>{/if}
</div>
