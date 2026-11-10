<script lang="ts">
  import { IrisBadge, IrisIcon } from '../lib/iris'
  import { displayText, type Session } from '../lib/workspace'
  import { sessionLabel, localTime } from '../lib/presentation'
  let { rows, available, filtered = false }: { rows: Session[]; available: boolean; filtered?: boolean } = $props()
</script>

{#if available && rows.length}
  <div class="table-scroll" role="region" aria-label="会话记录表格">
    <table>
      <thead><tr><th scope="col">会话 / 标识</th><th scope="col">所属实例</th><th scope="col">记录状态</th><th scope="col">更新时间</th></tr></thead>
      <tbody id="sessions">
        {#each rows as row (row.session_id)}
          <tr>
            <td><strong class="record-name">{displayText(row.name, '未声明名称')}</strong><span class="record-meta">{row.session_id}</span></td>
            <td>{row.instance_id}</td>
            <td><IrisBadge tone={row.status === 'failed' ? 'danger' : row.status === 'running' ? 'primary' : 'neutral'} variant="subtle">{sessionLabel(row.status)}</IrisBadge></td>
            <td class="time-cell">{row.updated_at === null ? '未追踪' : localTime(row.updated_at)}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
{:else}
  <div class="list-empty" id="sessions">
    <span class="empty-icon"><IrisIcon name={available ? 'inbox' : 'lock'} size={24} /></span>
    <h3>{available ? filtered ? '没有匹配的会话' : '授权窗口暂无会话' : '会话数据尚不可用'}</h3>
    <p>{available ? filtered ? '试试会话名称、ID 或所属实例。不会扩大服务端授权范围。' : '在线实例不一定有登记会话。请核对 Gateway 与当前授权范围。' : '完成登录后可以查看授权会话；本页面不提供发送指令或取消任务。'}</p>
  </div>
{/if}
