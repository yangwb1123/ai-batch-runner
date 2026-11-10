<script lang="ts">
  import { onMount, onDestroy } from 'svelte'
  import { IrisButton } from '../lib/iris'
  import { createComparisonReader } from '../lib/file-comparison-reader'
  import { emptyComparison, comparisonJson, type Comparison } from '../lib/file-comparison'
  import { fileTarget } from '../lib/file-reader'
  import { localTime, sessionLabel } from '../lib/presentation'
  import type { Tasks } from '../lib/task-state'
  import FileDiff from './FileDiff.svelte'
  import '../file-diff.css'
  let { snapshot, fileEpoch = 0, notice = '', denied = () => {} }: { snapshot: Tasks; fileEpoch?: number; notice?: string; denied?: (problem: string) => void } = $props()
  let view = $state(emptyComparison()), objectUrl = '', revokeTimer: ReturnType<typeof setTimeout> | undefined
  let search = $state(''), searchState = $state(''), searchIdentity = ''
  function release() {
    clearTimeout(revokeTimer)
    if (objectUrl) URL.revokeObjectURL(objectUrl)
    objectUrl = ''
  }
  function save(comparison: Comparison) {
    release()
    objectUrl = URL.createObjectURL(new Blob([comparisonJson(comparison)], { type: 'application/json;charset=utf-8' }))
    const anchor = document.createElement('a')
    anchor.href = objectUrl; anchor.download = 'abr-file-comparison.json'
    document.body.append(anchor)
    try { anchor.click() } finally { anchor.remove() }
    revokeTimer = setTimeout(release, 1000)
  }
  function currentTarget() {
    const selected = fileTarget(snapshot)
    return selected ? { ...selected, key: `${selected.key}/${fileEpoch}` } : null
  }
  const reader = createComparisonReader(currentTarget, (next) => { view = next }, save, release, fetch, (problem) => denied(problem))
  const target = $derived(currentTarget())
  $effect(() => { target; reader.bind() })
  $effect(() => { if ((target?.key ?? '') !== searchIdentity) { searchIdentity = target?.key ?? ''; search = ''; searchState = '' } })
  async function resetHistory() { search = ''; searchState = ''; await reader.history() }
  onMount(() => { window.addEventListener('pagehide', reader.clear); return () => window.removeEventListener('pagehide', reader.clear) })
  onDestroy(reader.clear)
</script>

<section class="results-section file-comparison" aria-label="文件版本对比">
  <div class="result-row-heading"><h4>文件版本对比</h4>
    <IrisButton size="sm" variant="outline" disabled={!target || view.busy} onclick={() => void resetHistory()}>选择对比轮次</IrisButton></div>
  <p class="secondary-copy">选择同一任务中另一轮的快照作为基准，比较方向为「基准轮次 → 当前查看轮次」。仅核对共享内容，不能推断文件新增或删除，也不证明变更由该轮执行产生。</p>
  {#if !target}<p class="result-empty">本轮结束且文件读取权限有效后，可选择基准轮次。</p>{/if}
  {#if view.busy}<p class="secondary-copy" role="status">正在读取并核对对比内容…</p>{/if}
  {#if view.problem}<p class="task-notice" role="alert">{view.problem}</p>{/if}
  {#if notice}<p class="task-notice" role="alert">{notice}</p>{/if}
  {#if view.feedback}<p class="copy-feedback" role="status">{view.feedback}</p>{/if}
  {#if target}<form class="turn-search-form" onsubmit={(event) => { event.preventDefault(); void reader.history('reset', { q: search, state: searchState || 'all' }) }}>
    <label class="task-field">搜索基准轮次<input type="search" aria-label="搜索基准轮次" bind:value={search} maxlength="128" placeholder="可见指令内容或轮次 ID" disabled={view.busy} /></label>
    <label class="task-field">基准轮次状态<select aria-label="基准轮次状态" bind:value={searchState} disabled={view.busy}><option value="">全部已结束状态</option>
      {#each ['completed', 'failed', 'cancelled', 'interrupted', 'lost'] as state}<option value={state}>{sessionLabel(state)}</option>{/each}</select></label>
    <IrisButton size="sm" type="submit" variant="outline" disabled={view.busy}>搜索基准轮次</IrisButton>
  </form><p class="secondary-copy">搜索当前任务的脱敏指令或轮次 ID，定位更早的快照；不会改变当前轮次或草稿。</p>{/if}
  {#if view.history}
    <label class="comparison-choice">基准轮次
      <select aria-label="基准轮次" value={view.baseline?.turn_id ?? ''} disabled={view.busy || !target}
        onchange={(event) => void reader.choose(event.currentTarget.value)}>
        <option value="">请选择一轮</option>
        {#each view.history as turn (turn.turn_id)}<option value={turn.turn_id}>{localTime(turn.created_at)} · {sessionLabel(turn.state)} · {turn.turn_id}</option>{/each}
      </select>
    </label>
    {#if !view.history.length}<p class="result-empty">本页没有可选择的其他已结束轮次。</p>{/if}
    <div class="file-actions comparison-pagination">
      <IrisButton size="sm" variant="outline" disabled={view.busy || view.page === 0} onclick={() => void reader.history('previous')}>返回上一页</IrisButton>
      <span class="secondary-copy">第 {view.page + 1} 页</span>
      <IrisButton size="sm" variant="outline" disabled={view.busy || !view.next || view.page >= 31} onclick={() => void reader.history('next')}>较早一页</IrisButton>
    </div>
    {#if view.query.q || view.query.state !== 'all'}<p class="secondary-copy">已应用基准轮次搜索条件。只有其他已结束轮次可作为基准。</p>{/if}
    {#if view.page >= 31 && view.next}<p class="secondary-copy">本次搜索最多读取 32 页、640 轮候选。请缩小搜索条件以定位更早的基准；搜索仍受服务端读取预算限制。</p>{/if}
  {/if}
  {#if view.baseline && view.sets}
    <dl class="comparison-binding"><dt>基准轮次</dt><dd><code>{view.baseline.turn_id}</code></dd><dt>当前查看轮次</dt><dd><code>{target?.turn}</code></dd></dl>
    <p class="secondary-copy">Hub 接收时间：基准 {localTime(view.sets.before.created_at)} → 当前 {localTime(view.sets.after.created_at)}。不同文件并非原子采集。</p>
    {#if !view.pairs.length}<p class="result-empty">双方均无可比较的共享路径。这不代表工作区为空或没有变化。</p>{/if}
    {#if view.pairs.length}<ul>{#each view.pairs as pair (pair.path)}<li>
      <div class="result-row-heading"><strong><code>{pair.path}</code></strong>
        <IrisButton size="sm" variant="outline" aria-label={`比较 ${pair.path}`} disabled={view.busy || !target || !pair.before || !pair.after} onclick={() => void reader.compare(pair.path)}>比较内容</IrisButton></div>
      <p class="secondary-copy">基准：{pair.before ? `${pair.before.size_bytes} 字节 · ${pair.before.content_kind === 'text' ? '文本' : '二进制'}` : pair.beforeReason}；当前：{pair.after ? `${pair.after.size_bytes} 字节 · ${pair.after.content_kind === 'text' ? '文本' : '二进制'}` : pair.afterReason}</p>
      {#if !pair.before || !pair.after}<p class="result-empty">缺少双侧可读取快照，无法比较。</p>{/if}
    </li>{/each}</ul>{/if}
  {/if}
  {#if view.comparison}<FileDiff comparison={view.comparison} disabled={!target || view.busy} download={() => void reader.compare(view.comparison!.path, true)} />{/if}
</section>
