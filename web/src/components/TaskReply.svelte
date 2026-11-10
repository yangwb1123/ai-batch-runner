<script lang="ts">
  import { onDestroy } from 'svelte'
  import { IrisButton } from '../lib/iris'
  import { formatReply } from '../lib/reply-format'
  import type { FindMatch } from '../lib/conversation-find'
  import ConversationText from './ConversationText.svelte'
  import '../reply.css'
  let { text, scope, matches = [], selectedIndex = -1 }: { text: string; scope: string;
    matches?: FindMatch[]; selectedIndex?: number } = $props()
  let raw = $state(false), copied = $state(''), identity = '', alive = true
  const preview = $derived(formatReply(text))
  $effect(() => { if (scope !== identity) { identity = scope; raw = false; copied = '' } })
  onDestroy(() => { alive = false })
  async function copy(value: string, label: string) {
    const current = scope
    try {
      await navigator.clipboard.writeText(value)
      if (alive && scope === current) copied = `已复制${label}`
    } catch {
      if (alive && scope === current) copied = '浏览器未允许复制，请选择文本复制。'
    }
  }
</script>

<div class="task-reply">
  <div class="message-author reply-toolbar"><span>Agent 回复</span><div class="reply-actions">
    {#if !preview.limited}<IrisButton size="sm" variant="ghost" disabled={matches.length > 0} onclick={() => raw = !raw}>{raw ? '格式预览' : '查看原文'}</IrisButton>{/if}
    <IrisButton size="sm" variant="ghost" onclick={() => void copy(text, '回复')}>复制回复</IrisButton>
  </div></div>
  {#if raw || preview.limited || matches.length}
    {#if matches.length}<p class="secondary-copy">查找匹配时显示原文；清除查找后恢复此前的格式偏好。</p>{/if}
    <pre class="reply-original"><ConversationText {text} {matches} {selectedIndex} /></pre>
    {#if preview.limited}<p class="secondary-copy">此回复超出格式预览预算，已显示完整的当前原文。</p>{/if}
  {:else}
    <div class="reply-preview" aria-label="回复格式预览">
      {#each preview.blocks as block}
        {#if block.kind === 'heading'}<h3 class="reply-heading">{block.text}</h3>
        {:else if block.kind === 'quote'}<blockquote class="reply-quote">{block.text}</blockquote>
        {:else if block.kind === 'paragraph'}<p class="reply-paragraph">{block.text}</p>
        {:else if block.kind === 'list'}
          <svelte:element this={block.ordered ? 'ol' : 'ul'} class="reply-list">
            {#each block.items as item}<li><span class="reply-marker">{item.marker}</span><span>{item.text}</span></li>{/each}
          </svelte:element>
        {:else if block.kind === 'code'}
          <div class="reply-code"><div class="reply-code-heading"><span>{block.language || '代码'}</span>
            <IrisButton size="sm" variant="ghost" onclick={() => void copy(block.text, '代码')}>复制代码</IrisButton></div>
            <pre><code>{block.text}</code></pre>
            {#if !block.closed}<p class="secondary-copy">代码围栏尚未闭合，当前内容可能仍在生成。</p>{/if}
          </div>
        {/if}
      {/each}
    </div>
  {/if}
  {#if copied}<p class="copy-feedback" role="status" aria-live="polite">{copied}</p>{/if}
</div>
