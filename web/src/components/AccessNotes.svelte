<script lang="ts">
  import { IrisCard, IrisIcon, IrisBadge } from '../lib/iris'
  let { available }: { available: boolean } = $props()
</script>

<aside class="access-notes" id="access" aria-label="接入与证据边界">
  <IrisCard variant="outline" padding="lg" style={undefined} header={undefined} footer={undefined}>
    <div class="section-eyebrow">ACCESS, WITH BOUNDARIES</div>
    <h2 class="aside-title">从身份，到工作现场</h2>
    <ol class="access-steps">
      <li><span class="step-icon"><IrisIcon name="user" size={17} /></span><div><strong>用 Snaplink 确认身份</strong><p>沿用现有账号，密码不交给 ABR。</p></div></li>
      <li><span class="step-icon"><IrisIcon name="shield" size={17} /></span><div><strong>由 Hub 核验访问范围</strong><p>登录不自动授予项目权限；任务写入需本人精确授权。</p><IrisBadge size="sm" tone={available ? 'success' : 'neutral'}>{available ? '本次读取已通过' : '等待读取核验'}</IrisBadge></div></li>
      <li><span class="step-icon"><IrisIcon name="folder" size={17} /></span><div><strong>进入 Agent 任务管理</strong><p>选择在线执行型 Gateway，创建会话、发送指令并核对结果。仅观察模式不能执行。</p></div></li>
    </ol>
  </IrisCard>
  <div class="evidence-note">
    <IrisIcon name="info" size={18} />
    <div><strong>看见状态，不等于完成验收</strong><p>在线仅为心跳推断。执行完成不等于目标达成，也不代表发布或生产授权。</p>
      <dl><div><dt>费用</dt><dd>未追踪</dd></div><div><dt>独立验收</dt><dd>未追踪</dd></div><div><dt>审计入账</dt><dd>未追踪</dd></div></dl>
    </div>
  </div>
</aside>
