<script lang="ts">
  import { localTime } from '../lib/presentation'
  import { reviewCategoryLabel, reviewStatusLabel, type ReviewRecord } from '../lib/result-review'
  import CommandOutput from './CommandOutput.svelte'
  let { record, scope, disabled }: { record: ReviewRecord; scope: string; disabled: boolean } = $props()
  const source = $derived(record.source)
</script>

<section class="review-record" aria-label="选中结果记录">
  <div class="result-row-heading"><h5>{record.title}</h5><span class="evidence-state" data-state={record.status}>{reviewStatusLabel(record.status)}</span></div>
  <p class="secondary-copy">{reviewCategoryLabel(record.category)}{record.partial ? ' · 记录不完整，需继续核对' : ''}</p>
  {#if source}
    <p class="secondary-copy">下面是同一记录的组合展示；命令、状态与输出可能来自不同事件。对应来源单独列出。</p>
    <dl class="review-metadata"><dt>最近时间</dt><dd>{localTime(source.created_at)}</dd><dt>记录类型</dt><dd>{reviewCategoryLabel(record.category)}</dd>
      {#if source.item_id}<dt>记录 ID</dt><dd><code>{source.item_id}</code></dd>{/if}
      {#if source.exit_code !== undefined}<dt>记录退出码</dt><dd>{source.exit_code === null ? '未提供' : source.exit_code}</dd>{/if}</dl>
    <details class="review-source-events"><summary>查看组成记录的事件（{record.sources.length} 条）</summary>
      <p class="secondary-copy">展示当前窗口中最近 {Math.min(record.sources.length, 8)} 条来源，其余已读取事件保留在轮次记录下载中。</p>
      <ul>{#each record.sources.slice(-8) as event (event.event_id)}<li><code>{event.event_id}</code><span> · #{event.cursor} · {event.kind}</span>
        {#if event.exit_code !== undefined}<span> · 退出码 {event.exit_code === null ? '未提供' : event.exit_code}</span>{/if}</li>{/each}</ul>
    </details>
    {#if source.text}<pre class="review-record-text">{source.text}</pre>{/if}
    {#if source.reason && source.reason !== source.text}<p class="result-reason">{source.reason}</p>{/if}
    {#if source.paths?.length}<ul class="review-paths">{#each source.paths as path}<li><code>{path}</code></li>{/each}</ul>{/if}
    {#if record.category === 'artifact'}<dl class="review-metadata"><dt>SHA-256</dt><dd><code>{source.sha256 || '未提供'}</code></dd>
      <dt>大小</dt><dd>{source.size_bytes === undefined ? '未提供' : `${source.size_bytes} 字节`}</dd><dt>类型</dt><dd>{source.media_type || '未提供'}</dd></dl>{/if}
    {#if record.category === 'command'}<CommandOutput text={source.output_text} truncated={source.output_truncated} scope={`${scope}/${record.key}`} {disabled} />{/if}
  {:else}<p class="secondary-copy">来源为 Hub 当前轮次状态；当前窗口没有对应的执行异常事件，不能补造异常原因。</p>{/if}
</section>
