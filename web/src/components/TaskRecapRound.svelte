<script lang="ts">
  import { IrisButton } from '../lib/iris'
  import { projectConversation, replyCompletionMissing } from '../lib/conversation'
  import { localTime, sessionLabel } from '../lib/presentation'
  import type { Tasks } from '../lib/task-state'
  import TaskReply from './TaskReply.svelte'
  let { row, locked, open, scope }: { row: NonNullable<Tasks['recap']>['rows'][number]; locked: boolean;
    open: (turn: string) => Promise<void>; scope: string } = $props()
  const view = $derived(projectConversation(row.events.data))
  const hasReply = $derived(view.messages.some((message) => message.text))
  const missingCompletion = $derived(replyCompletionMissing(row.events.data, row.turn.state))
  const incomplete = $derived(row.more || row.trimmed || view.partial || missingCompletion)
</script>

<section class="recap-round" aria-label={`回顾轮次 ${row.turn.turn_id}`}>
  <header class="recap-round-heading"><div><span class="session-status" data-state={row.turn.state}>{sessionLabel(row.turn.state)}</span>
    <span class="record-meta">{localTime(row.turn.created_at)} · {row.turn.turn_id}</span></div>
    <IrisButton size="sm" variant="outline" disabled={locked} onclick={() => void open(row.turn.turn_id)}>打开此轮</IrisButton></header>
  <article class="conversation-message user-message"><div class="message-author">你的指令</div><pre>{row.turn.prompt}</pre>
    {#if row.turn.prompt_truncated}<p class="secondary-copy">历史指令显示已截断。</p>{/if}</article>
  {#each view.messages as message (message.id)}
    {#if message.text}<article class="conversation-message agent-message"><TaskReply text={message.text} scope={JSON.stringify([scope, message.id])} />
      {#if message.partial}<p class="secondary-copy">此回复包含不完整或截断的片段。</p>{/if}</article>{/if}
  {/each}
  {#if !hasReply}<p class="recap-no-reply">{incomplete ? '已读取片段尚未包含可显示的回复，不能据此判断本轮没有回复。'
      : ['queued', 'running', 'cancel_requested'].includes(row.turn.state) ? '读取时本轮仍在处理中，当前片段没有可显示的回复。可刷新回顾或打开此轮跟进。'
      : '已读取事件中没有可显示的 Agent 回复。请打开此轮核对执行状态与事件。'}</p>{/if}
  {#if incomplete}<div class="task-notice">
    {#if row.more}<p>尚有未读取事件，请打开此轮继续查看。</p>{/if}
    {#if row.trimmed}<p>本轮较早事件未保留在回顾窗口。</p>{/if}
    {#if view.partial}<p>当前回复含缺片或截断，不能视为完整回复。</p>{/if}
    {#if missingCompletion}<p>未收到回复结束记录，完整性未知。</p>{/if}
  </div>{/if}
  {#if view.legacy}<p class="secondary-copy">部分事件没有消息标识，仅在本轮内按原有输出语义呈现。</p>{/if}
</section>
