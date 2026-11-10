<script lang="ts">
  import { IrisButton } from '../lib/iris'
  import type { Comparison } from '../lib/file-comparison'
  import { visibleFileText } from '../lib/file-snapshots'
  let { comparison, disabled, download }: { comparison: Comparison; disabled: boolean; download: () => void } = $props()
  let expanded = $state(false)
  const result = $derived(comparison.result)
  const groups = $derived(result.state === 'changed' ? expanded ? [{ start: 0, end: result.rows.length }] : result.hunks : [])
  const limitReason = { bytes: '单侧超过 64 KiB', lines: '单侧超过 2000 行', work: '超过有界计算预算', invalid_text: '文本编码无效' }
  $effect(() => { comparison; expanded = false })
</script>

<section class="file-diff" aria-label="文件差异">
  <div class="result-row-heading"><h5>{comparison.path}</h5>
    <IrisButton size="sm" variant="outline" {disabled} onclick={download}>下载差异记录</IrisButton></div>
  <p class="comparison-status">{comparison.byte_equal ? '字节相同' : '字节不同'} · 双侧 SHA-256 与字节数已核对</p>
  <details><summary>核对双方快照标识与摘要</summary>
    {#each [{ label: '基准', file: comparison.before }, { label: '当前', file: comparison.after }] as side}
      <p class="secondary-copy">{side.label} · {side.file.size_bytes} 字节 · <code>{side.file.turn_id}</code><br />快照 <code>{side.file.snapshot_id}</code><br />SHA-256 <code>{side.file.sha256}</code></p>
    {/each}
  </details>
  {#if result.state === 'binary'}<p class="result-empty">包含二进制文件，仅比较原始字节摘要；不生成文本差异。原始文件可在相应轮次的文件检查快照中下载。</p>
  {:else}
    {#each [{ label: '基准', meta: result.before }, { label: '当前', meta: result.after }] as side}
      {#if side.meta}<p class="secondary-copy">{side.label}：{side.meta.lines} 行 · BOM {side.meta.bom ? '有' : '无'} · LF {side.meta.endings.LF} / CRLF {side.meta.endings.CRLF} / CR {side.meta.endings.CR} · {side.meta.finalNewline ? '末尾有换行' : '末尾无换行'}</p>{/if}
    {/each}
    {#if result.state === 'limited'}<p class="task-notice" role="status">未完成逐行比较：{limitReason[result.reason]}。字节状态已核对，不能据此认定没有行变化；下载记录会保留此限制。</p>
    {:else if result.state === 'equal'}<p class="result-empty">文本与行尾完全相同。</p>
    {:else}
      <p class="comparison-status">增加 {result.added} 行 · 移除 {result.removed} 行</p>
      <label class="comparison-context"><input type="checkbox" bind:checked={expanded} /> 显示全部上下文</label>
      <p class="secondary-copy">左列为基准行号，右列为当前行号；− 为移除，+ 为增加。默认保留变化前后各 3 行。每行标注原始行尾；格式控制字符与反斜杠转义显示，下载保留原始文本。</p>
      <div class="diff-lines" role="region" aria-label="逐行差异">
        {#each groups as group, index}
          {#if group.start > (groups[index - 1]?.end ?? 0)}<p class="diff-gap">省略 {group.start - (groups[index - 1]?.end ?? 0)} 行未变上下文</p>{/if}
          {#each result.rows.slice(group.start, group.end) as row}
            <div class="diff-line" data-kind={row.kind}><span class="diff-number">{row.beforeLine ?? ''}</span><span class="diff-number">{row.afterLine ?? ''}</span>
              <span class="diff-sign">{row.kind === 'add' ? '+' : row.kind === 'remove' ? '−' : ' '}</span><code>{visibleFileText(row.text)}<small class="diff-ending"> [{row.ending === 'none' ? '无行尾' : row.ending}]</small></code></div>
          {/each}
        {/each}
        {#if groups.length && groups[groups.length - 1].end < result.rows.length}<p class="diff-gap">省略 {result.rows.length - groups[groups.length - 1].end} 行未变上下文</p>{/if}
      </div>
    {/if}
  {/if}
  <p class="secondary-copy">差异仅表示这两个不可变快照的内容关系，不代表目标验收或可直接应用的补丁。</p>
</section>
