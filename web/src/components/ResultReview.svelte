<script lang="ts">
  import { onDestroy } from 'svelte'
  import { IrisButton } from '../lib/iris'
  import { reviewRecords, reviewPage, reviewStatusLabel, reviewCategoryLabel } from '../lib/result-review'
  import { prepareReference, referenceMatches, type DraftReference } from '../lib/result-followup'
  import type { Tasks } from '../lib/task-state'
  import ResultRecord from './ResultRecord.svelte'
  import '../result-review.css'
  let { snapshot, append }: { snapshot: Tasks; append: (reference: DraftReference, text: string) => Promise<string> } = $props()
  type Filter = 'attention' | 'all' | 'failure' | 'unknown' | 'running' | 'success' | 'recorded'
  const filters: { value: Filter; title: string }[] = [{ value: 'attention', title: '需要核对' }, { value: 'all', title: '全部记录' },
    { value: 'failure', title: '报告异常' }, { value: 'unknown', title: '结果未知' }, { value: 'running', title: '执行中' },
    { value: 'success', title: '报告成功' }, { value: 'recorded', title: '已记录元数据' }]
  const view = $derived(reviewRecords(snapshot))
  let q = $state(''), filter = $state<Filter>('attention'), applied = $state({ q: '', filter: 'attention' as Filter })
  let page = $state(1), selected = $state(''), problem = $state(''), identity = ''
  let reference = $state<DraftReference | null>(null), edited = $state(''), draftProblem = $state('')
  let applying = $state(false), alive = true
  onDestroy(() => { alive = false })
  const window = $derived(reviewPage(view?.records ?? [], applied.q, applied.filter, page))
  const record = $derived(view?.records.find((item) => item.key === selected) ?? null)
  const scope = $derived(`${snapshot.contextEpoch}/${snapshot.access?.context}/${snapshot.selectedSession}/${snapshot.selectedTurn}`)
  const readLocked = $derived(!view || snapshot.busy || snapshot.refreshing || snapshot.syncStopped || snapshot.readAt === null)
  const locked = $derived(readLocked ||
    snapshot.outcome === 'unknown' || !snapshot.access?.scopes.includes('agent.sessions:write') || snapshot.detail?.status === 'closed')
  const draftBytes = $derived(new TextEncoder().encode(edited).length)
  $effect(() => { if (page !== window.page) page = window.page })
  $effect(() => {
    if (identity !== scope || !view) {
      identity = scope; q = ''; filter = 'attention'; applied = { q: '', filter: 'attention' }; page = 1
      selected = ''; problem = ''; reference = null; edited = ''; draftProblem = ''
    }
  })
  $effect(() => {
    if (selected && !record) { selected = ''; reference = null; edited = ''; draftProblem = '原记录已不在当前读取窗口，请重新选择。' }
    if (reference && !referenceMatches(snapshot, reference)) {
      reference = null; edited = ''; draftProblem = '引用依据已变化，请重新核对记录后起草。'
    }
  })
  function search() {
    try {
      reviewPage(view?.records ?? [], q, filter, 1)
      applied = { q, filter }; page = 1; problem = ''
    } catch { problem = '搜索条件无效，请使用最多 128 个可见字符。原有结果尚未应用新条件。' }
  }
  function choose(key: string) { selected = key; reference = null; edited = ''; draftProblem = '' }
  function prepare() {
    try {
      const result = prepareReference(snapshot, selected)
      reference = result.reference; edited = result.text; draftProblem = ''
    } catch (error) { reference = null; edited = ''; draftProblem = error instanceof Error ? error.message : '请刷新并重新核对记录。' }
  }
  async function apply() {
    if (!reference || locked || applying) return
    applying = true
    try {
      const problem = await append(reference, edited)
      if (alive && problem) draftProblem = problem
    } catch { if (alive) draftProblem = '未能追加引用，请核对现有草稿后重试。' }
    finally { if (alive) applying = false }
  }
