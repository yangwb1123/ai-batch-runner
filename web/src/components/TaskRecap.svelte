<script lang="ts">
  import { IrisButton } from '../lib/iris'
  import { localTime } from '../lib/presentation'
  import type { Tasks } from '../lib/task-state'
  import TaskRecapRound from './TaskRecapRound.svelte'
  import '../recap.css'
  let { snapshot, read, open }: { snapshot: Tasks;
    read: (direction: 'latest' | 'older' | 'newer' | 'refresh') => Promise<void>;
    open: (turn: string) => Promise<void> } = $props()
  const loading = $derived(snapshot.busy || snapshot.refreshing)
  const locked = $derived(snapshot.busy || snapshot.syncStopped || !snapshot.access?.task_access || snapshot.readAt === null)
  const recap = $derived(snapshot.recap)
  const rows = $derived(recap ? [...recap.rows].reverse() : [])
</script>

<section class="task-recap" aria-label="多轮回顾工作区" aria-busy={loading}>
  <header class="recap-heading"><div><h3>回顾指令与回复</h3><p>每页五轮，本页按从早到晚排列。打开某一轮，可继续核对结果与执行证据。</p></div>
    <IrisButton size="sm" variant="outline" disabled={locked} onclick={() => void read('latest')}>最新五轮</IrisButton></header>
  <p class="secondary-copy">这里保留上次读取的回顾，新执行不会自动替换本页。各轮内容在不同时间读取，不代表同一时刻的完整现场。目标验收未独立验证。</p>
  {#if recap}
    <div class="recap-page-heading"><p class="secondary-copy">回顾读取于 {localTime(recap.readAt, true)} · 第 {snapshot.recapPage} 页 · 本页 {rows.length} 轮</p>
      <IrisButton size="sm" variant="outline" disabled={locked} onclick={() => void read('refresh')}>刷新本页回顾</IrisButton></div>
    <div class="recap-rounds task-conversation" aria-label="本页多轮对话">
      {#each rows as row (row.turn.turn_id)}
        <TaskRecapRound {row} {locked} {open} scope={JSON.stringify([snapshot.contextEpoch, snapshot.selectedSession, row.turn.turn_id])} />
      {:else}<p class="conversation-empty">本页没有可回顾的轮次。可以返回最新五轮核对当前记录。</p>{/each}
    </div>
    <nav class="recap-navigation" aria-label="多轮回顾分页">
      <IrisButton size="sm" variant="outline" disabled={locked || !recap.next_cursor} onclick={() => void read('older')}>更早的五轮</IrisButton>
      <IrisButton size="sm" variant="outline" disabled={locked || !snapshot.recapTrail.length} onclick={() => void read('newer')}>返回较新一页</IrisButton>
    </nav>
    <p class="secondary-copy">每轮最多读取前 100 条事件；后续内容或超出窗口的片段会单独标明。回顾翻页替换正文，最多保留 32 个返回位置。</p>
  {:else}
    <div class="recap-empty"><p>{loading ? '正在读取任务数据…' : '回顾尚未读取成功。读取只查看记录，不执行指令，也不改变当前草稿。'}</p>
      <IrisButton variant="outline" disabled={locked} onclick={() => void read('latest')}>读取多轮回顾</IrisButton></div>
  {/if}
</section>
