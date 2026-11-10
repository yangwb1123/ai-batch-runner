<script lang="ts">
  import { onDestroy } from 'svelte'
  import { IrisButton } from '../lib/iris'
  let { text, truncated, scope, disabled }: { text?: string; truncated?: boolean; scope: string; disabled: boolean } = $props()
  let copied = $state(''), identity = '', currentText: string | undefined, alive = true
  $effect(() => { if (scope !== identity || text !== currentText) { identity = scope; currentText = text; copied = '' } })
  onDestroy(() => { alive = false })
  async function copy() {
    if (disabled || text === undefined) return
    const current = scope, value = text
    try {
      await navigator.clipboard.writeText(value)
      if (alive && scope === current && text === value) copied = '已复制当前显示的命令输出。'
    } catch {
      if (alive && scope === current && text === value) copied = '浏览器未允许复制，请选择文本复制。'
    }
  }
</script>

{#if text !== undefined}
  <details class="command-output">
    <summary>查看命令输出{truncated ? '（不完整）' : ''}</summary>
    {#if text}<div class="command-output-actions"><IrisButton size="sm" variant="ghost" {disabled} onclick={() => void copy()}>复制命令输出</IrisButton></div>
      <pre aria-label="命令输出正文">{text}</pre>
    {:else}<p class="secondary-copy">{truncated ? '没有可显示的完整输出内容。' : '执行端返回了空文本输出。'}</p>{/if}
    {#if truncated}<p class="secondary-copy">输出已截断、缺失或省略。这里不能代表完整命令日志。</p>{/if}
    {#if copied}<p class="copy-feedback" role="status">{copied}</p>{/if}
  </details>
{:else}<p class="secondary-copy">执行端未提供命令输出。</p>{/if}
