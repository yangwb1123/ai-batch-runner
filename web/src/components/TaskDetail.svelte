<script lang="ts">
  import { tick } from 'svelte'
  import { IrisButton, IrisIcon } from '../lib/iris'
  import { sessionLabel } from '../lib/presentation'
  import type { Tasks } from '../lib/tasks'
  import type { ActionKind } from '../lib/task-models'
  import TaskComposer from './TaskComposer.svelte'
  import TaskConversation from './TaskConversation.svelte'
  import TaskResults from './TaskResults.svelte'
  import TaskTurnHistory from './TaskTurnHistory.svelte'
  import TaskTurnMode from './TaskTurnMode.svelte'
  import TaskRecap from './TaskRecap.svelte'
  import PromptReuse from './PromptReuse.svelte'
  import { taskRouteUrl, taskRouteHash } from '../lib/task-route'
  import { resultContext } from '../lib/task-results'
  import type { TurnQuery } from '../lib/turn-query'
  import type { DraftReference } from '../lib/result-followup'
  import type { DraftTarget } from '../lib/draft-file'
  import type { DownloadTurn } from './ResultDownloads.svelte'
  import type { PromptReference } from '../lib/prompt-reuse'
  const tabs = [{ id: 'conversation', label: '对话' }, { id: 'results', label: '结果检查' }, { id: 'recap', label: '多轮回顾' }, { id: 'turns', label: '轮次记录' }, { id: 'events', label: '执行事件' }]
  let { snapshot, prompt, write, cancel, close, change, send, act, selectTurn, turnSearch, turnNavigate, recapRead, loadEvents, appendReview, backupDraft, appendDraftFile, downloadTurn, reusePrompt }: {
    snapshot: Tasks; prompt: string; write: boolean; cancel: boolean; close: boolean;
    change: (text: string) => boolean; send: () => Promise<void>;
    act: (kind: ActionKind, target: string, text?: string) => Promise<void>;
    selectTurn: (id: string) => Promise<void>; turnSearch: (query: TurnQuery) => Promise<void>;
    turnNavigate: (direction: 'first' | 'previous' | 'next') => Promise<void>; loadEvents: () => Promise<void>
    recapRead: (direction: 'latest' | 'older' | 'newer' | 'refresh') => Promise<void>
    appendReview: (reference: DraftReference, text: string) => string
    backupDraft: (target: DraftTarget) => { text: string; filename: string }
    appendDraftFile: (target: DraftTarget, text: string) => string
    downloadTurn: DownloadTurn
    reusePrompt: (reference: PromptReference, text: string) => string
  } = $props()
  let tab = $state('conversation'), confirmed = $state(false), identity = '', opened = ''
  let panel = $state<HTMLElement>()
  let linkFeedback = $state(''), linkIdentity = ''
  const detail = $derived(snapshot.detail)
  const locked = $derived(snapshot.busy || snapshot.outcome === 'unknown')
  const conversationScope = $derived(JSON.stringify([snapshot.contextEpoch, snapshot.access?.context,
    snapshot.selectedSession, detail?.instance_id, snapshot.selectedTurn]))
  const conversationReadable = $derived(!!resultContext(snapshot) && !snapshot.syncStopped && snapshot.readAt !== null)
  const conversationRoute = $derived(detail && snapshot.currentTurn ? taskRouteHash({ session: snapshot.selectedSession,
    turn: snapshot.turnMode === 'fixed' ? snapshot.selectedTurn : '' }) : '')
  $effect(() => { if (snapshot.selectedSession !== identity) { identity = snapshot.selectedSession; tab = 'conversation'; confirmed = false } })
  $effect(() => {
    const key = `${snapshot.contextEpoch}/${snapshot.selectedSession}/${snapshot.selectedTurn}`
    if (key !== linkIdentity || !snapshot.access?.task_access) { linkIdentity = key; linkFeedback = '' }
  })
  $effect(() => {
    if (detail && detail.session_id !== opened) {
      opened = detail.session_id
      void tick().then(() => {
        if (panel && window.matchMedia('(max-width: 900px)').matches) panel.scrollIntoView({ block: 'start' })
      })
    }
  })
  async function select(id: string) { tab = 'conversation'; await selectTurn(id) }
  async function returnToLive() { tab = 'conversation'; await selectTurn('') }
  function chooseTab(id: string) {
    tab = id
    if (id === 'recap' && !snapshot.recap) void recapRead('latest')
  }
  async function copyLink() {
    if (!detail || !snapshot.currentTurn || !snapshot.access?.task_access || snapshot.syncStopped || snapshot.readAt === null) return
    const key = linkIdentity
    try {
      await navigator.clipboard.writeText(taskRouteUrl({ session: detail.session_id, turn: snapshot.currentTurn.turn_id }, location.href))
      if (key === linkIdentity) linkFeedback = '轮次链接已复制，打开后仍需重新核对访问权限。'
    } catch { if (key === linkIdentity) linkFeedback = '未能复制链接，请核对浏览器剪贴板权限后重试。' }
  }
  async function action(kind: ActionKind, id: string) { await act(kind, id); confirmed = false }
  async function append(reference: DraftReference, text: string): Promise<string> {
    const key = linkIdentity, problem = appendReview(reference, text)
    if (problem) return problem
    tab = 'conversation'
    await tick()
    if (key === linkIdentity) document.getElementById('task-prompt')?.focus()
    return ''
  }
  function tabKey(event: KeyboardEvent, index: number) {
    const target = { ArrowRight: (index + 1) % tabs.length, ArrowLeft: (index + tabs.length - 1) % tabs.length,
      Home: 0, End: tabs.length - 1 }[event.key]
    if (target === undefined) return
    event.preventDefault(); chooseTab(tabs[target]!.id)
    document.getElementById(`tab-${tab}`)?.focus()
  }
