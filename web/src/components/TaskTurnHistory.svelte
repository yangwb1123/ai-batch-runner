<script lang="ts">
  import { IrisButton } from '../lib/iris'
  import { localTime, sessionLabel } from '../lib/presentation'
  import { TURN_STATES } from '../lib/task-values'
  import { defaultTurnQuery, type TurnQuery } from '../lib/turn-query'
  import type { Tasks } from '../lib/task-state'
  import '../turn-history.css'
  let { snapshot, search, navigate, select }: { snapshot: Tasks; search: (query: TurnQuery) => Promise<void>;
    navigate: (direction: 'first' | 'previous' | 'next') => Promise<void>; select: (id: string) => Promise<void> } = $props()
  let q = $state(''), filterState = $state(''), identity = ''
  const locked = $derived(snapshot.busy || !snapshot.access?.task_access)
  const filtered = $derived(!!snapshot.turnQuery.q || snapshot.turnQuery.state !== 'all')
  $effect(() => {
    const key = `${snapshot.contextEpoch}/${snapshot.selectedSession}`
    if (key !== identity) { identity = key; q = snapshot.turnQuery.q; filterState = snapshot.turnQuery.state === 'all' ? '' : snapshot.turnQuery.state }
  })
  async function latest() { q = ''; filterState = ''; await search(defaultTurnQuery()) }
</script>

<div class="task-actions"><h3>每次指令与执行结果</h3><IrisButton size="sm" variant="outline" disabled={locked} onclick={() => void latest()}>最新轮次</IrisButton></div>
<form class="turn-search-form" onsubmit={(event) => { event.preventDefault(); void search({ q, state: filterState || 'all' }) }}>
  <label class="task-field">搜索轮次<input type="search" aria-label="搜索轮次" bind:value={q} maxlength="128" placeholder="可见指令内容或轮次 ID" disabled={locked} /></label>
  <label class="task-field">轮次状态<select aria-label="轮次状态" bind:value={filterState} disabled={locked}><option value="">全部状态</option>
    {#each TURN_STATES as value}<option value={value}>{sessionLabel(value)}</option>{/each}</select></label>
  <IrisButton type="submit" size="sm" variant="outline" disabled={locked}>搜索轮次</IrisButton>
</form>
<p class="secondary-copy">在当前任务的已授权历史中搜索脱敏后的指令或轮次 ID。搜索不执行指令；按状态筛选后的结果不代表目标验收。</p>
{#if snapshot.turnHistoryReadAt}<p class="secondary-copy">结果读取于 {localTime(snapshot.turnHistoryReadAt, true)} · 第 {snapshot.turnHistoryPage} 页 · 本页 {snapshot.turns?.data.length ?? 0} 条{filtered ? ' · 已应用搜索条件' : ''}</p>{/if}
{#if !snapshot.turns}<p class="task-list-empty">轮次记录尚未读取成功，请搜索或刷新后核对。</p>
{:else}
  <ul aria-label="轮次记录">{#each snapshot.turns.data as turn (turn.turn_id)}<li><button aria-label={`查看轮次 ${turn.turn_id}`} disabled={locked} onclick={() => void select(turn.turn_id)}>
    <span class="session-row-top"><strong>{turn.prompt.slice(0, 100) || '未提供指令摘要'}</strong><span class="session-status" data-state={turn.state}>{sessionLabel(turn.state)}</span></span>
    <span class="record-meta">{localTime(turn.created_at)} · {turn.turn_id}</span>{#if turn.prompt_truncated}<span class="record-meta">指令记录已截断</span>{/if}</button></li>
    {:else}<li class="task-list-empty">{filtered || snapshot.turnHistoryCursor ? '没有匹配的轮次。可以更换条件或查看最新轮次；这不代表任务没有历史记录。' : '还没有执行轮次，发送第一条指令即可开始。'}</li>{/each}</ul>
  <div class="turn-page-actions"><IrisButton size="sm" variant="outline" disabled={locked || snapshot.turnHistoryPage === 1} onclick={() => void navigate('first')}>轮次首页</IrisButton>
    <IrisButton size="sm" variant="outline" disabled={locked || !snapshot.turnHistoryTrail.length} onclick={() => void navigate('previous')}>返回上一页</IrisButton>
    <IrisButton size="sm" variant="outline" disabled={locked || !snapshot.turns.next_cursor} onclick={() => void navigate('next')}>查看更早轮次</IrisButton></div>
  {#if filtered || snapshot.turnHistoryCursor}<p class="secondary-copy">搜索与历史分页结果保留到下次手动查询。已选轮次的状态仍持续更新；最多保留最近 32 页的返回位置。</p>{/if}
{/if}