</script>

{#if view}
  <section class="result-review" aria-label="结果记录工作区">
    <h4>定位记录，继续处理</h4>
    <p class="secondary-copy">当前已读取窗口：{view.counts.all} 条记录，其中 {view.counts.attention} 条需要核对、{view.counts.failure} 条报告异常。数量不代表完整任务统计，零条异常也不代表目标验收通过。</p>
    <form class="review-search" onsubmit={(event) => { event.preventDefault(); search() }}>
      <label class="task-field">搜索结果记录<input type="search" aria-label="搜索结果记录" maxlength="128" placeholder="命令、原因、输出或记录 ID" bind:value={q} /></label>
      <label class="task-field">结果状态筛选<select aria-label="结果状态筛选" bind:value={filter}>{#each filters as option}<option value={option.value}>{option.title}</option>{/each}</select></label>
      <IrisButton type="submit" size="sm" variant="outline">搜索结果记录</IrisButton>
    </form>
    <p class="secondary-copy">仅搜索当前已读取的命令、验证、门禁、产物和异常记录。继续读取事件可能改变结果；回复文字不会成为验证记录。</p>
    {#if problem}<p class="task-notice" role="alert">{problem}</p>{/if}
    <p class="secondary-copy">匹配 {window.total} 条 · 第 {window.page} / {window.pages} 页 · 每页最多 20 条</p>
    <ul class="review-rows" aria-label="结果记录">{#each window.rows as item (item.key)}<li><button type="button" aria-label={`检查结果 ${item.key}`} aria-pressed={selected === item.key} onclick={() => choose(item.key)}>
      <span class="review-row-top"><strong>{item.title}</strong><span>{reviewStatusLabel(item.status)}</span></span>
      <span class="record-meta">{reviewCategoryLabel(item.category)} · {item.sources.length ? `${item.sources.length} 条来源 · 最近 #${item.sources.at(-1)?.cursor}` : '当前轮次状态'}{item.partial ? ' · 记录不完整' : ''}</span>
    </button></li>{:else}<li class="result-empty">当前已读取窗口没有符合条件的记录。可以切换“全部记录”或继续读取事件；这不代表没有问题或已经验收通过。</li>{/each}</ul>
    <div class="review-page-actions"><IrisButton size="sm" variant="outline" disabled={window.page <= 1} onclick={() => page = window.page - 1}>上一页结果</IrisButton>
      <IrisButton size="sm" variant="outline" disabled={window.page >= window.pages} onclick={() => page = window.page + 1}>下一页结果</IrisButton></div>
    {#if record}
      <ResultRecord {record} {scope} disabled={readLocked} />
      <IrisButton size="sm" variant="outline" disabled={locked} onclick={prepare}>起草后续指令</IrisButton>
      {#if locked}<p class="secondary-copy">当前不能起草或追加引用。请先核对待处理请求、访问权限及会话状态。</p>{/if}
    {/if}
    {#if reference}
      <div class="review-draft"><label class="task-field">后续指令草稿<textarea aria-label="后续指令草稿" bind:value={edited} rows="8" maxlength="16384"></textarea></label>
        <p class="secondary-copy">这里编辑的只是新增段落。追加会保留当前已有草稿；引用中的执行端文本仍需核对。检查目标、范围和验收要求后，再使用原发送按钮提交。</p>
        <div class="review-page-actions"><span class="secondary-copy">新增段落 {draftBytes} / 16384 字节</span><IrisButton size="sm" disabled={locked || applying || !edited.trim()} onclick={() => void apply()}>追加到当前草稿</IrisButton>
          <IrisButton size="sm" variant="outline" onclick={() => { reference = null; edited = ''; draftProblem = '' }}>放弃引用草稿</IrisButton></div>
      </div>
    {/if}
    {#if draftProblem}<p class="task-notice" role="alert">{draftProblem}</p>{/if}
  </section>
{/if}
