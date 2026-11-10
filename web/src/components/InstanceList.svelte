<script lang="ts">
  import { IrisBadge, IrisIcon } from '../lib/iris'
  import { displayText, type Instance } from '../lib/workspace'
  import { heartbeat, localTime } from '../lib/presentation'
  let { rows, available, filtered = false }: { rows: Instance[]; available: boolean; filtered?: boolean } = $props()
</script>

{#if available && rows.length}
  <div class="table-scroll" role="region" aria-label="授权实例表格">
    <table>
      <thead><tr><th scope="col">实例 / 声明名称</th><th scope="col">项目</th><th scope="col">心跳推断</th><th scope="col">最近心跳</th></tr></thead>
      <tbody id="instances">
        {#each rows as row (row.instance_id)}
          <tr>
            <td><strong class="record-id">{row.instance_id}</strong><span class="record-meta">{displayText(row.name, '未声明名称')}</span></td>
            <td>{displayText(row.project_id)}</td>
            <td><IrisBadge tone={row.online === true ? 'success' : 'neutral'} variant="subtle">{heartbeat(row.online)}</IrisBadge></td>
            <td class="time-cell">{row.last_seen === null ? '未追踪' : localTime(row.last_seen)}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
{:else}
  <div class="list-empty" id="instances">
    <span class="empty-icon"><IrisIcon name={available ? 'folder' : 'lock'} size={24} /></span>
    <h3>{available ? filtered ? '没有匹配的实例' : '授权窗口暂无实例' : '实例数据尚不可用'}</h3>
    <p>{available ? filtered ? '试试实例名称、ID 或项目标识。筛选只作用于已读取窗口。' : '请确认 Gateway 已登记且与你的账号授权匹配；这里不会自动接入项目。' : '先完成登录与授权核验。无法读取时，不会显示缓存的旧实例。'}</p>
  </div>
{/if}
