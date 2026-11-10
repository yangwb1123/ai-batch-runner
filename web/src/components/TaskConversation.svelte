<script lang="ts">
  import { onDestroy, onMount, tick, type Snippet } from 'svelte'
  import { IrisButton } from '../lib/iris'
  import { projectConversation, replyCompletionMissing } from '../lib/conversation'
  import { findConversation, normalizeFindQuery, type FindMatch, type FindSource } from '../lib/conversation-find'
  import { sessionLabel } from '../lib/presentation'
  import type { Events, Turn } from '../lib/task-models'
  import TaskReply from './TaskReply.svelte'
  import ConversationFind from './ConversationFind.svelte'
  import ConversationText from './ConversationText.svelte'
  import '../conversation-find.css'
  let { events, turn, trimmed, more, loading, load, scope, readable, routeHash, promptActions }: { events: Events | null; turn: Turn | undefined;
    trimmed: boolean; more: boolean; loading: boolean; load: () => Promise<void>;
    scope: string; readable: boolean; routeHash: string; promptActions?: Snippet<[() => void]> } = $props()
  const view = $derived(projectConversation(events?.data ?? []))
  const missingCompletion = $derived(replyCompletionMissing(events?.data ?? [], turn?.state ?? 'running'))
  const sources: FindSource[] = $derived(turn ? [{ key: 'prompt', kind: 'prompt', label: '你的指令', text: turn.prompt },
    ...view.messages.map((message, index) => ({ key: replyKey(message.id), kind: 'reply' as const, label: `Agent 回复 ${index + 1}`, text: message.text }))] : [])
  let value = $state(''), query = $state(''), problem = $state(''), updated = $state(false)
  let selected = $state<{ key: string; start: number; end: number; text: string } | null>(null)
  let visible = $state(true), identity = '', navigation = 0, alive = true, followLocked = false
  const scan = $derived.by(() => {
    try { return { result: findConversation(readable && visible ? sources : [], query), problem: '' } }
    catch (error) { return { result: { query: '', matches: [], limited: false }, problem: error instanceof Error ? error.message : '当前对话未通过查找校验。' } }
  })
  const selectedIndex = $derived.by(() => {
    const current = selected
    if (!current) return -1
    const source = sources.find((item) => item.key === current.key)
    return scan.result.matches.find((match) => match.key === current.key && match.start === current.start &&
      match.end === current.end && source?.text.slice(match.start, match.end) === current.text)?.index ?? -1
  })
  const diagnostics = $derived((events?.data ?? []).filter((event) => ['turn.failed', 'turn.interrupted', 'turn.lost'].includes(event.kind)))
  const activity = $derived((events?.data ?? []).filter((event) => event.kind.startsWith('command.') || event.kind.startsWith('file.')))
  let following = $state(true), panel = $state<HTMLDivElement>()
  function pauseFollowing() { following = false; followLocked = true }
  function replyKey(id: string) { return JSON.stringify(['reply', id]) }
  function clearFind() { value = ''; query = ''; selected = null; problem = ''; updated = false; navigation++ }
  function usable() { return alive && readable && visible && !document.hidden && scope === identity && location.hash === routeHash }
  function sameSources(before: FindSource[]) { return before.length === sources.length && before.every((item, index) => item.key === sources[index].key && item.text === sources[index].text) }
  function scrollToMatch(index: number) {
    const mark = panel?.querySelector<HTMLElement>(`mark[data-conversation-match="${index}"]`)
    if (!panel || !mark) return
    const frame = panel.getBoundingClientRect(), bounds = mark.getBoundingClientRect()
    const top = Math.max(frame.top + panel.clientTop, 0)
    const bottom = Math.min(frame.top + panel.clientTop + panel.clientHeight, window.innerHeight)
    const center = bottom > top ? (top + bottom) / 2 : frame.top + panel.clientTop + panel.clientHeight / 2
    const offset = panel.scrollTop + bounds.top + bounds.height / 2 - center
    panel.scrollTop = Math.max(0, Math.min(panel.scrollHeight - panel.clientHeight, offset))
  }
  function locate(match: FindMatch) {
    const source = sources.find((item) => item.key === match.key)
    if (!source || !usable()) { clearFind(); return }
    pauseFollowing(); updated = false
    selected = { key: match.key, start: match.start, end: match.end, text: source.text.slice(match.start, match.end) }
    const request = ++navigation, context = scope, before = sources
    void tick().then(() => {
      if (!usable() || context !== scope || request !== navigation || !sameSources(before) || selectedIndex < 0) return
      scrollToMatch(selectedIndex)
    })
  }
  function search() {
    if (!usable()) { clearFind(); return }
    try {
      const next = normalizeFindQuery(value), found = findConversation(sources, next)
      value = next; query = next; selected = null; problem = ''; updated = false; navigation++
      if (found.matches.length) locate(found.matches[0])
    } catch (error) { query = ''; selected = null; navigation++; problem = error instanceof Error ? error.message : '关键词未通过校验。' }
  }
  function move(direction: number) {
    if (!usable() || !scan.result.matches.length) { clearFind(); return }
    const count = scan.result.matches.length, index = selectedIndex < 0 ? direction > 0 ? 0 : count - 1 : (selectedIndex + direction + count) % count
    locate(scan.result.matches[index])
  }
  $effect(() => {
    const current = selected, index = selectedIndex
    if (scope !== identity || !readable || !visible) { identity = scope; clearFind() }
    else if (current && index < 0) { selected = null; navigation++; updated = true }
  })
  $effect(() => {
    void view.messages
    if (following) void tick().then(() => { if (following && panel) panel.scrollTop = panel.scrollHeight })
  })
  onMount(() => {
    const visibility = () => { visible = !document.hidden; if (!visible) clearFind() }
    const hide = () => { visible = false; clearFind() }
    visibility()
    document.addEventListener('visibilitychange', visibility)
    window.addEventListener('pagehide', hide); window.addEventListener('pageshow', visibility); window.addEventListener('hashchange', clearFind)
    return () => {
      document.removeEventListener('visibilitychange', visibility)
      window.removeEventListener('pagehide', hide); window.removeEventListener('pageshow', visibility); window.removeEventListener('hashchange', clearFind)
    }
  })
  onDestroy(() => { alive = false; clearFind() })
