<script lang="ts">
  import { IrisButton, IrisIcon } from '../lib/iris'
  import type { Detail } from '../lib/task-models'
  import type { Tasks } from '../lib/task-state'
  import type { DraftTarget } from '../lib/draft-file'
  import { MAX_DRAFT_BYTES } from '../lib/drafts'
  import DraftFiles from './DraftFiles.svelte'
  let { detail, snapshot, prompt, locked, write, change, send, backupDraft, appendDraftFile }: { detail: Detail; snapshot: Tasks; prompt: string; locked: boolean; write: boolean;
    change: (text: string) => boolean; send: () => Promise<void>;
    backupDraft: (target: DraftTarget) => { text: string; filename: string };
    appendDraftFile: (target: DraftTarget, text: string) => string } = $props()
  const bytes = $derived(new TextEncoder().encode(prompt).length)
  const canSend = $derived(detail.online && detail.controllable && !detail.frozen && !detail.active_turn_id && detail.status !== 'closed' && write && !locked)
  const reason = $derived(!write ? '当前账号只有读取权限。' : detail.status === 'closed' ? '此会话已关闭，可以创建新任务。' : !detail.online ? '项目实例离线，恢复连接后可继续。' : detail.frozen ? '会话结果需先核对，当前不能继续执行。' : detail.active_turn_id ? '本轮正在执行。你可以先起草下一条指令，完成后发送。' : locked ? '正在提交或核对请求。' : !detail.controllable ? '此会话不能由当前 Gateway 控制。' : '')
  function keyboard(event: KeyboardEvent) {
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter' && !event.isComposing && canSend && prompt.trim() && bytes <= 16384) {
      event.preventDefault()
      void send()
    }
  }
</script>

<form class="task-composer" onsubmit={(event) => { event.preventDefault(); if (canSend && prompt.trim() && bytes <= 16384) void send() }}>
  <label class="task-field" for="task-prompt">发送下一条指令<textarea id="task-prompt" value={prompt} oninput={(event) => { if (!change(event.currentTarget.value)) event.currentTarget.value = prompt }} onkeydown={keyboard}
    maxlength={MAX_DRAFT_BYTES} rows="4" disabled={!write || detail.status === 'closed'} placeholder="描述目标、范围和验收要求。可以持续追问或根据结果调整任务。"></textarea></label>
  {#if reason}<p class="composer-reason">{reason}</p>{/if}
  <div class="task-actions"><span class="secondary-copy">草稿 {bytes} / {MAX_DRAFT_BYTES} 字节 · Ctrl / ⌘ + Enter 发送</span><IrisButton type="submit" disabled={!canSend || !prompt.trim() || bytes > 16384}><IrisIcon name="send" size={16} />发送指令</IrisButton></div>
  {#if bytes > 16384}<p role="alert" class="composer-reason">单次发送最多 16384 字节，请精简后发送。当前草稿仍保留，可保存最多 65536 字节的草稿文本。</p>{/if}
  <DraftFiles {snapshot} {prompt} backup={backupDraft} append={appendDraftFile} />
</form>
