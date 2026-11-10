<script lang="ts">
  import { IrisButton } from '../lib/iris'
  import { sessionLabel } from '../lib/presentation'
  import type { Operations } from '../lib/task-models'
  let { operations, disabled, history, open }: { operations: Operations | null; disabled: boolean;
    history: (before?: string) => Promise<void>; open: (id: string) => Promise<void> } = $props()
</script>

<section class="records-panel" aria-labelledby="task-operations-title">
  <div class="records-heading"><div><span class="section-eyebrow">DURABLE REQUESTS</span><h2 id="task-operations-title">创建 / 关闭请求记录</h2></div>
    <IrisButton size="sm" variant="outline" disabled={disabled} onclick={() => void history()}>刷新请求记录</IrisButton></div>
  <p class="task-content secondary-copy">来自 Hub 持久账本，覆盖当前授权实例，每页 20 条。响应丢失可先在此核对；当前页未找到不证明请求未受理，不会自动重发。</p>
  {#if operations}
    <ul class="task-operations">
      {#each operations.data as item (`${item.operation}:${item.request_id}`)}
        <li><div><strong>{item.operation === 'create' ? '创建' : '关闭'} · {item.name || item.session_id}</strong>
          <span class="record-meta">实例 {item.instance_id} · {sessionLabel(item.status)}</span><span class="record-meta">请求 {item.request_id}</span></div>
          <IrisButton size="sm" variant="outline" disabled={disabled || !item.session_id} onclick={() => void open(item.session_id)}>打开对应会话</IrisButton></li>
      {:else}<li>当前页没有创建或关闭请求。</li>{/each}
    </ul>
    {#if operations.next_cursor}<div class="task-content"><IrisButton size="sm" variant="outline" disabled={disabled} onclick={() => void history(operations!.next_cursor)}>查看更早请求</IrisButton></div>{/if}
  {:else}<p class="task-content secondary-copy">请求记录未读取，或当前只有元数据读取权限。</p>{/if}
</section>
