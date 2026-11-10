<script lang="ts">
  import { IrisButton, IrisIcon } from '../lib/iris'
  import { sessionLabel, heartbeat, localTime } from '../lib/presentation'
  import type { Instance } from '../lib/api'
  import type { CatalogQuery, CatalogEntry } from '../lib/task-catalog'
  import '../catalog.css'
  let { rows, instances, selected, query, page, hasNext, hasPrevious, readable, disabled, refreshing, chooseQuery, navigate, open, create }: {
    rows: CatalogEntry[]; instances: Instance[]; selected: string; query: CatalogQuery;
    page: number; hasNext: boolean; hasPrevious: boolean; readable: boolean; disabled: boolean; refreshing: boolean;
    chooseQuery: (query: CatalogQuery) => void; navigate: (direction: 'first' | 'previous' | 'next') => void;
    open: (id: string) => void; create: () => void
  } = $props()
  const filters: { id: CatalogQuery['state']; label: string }[] = [
    { id: 'all', label: '全部' }, { id: 'active', label: '进行中' }, { id: 'attention', label: '需处理' },
    { id: 'completed', label: '已完成' }, { id: 'idle', label: '空闲' }, { id: 'closed', label: '已关闭' },
  ]
  let search = $state(''), appliedSearch = ''
  const unavailable = $derived(disabled || !readable)
  const entries = $derived(readable ? rows : [])
  const filtered = $derived(!!query.q || query.state !== 'all' || !!query.instance_id)
  const emptyTitle = $derived(!readable ? '任务暂不可读取' : disabled || refreshing ? '正在读取任务…' : filtered ? '没有匹配的任务' : page > 1 ? '本页已没有任务' : '开始第一个项目任务')
  $effect(() => {
    if (!readable) { search = ''; appliedSearch = query.q }
    else if (query.q !== appliedSearch) { appliedSearch = query.q; search = query.q }
  })
  function submit(event: SubmitEvent) {
    event.preventDefault()
    if (!unavailable) chooseQuery({ ...query, q: search })
  }
  function recent(state: string) {
    return ({ completed: '已完成', failed: '失败', cancelled: '已取消', interrupted: '已中断', lost: '失联',
      running: '执行中', queued: '排队中', cancel_requested: '已请求取消' } as Record<string, string>)[state] ?? '结果未知'
  }
</script>

<aside class="task-list-panel catalog-panel" aria-label="项目与任务">
  <div class="task-list-heading"><h2>项目任务</h2><IrisButton size="sm" onclick={create} disabled={unavailable} aria-label="新建任务"><IrisIcon name="plus" size={16} />新建任务</IrisButton></div>
  <label class="task-field" for="project-filter">项目实例<select id="project-filter" aria-label="项目实例" value={query.instance_id} onchange={(event) => chooseQuery({ ...query, instance_id: event.currentTarget.value })} disabled={unavailable}>
    <option value="">全部授权项目</option>{#each instances as row (row.instance_id)}<option value={row.instance_id}>{row.name || row.project_id || row.instance_id} · {heartbeat(row.online)}</option>{/each}
  </select></label>
  <form class="catalog-search-form" onsubmit={submit} role="search" aria-label="任务搜索">
    <label class="task-search"><IrisIcon name="search" size={16} /><input type="search" bind:value={search} aria-label="搜索任务" placeholder="名称、项目或会话 ID" maxlength="128" disabled={unavailable} /></label>
    <IrisButton type="submit" size="sm" variant="outline" disabled={unavailable}>搜索任务</IrisButton>
    {#if readable && search.trim() !== query.q}<p class="catalog-search-hint" role="status">输入已修改，提交搜索后更新任务列表。</p>{/if}
  </form>
  <div class="task-filters catalog-filters" aria-label="任务状态筛选">
    {#each filters as item}
      <button class:chosen={query.state === item.id} aria-pressed={query.state === item.id} disabled={unavailable} onclick={() => chooseQuery({ ...query, state: item.id })}>{item.label}</button>
    {/each}
  </div>
  <p class="catalog-scope">搜索与筛选覆盖当前账号获授权的任务。</p>
  <ul class="task-session-list" aria-label="Agent 任务列表" aria-busy={disabled || refreshing}>
    {#each entries as row (row.session_id)}
      <li><button class:selected={selected === row.session_id} aria-current={selected === row.session_id ? 'true' : undefined} aria-label={row.name || row.session_id}
        disabled={unavailable} onclick={() => open(row.session_id)}>
        <span class="session-row-top"><strong>{row.name || row.session_id}</strong><span class="session-status" data-state={row.status}>{sessionLabel(row.status)}</span></span>
        <span class="record-meta">{row.project_id || row.instance_id}</span>
        <span class="catalog-outcome" data-state={row.latest_turn?.state}>最近执行：{row.latest_turn ? recent(row.latest_turn.state) : '尚未执行'}</span>
        {#if row.latest_turn}<span class="record-meta catalog-updated">{localTime(row.latest_turn.updated_at)}</span>{/if}
        <span class="record-meta task-list-id">{row.session_id}</span>
      </button></li>
    {:else}<li class="task-list-empty"><IrisIcon name="inbox" size={24} /><strong>{emptyTitle}</strong>
      <p>{!readable ? '登录并获得项目任务权限后，在这里开始工作。' : disabled || refreshing ? '正在核对当前查询和任务状态。' : filtered ? '试试其他关键词、项目或状态。' : page > 1 ? '任务状态可能已变化，可以返回上一页或首页。' : '选择在线项目，创建任务后即可发送目标。'}</p></li>{/each}
  </ul>
  {#if readable && !disabled && selected && !rows.some((row) => row.session_id === selected)}
    <div class="catalog-open-task"><p>当前打开的任务不在本页或筛选结果中，详情仍保留。</p>
      <IrisButton size="sm" variant="outline" onclick={() => chooseQuery({ instance_id: '', state: 'all', q: selected })}>定位当前任务</IrisButton></div>
  {/if}
  <nav class="catalog-pagination" aria-label="任务列表分页">
    <p class="task-list-footnote" aria-live="polite">第 {page} 页 · 本页 {entries.length} 条{refreshing ? ' · 更新中' : ''}</p>
    <div class="catalog-page-actions">
      <IrisButton size="sm" variant="outline" disabled={unavailable || page <= 1} onclick={() => navigate('first')}>首页</IrisButton>
      <IrisButton size="sm" variant="outline" disabled={unavailable || !hasPrevious} onclick={() => navigate('previous')}>上一页</IrisButton>
      <IrisButton size="sm" variant="outline" disabled={unavailable || !hasNext} onclick={() => navigate('next')}>下一页</IrisButton>
    </div>
  </nav>
</aside>
