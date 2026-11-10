<script lang="ts">
  import { onMount } from 'svelte'
  import { IrisButton, IrisIcon, IrisBadge, ThemeProvider } from './lib/iris'
  import { createWorkspace } from './lib/workspace'
  import { theme } from './theme'
  import Sidebar from './components/Sidebar.svelte'
  import ReadState from './components/ReadState.svelte'
  import InstanceList from './components/InstanceList.svelte'
  import SessionList from './components/SessionList.svelte'
  import AccessNotes from './components/AccessNotes.svelte'
  import TaskConsole from './components/TaskConsole.svelte'

  type View = 'overview' | 'instances' | 'sessions' | 'tasks'
  const workspace = createWorkspace()
  let view = $state<View>('overview')
  let search = $state('')
  let dark = $state(false)
  let navigating = $state(false)
  const available = $derived($workspace.phase === 'ready')
  const busy = $derived($workspace.phase === 'loading')
  const term = $derived(search.trim().toLocaleLowerCase())
  const instances = $derived($workspace.instances.filter((row) => [row.instance_id, row.name, row.project_id].some((value) => value.toLocaleLowerCase().includes(term))))
  const sessions = $derived($workspace.sessions.filter((row) => [row.session_id, row.instance_id, row.name, row.project_id].some((value) => value.toLocaleLowerCase().includes(term))))
  const heading = $derived({ overview: '工作台', instances: '授权实例', sessions: '会话记录', tasks: 'Agent 任务' }[view])

  function select(next: View) { view = next; search = ''; location.hash = next; if (next !== 'tasks') void workspace.refresh() }
  function toggleTheme() { dark = !dark; theme.setTheme(dark ? 'dark' : 'light') }
  function beginNavigation(event: SubmitEvent) {
    if (navigating) { event.preventDefault(); return }
    navigating = true
    // Keep the native form connected until its default POST starts.
    // pagehide clears data/requests before the old document can enter BFCache.
  }

  onMount(() => {
    let navigationTimer: ReturnType<typeof setTimeout> | undefined
    const leaving = () => { navigationTimer = setTimeout(() => { navigating = false }, 0) }
    const navigate = () => {
      const name = location.hash.slice(1).split('/')[0]
      if (['overview', 'instances', 'sessions', 'tasks'].includes(name)) {
        if (name !== view && name !== 'tasks') void workspace.refresh()
        view = name as View
      }
    }
    navigate()
    window.addEventListener('hashchange', navigate)
    window.addEventListener('beforeunload', leaving)
    void workspace.refresh()
    const forget = () => workspace.forget()
    const restore = (event: PageTransitionEvent) => {
      if (event.persisted) { navigating = false; workspace.forget(); void workspace.refresh() }
    }
    window.addEventListener('pagehide', forget)
    window.addEventListener('pageshow', restore)
    return () => {
      workspace.dispose()
      window.removeEventListener('pagehide', forget)
      window.removeEventListener('pageshow', restore)
      window.removeEventListener('hashchange', navigate)
      window.removeEventListener('beforeunload', leaving)
      clearTimeout(navigationTimer)
    }
  })
</script>

<svelte:head><title>abr · {heading}</title></svelte:head>
<ThemeProvider store={theme}>
  <a class="skip-link" href="#main">跳到主要内容</a>
  <div class="workbench">
    <Sidebar {view} {select} />
    <div class="workspace-body">
      <header class="topbar">
        <div class="breadcrumb"><span>工作空间</span><span class="breadcrumb-separator" aria-hidden="true"><IrisIcon name="chevron-right" size={14} /></span><strong>{heading}</strong></div>
        <div class="topbar-actions" id="account">
          <IrisButton variant="ghost" size="sm" onclick={toggleTheme} aria-label={dark ? '切换浅色主题' : '切换深色主题'}>
            <IrisIcon name={dark ? 'sun' : 'moon'} size={18} />
          </IrisButton>
          {#if available}
            <form action="/auth/logout" method="post" onsubmit={beginNavigation}>
              <IrisButton type="submit" variant="outline" size="sm" disabled={navigating}>退出本地 abr 会话</IrisButton>
            </form>
          {/if}
          <form action="/auth/login" method="post" onsubmit={beginNavigation}>
            <IrisButton type="submit" size="sm" disabled={navigating}>
              {#snippet leading()}<IrisIcon name="log-in" size={16} />{/snippet}
              使用 Snaplink 登录
            </IrisButton>
          </form>
        </div>
      </header>
      <main id="main" tabindex="-1">
        <div class="page-heading">
          <div><div class="section-eyebrow">ABR / AGENT WORKBENCH</div><h1>{heading}<span class="title-dot">.</span></h1>
            <p>选择项目实例，管理 Agent 任务，核对执行证据。</p></div>
          {#if view !== 'tasks'}<IrisButton onclick={() => select('tasks')}>管理 Agent 任务</IrisButton>{:else}<IrisBadge tone="neutral" variant="outline">按授权操作</IrisBadge>{/if}
        </div>
        <TaskConsole active={view === 'tasks'} contextChanged={() => workspace.forget()} />
        {#if view !== 'tasks'}
        <div class="main-grid">
          <div class="main-column">
            <ReadState snapshot={$workspace} refresh={() => void workspace.refresh()} />
            <section class="records-panel" aria-labelledby="records-title" aria-busy={busy}>
              <div class="records-heading">
                <div><span class="section-eyebrow">AUTHORIZED WINDOW</span><h2 id="records-title">{view === 'overview' ? '你的授权窗口' : heading}</h2></div>
                <IrisButton id="refresh" variant="ghost" size="sm" onclick={() => void workspace.refresh()} disabled={busy} aria-label="刷新授权窗口">
                  <IrisIcon name="refresh" size={17} />
                </IrisButton>
              </div>
              <div class="window-tools">
                <div class="window-counts"><span>实例 <strong>{available ? $workspace.instances.length : '未读取'}</strong></span><span>会话 <strong>{available ? $workspace.sessions.length : '未读取'}</strong></span></div>
                {#if view !== 'overview'}
                  <label class="search"><IrisIcon name="search" size={16} /><input type="search" bind:value={search} maxlength="256" placeholder={view === 'instances' ? '筛选实例、项目…' : '筛选会话、实例…'} aria-label="筛选已读取窗口" disabled={!available} /></label>
                {/if}
              </div>
              {#if view !== 'sessions'}
                <div class="subheading"><h3>实例</h3>{#if view === 'overview'}<button class="text-link" onclick={() => select('instances')}>查看实例窗口 <IrisIcon name="arrow-right" size={14} /></button>{/if}</div>
                <InstanceList rows={instances} {available} filtered={!!term} />
              {/if}
              {#if view !== 'instances'}
                <div class="subheading"><h3>会话</h3>{#if view === 'overview'}<button class="text-link" onclick={() => select('sessions')}>查看会话窗口 <IrisIcon name="arrow-right" size={14} /></button>{/if}</div>
                <SessionList rows={sessions} {available} filtered={!!term} />
              {/if}
              <p class="window-footnote">来源：Hub 只读 API · 每类最多 100 条 · 刷新会先清除旧数据并重新鉴权</p>
            </section>
          </div>
          <AccessNotes {available} />
        </div>
        {/if}
        <footer class="page-footer"><span>abr <span>·</span> 你的项目，你的任务工作台</span><span>任务在授权项目中执行；结果需结合项目验证核对。</span></footer>
      </main>
    </div>
  </div>
</ThemeProvider>
