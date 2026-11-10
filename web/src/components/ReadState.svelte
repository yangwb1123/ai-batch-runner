<script lang="ts">
  import { IrisBadge, IrisButton, IrisIcon } from '../lib/iris'
  import { problems, localTime } from '../lib/presentation'
  import type { Snapshot } from '../lib/workspace'
  let { snapshot, refresh }: { snapshot: Snapshot; refresh: () => void } = $props()
  const ready = $derived(snapshot.phase === 'ready')
  const loading = $derived(snapshot.phase === 'loading' || snapshot.phase === 'idle')
  const problem = $derived(snapshot.problem ? problems[snapshot.problem] : null)
  const empty = $derived(ready && !snapshot.instances.length && !snapshot.sessions.length)
</script>

<section class="read-state" aria-labelledby="read-title" aria-busy={loading}>
  <div class="read-state-copy">
    <div class="eyebrow"><span class="eyebrow-rule"></span> YOUR AGENT WORKSPACE</div>
    <div class="hero-badge"><IrisBadge tone={ready ? 'success' : 'neutral'} variant="subtle">
      {ready ? '本次读取已授权' : loading ? '正在核验读取权限' : snapshot.problem === 'login' ? '需要登录' : '读取尚未完成'}
    </IrisBadge></div>
    <h2 id="read-title">{problem?.title ?? (empty ? '登录已验证，等待你的工作现场。' : ready ? '你的授权工作现场，已更新。' : '正在读取你的授权工作现场…')}</h2>
    <p id="notice" role="status" aria-live="polite">{problem?.detail ?? (ready
      ? `已读取授权窗口：实例 ${snapshot.instances.length}，会话 ${snapshot.sessions.length}。每类最多 100 条，不代表完整清单。`
      : '正在重新鉴权读取；旧记录已清除。')}</p>
    {#if empty}<p class="secondary-copy">空窗口不代表项目已经接入。请确认 Gateway 已登记，并且实例在你的授权范围内。</p>{/if}
    <div class="hero-actions">
      {#if snapshot.problem === 'login'}
        <a class="text-link" href="#account">前往 Snaplink 登录 <IrisIcon name="arrow-right" size={16} /></a>
      {:else}
        <IrisButton variant="outline" size="sm" onclick={refresh} loading={loading}>
          {#snippet leading()}<IrisIcon name="refresh" size={15} />{/snippet}
          {ready ? '刷新授权窗口' : '重新读取'}
        </IrisButton>
      {/if}
      <span class="read-time">本地时间 · {localTime(snapshot.readAt, true)}</span>
    </div>
  </div>
  <div class="hero-illustration" aria-hidden="true">
    <div class="orbit orbit-one"></div><div class="orbit orbit-two"></div>
    <div class="orbit-node node-one"><IrisIcon name="folder" size={22} /></div>
    <div class="orbit-node node-two"><IrisIcon name="shield" size={20} /></div>
    <div class="hero-symbol">a<span>·</span></div>
    <div class="orbit-label">明确目标 · 核验现场</div>
  </div>
</section>
