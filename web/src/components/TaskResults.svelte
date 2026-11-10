<script lang="ts">
  import { IrisButton } from '../lib/iris'
  import { commandLabel, evidenceLabel, projectResults } from '../lib/task-results'
  import { localTime, sessionLabel } from '../lib/presentation'
  import type { Tasks } from '../lib/task-state'
  import CommandOutput from './CommandOutput.svelte'
  import FileSnapshots from './FileSnapshots.svelte'
  import FileComparison from './FileComparison.svelte'
  import ResultReview from './ResultReview.svelte'
  import ResultDownloads, { type DownloadTurn } from './ResultDownloads.svelte'
  import type { DraftReference } from '../lib/result-followup'
  import '../results.css'
  let { snapshot, load, append, downloadTurn }: { snapshot: Tasks; load: () => Promise<void>;
    append: (reference: DraftReference, text: string) => Promise<string>; downloadTurn: DownloadTurn } = $props()
  const view = $derived(projectResults(snapshot))
  const downloadDisabled = $derived(!view || snapshot.busy || snapshot.refreshing || snapshot.syncStopped || snapshot.readAt === null)
  let identity = ''
  let fileEpoch = $state(0), fileNotice = $state('')
  function denyFiles(problem: string) { fileEpoch++; fileNotice = problem }
  $effect(() => {
    const next = `${snapshot.contextEpoch}/${snapshot.selectedSession}/${snapshot.selectedTurn}`
    if (identity !== next || !snapshot.access?.task_access) { identity = next; fileNotice = '' }
  })
</script>

