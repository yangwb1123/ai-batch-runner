<script lang="ts">
  import { IrisIcon } from '../lib/iris'
  let { view, select }: { view: string; select: (view: 'overview' | 'instances' | 'sessions' | 'tasks') => void } = $props()
  const links = [
    { id: 'overview', label: '工作台', icon: 'grid', number: '01' },
    { id: 'instances', label: '授权实例', icon: 'folder', number: '02' },
    { id: 'sessions', label: '会话记录', icon: 'inbox', number: '03' },
    { id: 'tasks', label: 'Agent 任务', icon: 'send', number: '04' },
  ] as const
</script>

<aside class="sidebar" aria-label="工作台侧栏">
  <a class="brand" href="/" aria-label="abr 工作台首页">
    <span class="brand-mark" aria-hidden="true">a<span>·</span></span>
    <span class="brand-word">abr<span>Agent Workbench</span></span>
  </a>
  <div class="nav-caption">你的工作空间</div>
  <nav aria-label="工作台导航">
    {#each links as link}
      <button class:active={view === link.id} aria-current={view === link.id ? 'page' : undefined}
        onclick={() => select(link.id)} title={link.label} aria-label={link.label}>
        <IrisIcon name={link.icon} size={19} />
        <span>{link.label}</span><small aria-hidden="true">{link.number}</small>
      </button>
    {/each}
  </nav>
  <div class="sidebar-bottom">
    <div class="scope-note"><IrisIcon name="shield" size={19} /><span>明确授权，不越界<small>每次请求都重新核验权限</small></span></div>
    <span class="built-with">IRIS UI <span>×</span> SVELTE</span>
  </div>
</aside>