</script>

<ConversationFind bind:value result={scan.result} {selectedIndex} readable={readable && visible} problem={problem || scan.problem} {updated}
  incomplete={view.partial || !!turn?.prompt_truncated} {more} {trimmed} {search} {move} clear={clearFind} />
<div class="conversation-follow"><button aria-pressed={following} onclick={() => { following = !following; followLocked = !following }}>{following ? '跟随最新输出' : '已暂停跟随输出'}</button></div>
<div class="task-conversation" bind:this={panel} onscroll={() => { if (panel && !followLocked) following = panel.scrollHeight - panel.scrollTop - panel.clientHeight < 80 }} aria-label="任务对话" aria-busy={loading}>
  {#if turn}
    <div class="turn-context"><span class="session-status" data-state={turn.state}>{sessionLabel(turn.state)}</span><span>轮次 {turn.turn_id}</span></div>
    <article class="conversation-message user-message"><div class="message-author">你的指令</div><pre><ConversationText text={turn.prompt} matches={scan.result.matches.filter((match) => match.key === 'prompt')} {selectedIndex} /></pre>{#if turn.prompt_truncated}<p class="secondary-copy">历史指令显示已截断。</p>{/if}{@render promptActions?.(pauseFollowing)}</article>
    {#each view.messages as message (message.id)}
      {#if message.text}<article class="conversation-message agent-message"><TaskReply text={message.text} scope={JSON.stringify([scope, message.id])}
        matches={scan.result.matches.filter((match) => match.key === replyKey(message.id))} {selectedIndex} />{#if message.partial}<p class="secondary-copy">此回复包含不完整或截断的片段。</p>{/if}</article>{/if}
    {/each}
    {#if !view.messages.some((message) => message.text)}<div class="conversation-empty"><span class="activity-dot" aria-hidden="true"></span>{['queued', 'running', 'cancel_requested'].includes(turn.state) ? '任务正在处理，输出会自动出现在这里。' : '当前轮次没有可显示的 Agent 回复。请查看执行状态或事件。'}</div>{/if}
    {#if missingCompletion}<p class="task-notice" role="status">未收到回复结束记录，完整性未知。</p>{/if}
    {#if diagnostics.length}<div class="task-notice"><strong>执行需要处理</strong>{#each diagnostics as event (event.event_id)}<p>{event.text || sessionLabel(turn.state)}</p>{/each}<p>核对项目实际改动后，可以在下方发送下一条指令。</p></div>{/if}
    {#if activity.length}<details class="task-evidence"><summary>执行活动 · {activity.length} 条</summary>{#each activity as event (event.event_id)}<p><strong>{event.kind}</strong></p><pre>{event.text || '已记录活动，未提供正文。'}</pre>{/each}</details>{/if}
    <div class="result-evidence"><strong>结果核对</strong><p>执行状态：{sessionLabel(turn.state)}。目标验收、代码变更检查与交付文件未由 Web 独立验证；请结合 Agent 回复和项目门禁核对。</p></div>
  {:else}<div class="conversation-empty"><strong>任务已准备好</strong><p>在下方描述你的目标。执行进度、回复和状态会持续更新。</p></div>{/if}
  {#if trimmed || view.partial}<p class="task-notice">当前显示是有界输出窗口，部分内容未完整保留。可在执行事件中核对游标与截断标记。</p>{/if}
  {#if view.legacy}<p class="secondary-copy">部分历史事件没有消息标识，按原有输出语义呈现。</p>{/if}
  {#if more}<IrisButton variant="outline" disabled={loading} onclick={() => void load()}>继续读取输出</IrisButton>{/if}
</div>