</script>

<section class="task-detail-panel" bind:this={panel} aria-labelledby="task-detail-title">
  {#if detail}
    <header class="task-detail-heading"><div><span class="section-eyebrow">{detail.project_id || detail.instance_id}</span><h2 id="task-detail-title">{detail.name || '项目任务'}</h2></div>
      <div class="turn-link-actions"><span class="session-status" data-state={detail.status}>{sessionLabel(detail.status)}</span>
        <IrisButton size="sm" variant="outline" disabled={!snapshot.currentTurn || snapshot.busy || snapshot.syncStopped || snapshot.readAt === null} onclick={() => void copyLink()}>复制本轮链接</IrisButton>
        {#if linkFeedback}<p class="copy-feedback" role="status">{linkFeedback}</p>{/if}</div></header>
    <TaskTurnMode {snapshot} {returnToLive} />
    <div class="task-tabs" role="tablist" aria-label="任务详情视图">
      {#each tabs as item, index}
        <button role="tab" id={`tab-${item.id}`} tabindex={tab === item.id ? 0 : -1} aria-selected={tab === item.id} aria-controls={`pane-${item.id}`} onclick={() => chooseTab(item.id)} onkeydown={(event) => tabKey(event, index)}>{item.label}</button>
      {/each}
    </div>
    {#if tab === 'conversation'}
      {#snippet promptActions(pauseFollowing: () => void)}<PromptReuse {snapshot} append={reusePrompt} {pauseFollowing} />{/snippet}
      <div id="pane-conversation" role="tabpanel" aria-labelledby="tab-conversation"><TaskConversation events={snapshot.events} turn={snapshot.currentTurn ?? undefined} trimmed={snapshot.eventsTrimmed}
        more={snapshot.eventsMore} loading={snapshot.refreshing || snapshot.busy} load={loadEvents}
        scope={conversationScope} readable={conversationReadable} routeHash={conversationRoute} {promptActions} /></div>
    {:else if tab === 'results'}
      <div id="pane-results" role="tabpanel" aria-labelledby="tab-results"><TaskResults {snapshot} load={loadEvents} {append} {downloadTurn} /></div>
    {:else if tab === 'recap'}
      <div id="pane-recap" role="tabpanel" aria-labelledby="tab-recap"><TaskRecap {snapshot} read={recapRead} open={select} /></div>
    {:else if tab === 'turns'}
      <div id="pane-turns" class="turn-history" role="tabpanel" aria-labelledby="tab-turns"><TaskTurnHistory {snapshot} search={turnSearch} navigate={turnNavigate} {select} /></div>
    {:else}
      <div id="pane-events" class="event-inspector" role="tabpanel" aria-labelledby="tab-events"><h3>执行事件</h3><p class="secondary-copy">按服务端游标排列的当前轮次事件，用于核对运行现场。</p>
        <ol class="task-events" aria-label="执行事件窗口">{#each snapshot.events?.data ?? [] as event (event.event_id)}<li><div class="task-event-heading"><strong>#{event.cursor} {event.kind}</strong><span>{event.chunk ? `分块 ${event.chunk}` : ''}</span></div><small>轮次：{event.turn_id}</small>{#if event.text}<pre>{event.text}</pre>{/if}{#if event.truncated}<p class="secondary-copy">此事件正文已截断。</p>{/if}</li>{:else}<li>暂无事件，当前状态以任务与轮次记录为准。</li>{/each}</ol>
        {#if snapshot.eventsTrimmed}<p class="task-notice">已超过本页事件窗口，较早事件未在浏览器中保留。</p>{/if}
        {#if snapshot.eventsMore}<IrisButton variant="outline" disabled={snapshot.busy || snapshot.refreshing} onclick={() => void loadEvents()}>读取后续事件</IrisButton>{/if}
      </div>
    {/if}
    <TaskComposer {detail} {snapshot} {prompt} {locked} {write} {change} {send} {backupDraft} {appendDraftFile} />
    <details class="task-controls"><summary><IrisIcon name="settings" size={16} />任务操作与连接信息</summary>
      <p class="task-binding">项目：{detail.project_id} · 实例：{detail.instance_id}<br />会话：{detail.session_id}<br />活动轮次：{detail.active_turn_id || '无'}</p>
      <label class="confirm-operation"><input type="checkbox" bind:checked={confirmed} disabled={locked} /> 我确认操作当前会话；取消或关闭不会回滚已发生的代码改动。</label>
      <div class="task-actions"><IrisButton variant="outline" disabled={locked || !confirmed || !cancel || !detail.active_turn_id} onclick={() => void action('cancel', detail.active_turn_id)}>请求取消活动轮次</IrisButton>
        <IrisButton variant="outline" disabled={locked || !confirmed || !close || !write || !detail.controllable || !!detail.active_turn_id || detail.status === 'closed'} onclick={() => void action('close', detail.session_id)}>关闭空闲会话</IrisButton></div>
      <p class="secondary-copy">取消请求需等待执行端确认。状态更新为已中断或已取消后，再决定下一步。</p>
    </details>
  {:else}<div class="task-welcome" aria-busy={snapshot.busy}><IrisIcon name="send" size={32} /><h2 id="task-detail-title">{snapshot.busy ? '正在打开任务…' : '从一个明确目标开始'}</h2><p>{snapshot.busy ? '正在读取当前权限、任务状态和轮次记录。' : '选择左侧任务继续工作，或创建新的项目任务。执行中的输出会持续更新，历史指令和结果可以随时查看。'}</p><div class="welcome-steps"><span>01 选择项目</span><span>02 描述目标</span><span>03 跟踪并核对结果</span></div></div>{/if}
</section>
