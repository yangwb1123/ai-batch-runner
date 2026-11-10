<script lang="ts">
  import { IrisButton } from '../lib/iris'
  import type { FindResult } from '../lib/conversation-find'
  let { value = $bindable(''), result, selectedIndex, readable, problem, updated, incomplete, more, trimmed,
    search, move, clear }: { value: string; result: FindResult; selectedIndex: number; readable: boolean;
    problem: string; updated: boolean; incomplete: boolean; more: boolean; trimmed: boolean;
    search: () => void; move: (direction: number) => void; clear: () => void } = $props()
  const inputId = $props.id()
</script>

<form class="conversation-find" role="search" aria-label="本轮对话查找" onsubmit={(event) => { event.preventDefault(); search() }}>
  <div class="conversation-find-query">
    <label for={inputId}>查找本轮对话<input id={inputId} type="search" bind:value maxlength="512" autocomplete="off" spellcheck="false"
      disabled={!readable} placeholder="输入要定位的文字"
      onkeydown={(event) => { if (event.key === 'Escape') { event.preventDefault(); clear() } }} /></label>
    <IrisButton type="submit" size="sm" variant="outline" disabled={!readable}>查找</IrisButton>
  </div>
  <div class="conversation-find-actions">
    <IrisButton type="button" size="sm" variant="outline" disabled={!readable || !result.matches.length} onclick={() => move(-1)}>上一处</IrisButton>
    <IrisButton type="button" size="sm" variant="outline" disabled={!readable || !result.matches.length} onclick={() => move(1)}>下一处</IrisButton>
    <IrisButton type="button" size="sm" variant="ghost" disabled={!value && !result.query && !problem} onclick={clear}>清除查找</IrisButton>
    <span class="conversation-find-count" role="status" aria-live="polite">{#if result.query}{#if !result.matches.length}当前已读对话中未找到匹配；未读取部分是否包含该词未知。
      {:else if selectedIndex >= 0}第 {selectedIndex + 1} / {result.matches.length} 处
      {:else}找到 {result.matches.length} 处，尚未定位{/if}{/if}</span>
  </div>
  <p class="secondary-copy">仅查找当前已显示的指令与回复，最多 64 条、256 KiB 回复；不会自动读取或翻页。关键词最多 128 个字符、512 字节，按字面查找且不区分大小写。</p>
  {#if result.query && value !== result.query}<p class="secondary-copy">已修改关键词，点击“查找”后更新定位。</p>{/if}
  {#if result.limited}<p class="task-notice" role="status">最多显示前 200 处匹配，请缩小关键词范围。</p>{/if}
  {#if updated && result.query}<p class="secondary-copy">内容已更新，原定位已失效；请用上一处或下一处重新定位。</p>{/if}
  {#if trimmed}<p class="secondary-copy">较早事件未保留在当前窗口，查找不覆盖这些内容。</p>{/if}
  {#if more}<p class="secondary-copy">尚有未读取事件，查找不会自动读取。</p>{/if}
  {#if incomplete}<p class="secondary-copy">当前指令或回复含截断、缺片或展示限制，匹配结果不代表完整对话。</p>{/if}
  {#if problem}<p class="task-notice" role="alert">{problem}</p>{/if}
</form>
