<script lang="ts">
  import { onMount } from 'svelte'
  import { IrisButton, IrisIcon } from '../lib/iris'
  import { createTasks } from '../lib/tasks'
  import { createDrafts } from '../lib/drafts'
  import { appendReference, type DraftReference } from '../lib/result-followup'
  import { appendDraft, exportDraft, draftMatches, type DraftTarget } from '../lib/draft-file'
  import { exportMatches, exportTurnRecord, exportTurnReport, type ExportTarget } from '../lib/task-export'
  import { appendPromptReuse, type PromptReference } from '../lib/prompt-reuse'
  import type { ResultFormat } from './ResultDownloads.svelte'
  import { parseTaskRoute, taskRouteHash, type TaskRoute } from '../lib/task-route'
  import { localTime, sessionLabel } from '../lib/presentation'
  import TaskList from './TaskList.svelte'
  import TaskCreate from './TaskCreate.svelte'
  import TaskDetail from './TaskDetail.svelte'
  import TaskOperations from './TaskOperations.svelte'
  let { active, contextChanged }: { active: boolean; contextChanged: () => void } = $props()
  const tasks = createTasks(), drafts = createDrafts()
  const consumedReferences = new WeakSet<DraftReference>()
  const consumedDrafts = new WeakSet<DraftTarget | PromptReference>()
  const requestLabels = { create: '创建会话', send: '发送指令', cancel: '请求取消轮次', close: '关闭会话' }
  let instance = $state(''), creating = $state(false), suspended = $state(false), visible = $state(true)
  let prompt = $state(''), draftProblem = $state(''), routeProblem = $state(''), draftSession = '', draftEpoch = -1
  let route = $state<TaskRoute | null>(null), routing = $state(false)
  let lastTaskRoute = $state<(TaskRoute & { epoch: number }) | null>(null)
  const write = $derived($tasks.access?.scopes.includes('agent.sessions:write') ?? false)
  const cancel = $derived($tasks.access?.scopes.includes('agent.turns:cancel') ?? false)
  const locked = $derived($tasks.busy || $tasks.outcome === 'unknown')
  const close = $derived($tasks.instances.find((row) => row.instance_id === $tasks.detail?.instance_id)?.session_close_supported === true)
  $effect(() => { tasks.setActive(active && visible && !suspended) })
  $effect(() => { if (active && visible && !suspended && !$tasks.initialized && !$tasks.busy) void tasks.refresh() })
  $effect(() => {
    if (active && route && $tasks.access?.task_access && !$tasks.busy && !routing && !suspended) {
      const target = route; route = null; routing = true
      void applyRoute(target).finally(() => { routing = false })
    }
  })
  $effect(() => {
    const id = $tasks.selectedSession, epoch = $tasks.contextEpoch
    if (id !== draftSession || epoch !== draftEpoch) {
      if (draftEpoch !== -1 && epoch !== draftEpoch) { contextChanged(); creating = false; instance = ''; lastTaskRoute = null }
      drafts.context(epoch); draftEpoch = epoch; draftSession = id; prompt = drafts.get(id); draftProblem = ''
    }
    if (active && id && !route && !routing && !routeProblem && $tasks.detail?.session_id === id) {
      let current: TaskRoute | null
      try { current = parseTaskRoute(location.hash) } catch { return }
      if (lastTaskRoute && $tasks.currentTurn && (lastTaskRoute.session !== id ||
          (lastTaskRoute.turn && lastTaskRoute.turn !== $tasks.currentTurn.turn_id))) lastTaskRoute = null
      const intended = current ?? (lastTaskRoute?.epoch === epoch ? lastTaskRoute : null)
      const explicit = $tasks.turnMode === 'fixed' && intended?.session === id && intended.turn === $tasks.selectedTurn ? intended.turn : ''
      const target = taskRouteHash({ session: id, turn: explicit })
      if (location.hash === '#tasks') history.replaceState(null, '', target)
      else if (location.hash.startsWith('#tasks/') && location.hash !== target) history.pushState(null, '', target)
    }
  })
  async function applyRoute(target: TaskRoute) {
    if (target.session === $tasks.selectedSession && $tasks.detail) {
      if (!target.turn) await tasks.returnToLive()
      else if (target.turn !== $tasks.selectedTurn || $tasks.turnMode !== 'fixed' || !$tasks.currentTurn) await tasks.selectTurn(target.turn)
    } else await tasks.openTarget(target.session, target.turn)
    lastTaskRoute = $tasks.detail?.session_id === target.session && (!target.turn || $tasks.currentTurn?.turn_id === target.turn)
      ? { ...target, epoch: $tasks.contextEpoch } : null
  }
  function change(text: string) {
    try { drafts.set($tasks.selectedSession, text); prompt = text; draftProblem = ''; return true }
    catch { draftProblem = '草稿超过 65536 字节，或已达到 16 个会话的草稿容量；当前输入未接受。请精简，或先保存并清空其他任务的草稿。'; return false }
  }
  function appendReview(reference: DraftReference, text: string): string {
    if (!active || suspended || route || routing || routeProblem) return '任务视图已切换，请重新选择记录后起草。'
    if (consumedReferences.has(reference)) return '此引用已追加到草稿，请先核对已有内容。'
    try {
      const target = parseTaskRoute(location.hash)
      if (!target || target.session !== $tasks.selectedSession || (target.turn && target.turn !== $tasks.selectedTurn) ||
          draftSession !== $tasks.selectedSession || draftEpoch !== $tasks.contextEpoch) return '任务或草稿上下文正在切换，请重新核对后追加。'
      const next = appendReference($tasks, reference, prompt, text)
      if (!change(next)) return draftProblem
      consumedReferences.add(reference)
      return ''
    } catch (error) { return error instanceof Error ? error.message : '引用未通过校验，原草稿已保留。' }
  }
  function checkDraftTarget(target: DraftTarget) {
    if (!active || suspended || route || routing || routeProblem) throw Error('任务视图已切换，请重新选择草稿文件。')
    const current = parseTaskRoute(location.hash)
    if (!current || current.session !== $tasks.selectedSession || (current.turn && current.turn !== $tasks.selectedTurn) ||
        draftSession !== $tasks.selectedSession || draftEpoch !== $tasks.contextEpoch || !draftMatches($tasks, target)) {
      throw Error('草稿目标或权限已变化，请重新核对当前任务。')
    }
  }
  function backupDraft(target: DraftTarget) {
    checkDraftTarget(target)
    return exportDraft($tasks, prompt)
  }
  function downloadTurn(target: ExportTarget, format: ResultFormat) {
    if (!active || !visible || document.hidden || suspended || route || routing || routeProblem) {
      throw Error('任务视图已切换，请重新核对当前轮次。')
    }
    const current = parseTaskRoute(location.hash)
    if (!current || current.session !== target.session ||
        current.turn !== ($tasks.turnMode === 'fixed' ? target.turn : '') || !exportMatches($tasks, target)) {
      throw Error('轮次目标或读取权限已变化，请重新核对后下载。')
    }
    return format === 'text' ? exportTurnReport($tasks)
      : { ...exportTurnRecord($tasks), mime: 'application/json;charset=utf-8' }
  }
  function appendDraftFile(target: DraftTarget, text: string): string {
    try {
      checkDraftTarget(target)
      return commitDraftPreview(target, () => appendDraft($tasks, target, prompt, text))
    } catch (error) { return error instanceof Error ? error.message : '未能加入草稿，原稿已保留。' }
  }
  function commitDraftPreview(reference: DraftTarget | PromptReference, compose: () => string): string {
    if (consumedDrafts.has(reference)) return '这份预览已加入草稿，请先核对已有内容。'
    if (!change(compose())) return draftProblem
    consumedDrafts.add(reference)
    return ''
  }
  function reusePrompt(reference: PromptReference, text: string): string {
    try {
      checkDraftTarget(reference.target)
      const expected = taskRouteHash({ session: reference.target.session,
        turn: $tasks.turnMode === 'fixed' ? reference.turn : '' })
      if (!visible || document.hidden || location.hash !== expected ||
          $tasks.selectedTurn !== reference.turn || $tasks.currentTurn?.turn_id !== reference.turn) {
        throw Error('轮次或页面目标已变化，请重新核对当前指令。')
      }
      return commitDraftPreview(reference, () => appendPromptReuse($tasks, reference, prompt, text))
    } catch (error) { return error instanceof Error ? error.message : '未能复用指令，原草稿与预览已保留。' }
  }
  function open(id: string) {
    const target = { session: id, turn: '' }, hash = taskRouteHash(target)
    if (location.hash === hash) void tasks.open(id); else navigate(target, hash)
  }
  async function selectTurn(id: string) {
    const target = { session: $tasks.selectedSession, turn: id }, hash = taskRouteHash(target)
    if (location.hash === hash) await tasks.selectTurn(id); else navigate(target, hash)
  }
  function navigate(target: TaskRoute, hash: string) {
    routeProblem = ''; route = target
    history.pushState(null, '', hash)
  }
  async function create(id: string, name: string) {
    await tasks.act('create', id, name)
    if ($tasks.outcome !== 'accepted') return false
    instance = id
    return true
  }
  async function send() {
    const id = $tasks.selectedSession, text = prompt
    await tasks.act('send', id, text)
    if ($tasks.outcome === 'accepted' && $tasks.receipt?.session_id === id && drafts.consume(id, text) && draftSession === id) prompt = ''
  }
  async function retry() {
    const action = $tasks.pending
    await tasks.retry()
    if (action?.kind !== 'send' || $tasks.outcome !== 'accepted') return
    const text = JSON.parse(action.body).prompt as string
    if (drafts.consume(action.target, text) && draftSession === action.target) prompt = ''
  }
  onMount(() => {
    const readRoute = () => {
      routeProblem = ''
      try {
        route = parseTaskRoute(location.hash)
      } catch { route = null; routeProblem = '任务链接无效。请从任务列表选择已授权的任务或轮次。'; tasks.clearSelection() }
    }
    const visibility = () => { visible = !document.hidden }
    const forget = () => { suspended = true; lastTaskRoute = null; tasks.forget(); drafts.clear(); prompt = ''; instance = ''; creating = false }
    const restore = (event: PageTransitionEvent) => { if (event.persisted) { forget(); suspended = false; readRoute() } }
    const warn = (event: BeforeUnloadEvent) => {
      if (drafts.hasAny() || ($tasks.busy && $tasks.pending) || $tasks.outcome === 'unknown') { event.preventDefault(); event.returnValue = '' }
    }
    readRoute(); visibility()
    window.addEventListener('hashchange', readRoute); window.addEventListener('pagehide', forget)
    window.addEventListener('pageshow', restore); window.addEventListener('beforeunload', warn)
    document.addEventListener('visibilitychange', visibility)
    return () => {
      suspended = true; tasks.dispose(); drafts.clear()
      window.removeEventListener('hashchange', readRoute); window.removeEventListener('pagehide', forget)
      window.removeEventListener('pageshow', restore); window.removeEventListener('beforeunload', warn)
      document.removeEventListener('visibilitychange', visibility)
    }
  })
