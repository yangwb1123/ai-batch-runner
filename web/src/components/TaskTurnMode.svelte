<script lang="ts">
  import { IrisButton } from '../lib/iris'
  import { sessionLabel } from '../lib/presentation'
  import type { Tasks } from '../lib/task-state'
  import '../turn-follow.css'
  let { snapshot, returnToLive }: { snapshot: Tasks; returnToLive: () => Promise<void> } = $props()
  const fixed = $derived(snapshot.turnMode === 'fixed')
  const other = $derived(snapshot.liveTurn && snapshot.liveTurn.turn_id !== snapshot.selectedTurn ? snapshot.liveTurn : null)
  const unavailable = $derived(snapshot.busy || snapshot.syncStopped || snapshot.readAt === null || !snapshot.access?.task_access)
</script>

<section class="turn-view-mode" aria-label="轮次查看模式">
  <div class="turn-view-heading"><strong>{fixed ? '固定查看此轮' : '跟随任务现场'}</strong>
    {#if fixed}<IrisButton size="sm" variant="outline" disabled={unavailable} onclick={() => void returnToLive()}>回到任务现场</IrisButton>{/if}
  </div>
  {#if fixed}
    <p>这里保留所选轮次。发送新指令会继续当前会话，不会回滚或重放此轮。</p>
    {#if other}<p class="turn-view-update" role="status"><strong>任务现场已有另一轮</strong> · {sessionLabel(other.state)}<br />轮次 {other.turn_id}</p>{/if}
  {:else}<p>自动显示当前活动轮次；空闲时显示最新轮次。历史搜索与筛选不会改变跟随目标。</p>{/if}
</section>
