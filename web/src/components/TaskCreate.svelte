<script lang="ts">
  import { onMount } from 'svelte'
  import { IrisButton, IrisIcon } from '../lib/iris'
  import { heartbeat } from '../lib/presentation'
  import type { Instance } from '../lib/api'
  let { instances, initial, disabled, writable, create, dismiss }: { instances: Instance[]; initial: string;
    disabled: boolean; writable: boolean; create: (instance: string, name: string) => Promise<boolean>; dismiss: () => void } = $props()
  let instance = $state(''), name = $state('')
  onMount(() => { instance = initial })
  const target = $derived(instances.find((row) => row.instance_id === instance))
  const available = $derived(writable && target?.online === true && (target.session_capacity ?? 0) > 0 && !disabled)
  async function submit() { if (available && name.trim() && await create(instance, name)) dismiss() }
</script>

<section class="task-create-panel" aria-labelledby="create-heading">
  <div class="task-list-heading"><div><span class="section-eyebrow">NEW TASK</span><h2 id="create-heading">创建项目任务</h2></div><IrisButton variant="ghost" size="sm" onclick={dismiss} disabled={disabled} aria-label="收起创建任务"><IrisIcon name="x" size={18} /></IrisButton></div>
  <p>为一项工作建立独立会话。创建后直接进入对话，发送目标并持续跟踪执行。</p>
  <form class="task-create" onsubmit={(event) => { event.preventDefault(); void submit() }}>
    <label class="task-field" for="task-instance">目标实例 / 固定项目<select id="task-instance" bind:value={instance} disabled={disabled}>
      <option value="">请选择项目实例</option>{#each instances as row (row.instance_id)}<option value={row.instance_id}>{row.name || row.instance_id} · {row.project_id || '项目未声明'} · {heartbeat(row.online)}</option>{/each}
    </select></label>
    <label class="task-field" for="task-name">任务名称<input id="task-name" bind:value={name} maxlength="256" disabled={!available} placeholder="例如：修复登录失败并补回归" /></label>
    <IrisButton type="submit" disabled={!available || !name.trim()}>创建任务</IrisButton>
  </form>
  {#if !writable}<p class="task-notice">当前账号没有创建任务权限。请联系项目管理员开通任务写权限。</p>
  {:else if target && !available}<p class="task-notice">此实例暂时无法创建任务。请确认在线、支持执行且有可用会话容量。</p>{/if}
  {#if !instances.length}<p class="task-notice">尚无授权项目。请由项目管理员登记固定工作区的 Gateway，并授予你的账号访问权限。</p>{/if}
</section>