</script>

<div class="task-console" hidden={!active}>
  <div class="task-toolbar"><div><span class="live-indicator" class:connected={$tasks.watching && !$tasks.syncStopped}></span><strong>{$tasks.syncStopped ? '更新已暂停' : $tasks.refreshing ? '正在同步任务' : $tasks.watching ? '任务持续更新中' : '任务工作区'}</strong><span class="secondary-copy">最近同步 {localTime($tasks.readAt, true)}</span></div>
    <IrisButton size="sm" variant="outline" disabled={$tasks.busy} onclick={() => void tasks.refresh()}><IrisIcon name="refresh" size={16} />刷新任务现场</IrisButton></div>
  {#if $tasks.problem}<div class="task-notice" role="alert">{$tasks.problem}</div>{/if}
  {#if draftProblem}<p class="task-notice" role="alert">{draftProblem}</p>{/if}
  {#if routeProblem}<p class="task-notice" role="alert">{routeProblem}</p>{/if}
  {#if $tasks.access && !$tasks.access.task_access}<div class="task-access-note"><strong>当前账号尚未开通任务工作区</strong><p>可以查看已授权的项目概况。要发送任务，请联系管理员为你的账号开通对应项目的任务权限。</p></div>{/if}
  {#if $tasks.access?.task_access}<span class="task-access-label">{write ? '当前账号具备任务写授权' : '当前账号只读任务详情'}</span>{/if}
  {#if $tasks.outcome === 'unknown'}<div class="task-notice uncertain-request" role="alert"><strong>提交结果未知，需要先核对</strong><p>请求可能已执行。新的写操作已暂停；请先查看任务和请求记录，避免重复执行。</p>
    {#if $tasks.pending}<details><summary>请求核对信息</summary><code>{$tasks.pending.key}</code></details><IrisButton variant="outline" disabled={$tasks.busy} onclick={() => void retry()}>用原目标、原内容和原 key 核对重试</IrisButton>{/if}</div>{/if}
  {#if $tasks.receipt}
    {@const receipt = $tasks.receipt}
    <div class="task-receipt" role="status" aria-label="最近一次请求回执">
      <strong>最近一次请求回执</strong><span>响应当时状态：{sessionLabel(receipt.status)}。</span>
      <span>此回执保留响应当时的结果，当前执行状态请查看所选任务与轮次。</span>
      <details><summary>对应请求与目标</summary>
        <p>请求类型：{receipt.kind ? requestLabels[receipt.kind] : '未记录'}</p>
        <p>{receipt.kind === 'create' || receipt.kind === 'close' ? '请求 ID' : receipt.kind === 'send' || receipt.kind === 'cancel' ? '轮次 ID' : '请求或轮次标识'}：<code>{receipt.id}</code></p>
        <p>会话：<code>{receipt.session_id || '响应当时尚未提供'}</code></p>
        <p>原请求目标：{receipt.kind === 'create' ? '实例' : receipt.kind === 'cancel' ? '轮次' : receipt.kind ? '会话' : '类型未记录'} <code>{receipt.target || '未记录'}</code></p>
      </details>
    </div>
  {/if}
  {#if creating}<TaskCreate instances={$tasks.instances} initial={$tasks.catalogQuery.instance_id || instance} disabled={locked} writable={write} {create} dismiss={() => creating = false} />{/if}
  <div class="task-layout">{#key $tasks.contextEpoch}<TaskList rows={$tasks.catalog?.data ?? []} instances={$tasks.instances} selected={$tasks.selectedSession}
    query={$tasks.catalogQuery} page={$tasks.catalogPage} hasNext={!!$tasks.catalog?.next_cursor} hasPrevious={$tasks.catalogTrail.length > 0}
    disabled={$tasks.busy} refreshing={$tasks.refreshing} readable={$tasks.access?.task_access ?? false}
    chooseQuery={tasks.catalogSearch} navigate={tasks.catalogNavigate} {open} create={() => creating = true} />{/key}
    {#if active && !suspended && !routeProblem && !route && !routing}<TaskDetail snapshot={$tasks} {prompt} {write} {cancel} {close} {change} {send} act={tasks.act} {selectTurn}
      turnSearch={tasks.turnSearch} turnNavigate={tasks.turnNavigate} recapRead={tasks.recapRead} loadEvents={tasks.loadEvents} {appendReview} {backupDraft} {appendDraftFile} {downloadTurn} {reusePrompt} />
    {:else if active && !routeProblem}<section class="task-detail-panel task-welcome" aria-label="正在切换任务视图"><p role="status">正在切换任务视图，旧内容已关闭。等待当前请求结束并重新核对访问权限。</p></section>{/if}</div>
  <details class="request-history"><summary>创建与关闭请求记录</summary><TaskOperations operations={$tasks.operations} disabled={$tasks.busy || !$tasks.access?.task_access} history={tasks.history} open={tasks.open} /></details>
</div>