<div class="task-results">
  {#if view}
    <header class="results-heading"><div><h3>本轮结果检查</h3><p>执行端记录与当前读取窗口，供你核对任务结果。</p></div>
      <ResultDownloads {snapshot} download={downloadTurn} /></header>
    <dl class="results-overview"><div><dt>执行状态</dt><dd>{sessionLabel(view.turn.state)}</dd></div><div><dt>目标验收</dt><dd>未独立验证</dd></div>
      <div><dt>最近同步</dt><dd>{localTime(snapshot.readAt, true)}</dd></div><div><dt>轮次</dt><dd>{view.turn.turn_id}</dd></div></dl>
    <p class="secondary-copy">命令退出码、验证和门禁结论来自执行端事件。回复文字不作为验收证据；产物元数据与文件路径不证明文件内容已检查。</p>
    {#if view.partial}<p class="task-notice" role="status">当前记录不完整：存在尚未读取、已截断或缺失的证据。不能据此认定本轮已完整验收。</p>{/if}
    {#if snapshot.eventsTrimmed}<p class="secondary-copy">较早事件已超出浏览器保留窗口。</p>{/if}
    {#if snapshot.eventsMore}<IrisButton size="sm" variant="outline" disabled={snapshot.busy || snapshot.refreshing} onclick={() => void load()}>继续读取结果记录</IrisButton>{/if}
    <ResultReview {snapshot} {append} />
    <section class="results-section" aria-label="命令记录"><h4>命令记录 <span>{view.commands.length}</span></h4>
      {#if view.commands.length}<ul>{#each view.commands.slice(0, 64) as item (item.key)}<li><div class="result-row-heading"><strong>{commandLabel(item)}</strong><span>{localTime(item.created_at)}</span></div>
        <pre>{item.text || '未提供命令正文'}</pre>
        <CommandOutput text={item.output_text} truncated={item.output_truncated} scope={`${snapshot.contextEpoch}/${snapshot.selectedSession}/${snapshot.selectedTurn}/${item.key}`} disabled={downloadDisabled} />
        {#if item.truncated || item.evidence_partial}<p class="secondary-copy">此命令记录不完整。</p>{/if}</li>{/each}</ul>
      {:else}<p class="result-empty">当前已读取窗口没有命令事件，不能据此判断未执行命令。</p>{/if}
      {#if view.commands.length > 64}<p class="secondary-copy">展示前 64 条；下载当前记录可核对其余已读取命令。</p>{/if}
    </section>
    <FileSnapshots {snapshot} {fileEpoch} notice={fileNotice} denied={denyFiles} />
    <FileComparison {snapshot} {fileEpoch} notice={fileNotice} denied={denyFiles} />
    <section class="results-section" aria-label="文件变更记录"><h4>报告的文件变更 <span>{view.files.length}</span></h4>
      {#if view.files.length}<ul>{#each view.files.slice(0, 128) as path}<li><code>{path}</code></li>{/each}</ul>
      {:else}<p class="result-empty">没有可显示的变更路径，文件实际状态未由网页读取。</p>{/if}
      {#if view.changes.some((item) => item.truncated || !item.paths?.length)}<p class="secondary-copy">部分变更事件未提供完整路径。</p>{/if}
      {#if view.files.length > 128}<p class="secondary-copy">展示前 128 个路径；下载当前记录可核对其余已读取路径。</p>{/if}
    </section>
    {#each [{ title: '验证记录', rows: view.validations }, { title: '门禁记录', rows: view.gates }] as group}
      <section class="results-section" aria-label={group.title}><h4>{group.title} <span>{group.rows.length}</span></h4>
        {#if group.rows.length}<ul>{#each group.rows.slice(0, 64) as item (item.key)}<li><div class="result-row-heading"><strong>{item.text || '未提供名称'}</strong><span class="evidence-state" data-state={item.state}>{evidenceLabel(item.state)}</span></div>
          {#if item.exit_code !== undefined}<p class="secondary-copy">退出码：{item.exit_code === null ? '未提供' : item.exit_code}</p>{/if}
          {#if item.reason}<p class="result-reason">{item.reason}</p>{/if}{#if item.truncated || item.evidence_partial}<p class="secondary-copy">此证据记录不完整。</p>{/if}</li>{/each}</ul>
        {:else}<p class="result-empty">未提供{group.title}。普通命令和回复中的“通过”不会代替此记录。</p>{/if}
        {#if group.rows.length > 64}<p class="secondary-copy">展示前 64 条；其余已读取记录保留在下载文件中。</p>{/if}
      </section>
    {/each}
    <section class="results-section" aria-label="产物记录"><h4>产物元数据 <span>{view.artifacts.length}</span></h4>
      {#if view.artifacts.length}<ul>{#each view.artifacts.slice(0, 64) as item (item.key)}<li><div class="result-row-heading"><strong>{item.text || '未提供产物名称'}</strong><span>{evidenceLabel(item.state)}</span></div>
        {#each item.paths ?? [] as path}<p><code>{path}</code></p>{/each}<dl class="artifact-metadata"><dt>SHA-256</dt><dd><code>{item.sha256 || '未提供'}</code></dd>
          <dt>大小</dt><dd>{item.size_bytes === undefined ? '未提供' : `${item.size_bytes} 字节`}</dd><dt>类型</dt><dd>{item.media_type || '未提供'}</dd></dl>
        {#if item.truncated || item.evidence_partial}<p class="secondary-copy">此产物元数据不完整，文件内容未读取。</p>{/if}</li>{/each}</ul>
      {:else}<p class="result-empty">没有已记录的产物元数据。回复中的文件名不会自动成为交付文件。</p>{/if}
      {#if view.artifacts.length > 64}<p class="secondary-copy">展示前 64 条；其余已读取元数据保留在下载文件中。</p>{/if}
    </section>
    {#if view.diagnostics.length}<section class="results-section" aria-label="执行异常"><h4>执行异常</h4><ul>{#each view.diagnostics.slice(0, 64) as item (item.event_id)}<li><p class="result-reason">{item.text || item.reason || sessionLabel(view.turn.state)}</p></li>{/each}</ul></section>{/if}
  {:else}<p class="result-empty">选择一个已授权的执行轮次后，可在这里核对当前已读取的结果。</p>{/if}
</div>
