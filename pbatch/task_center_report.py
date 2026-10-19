"""Responsive global task view. Data is fetched as JSON, never embedded as HTML."""
from __future__ import annotations

from .task_center_intent_report import INTENT_SCRIPT
from .task_center_attention_report import ATTENTION_SCRIPT


_PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>全局任务中心 · pi-batch</title>
<style>
body{font-family:system-ui,sans-serif;margin:1rem;color:#222;max-width:1400px}
h1{font-size:1.4rem} .note{color:#555} #error{color:#a21}
.table-wrap{overflow:auto} table{border-collapse:collapse;width:100%;font-size:.9rem}
td,th{padding:.6rem;border-bottom:1px solid #ddd;text-align:left;vertical-align:top}
button,select{font:inherit;padding:.5rem;margin:.25rem} .bad{background:#fff3f3}
pre{background:#f5f5f5;padding:1rem;white-space:pre-wrap;overflow-wrap:anywhere}
.intent-hint{border-bottom:1px solid #ddd;padding:.5rem} #intent-detail p{white-space:pre-wrap;overflow-wrap:anywhere}
summary{cursor:pointer;padding:.5rem} #intents td{overflow-wrap:anywhere}
@media(max-width:600px){body{margin:.5rem}td,th{min-width:6rem}}
</style></head><body>
<h1>全局任务中心 · 只读</h1>
<p class="note">当前显示账本事实，不将模型自述当成验收。不启动 Agent，不删除现场，不上报数据。</p>
<label>项目 <select id="source"><option value="">所有已接入项目</option></select></label>
<button id="refresh">刷新</button><span id="summary"></span>
<p id="error" role="status"></p>
<h2>需关注事实（不是干预授权）</h2>
<p class="note">仅来自已接入的有界记录；不是实时预检、根因或必做操作。静默不算故障，未知不是零；未连接通知/执行器。</p>
<p id="attention-summary" role="status">等待新快照。</p>
<div class="table-wrap"><table><thead><tr><th>项目 / 来源</th><th>完整记录引用</th>
<th>观察事实</th><th>详情</th></tr></thead><tbody id="attention"></tbody></table></div>
<div class="table-wrap"><table><thead><tr><th>项目 / 类型</th><th>任务</th>
<th>最近状态 / 步骤</th><th>时长</th><th>模型配置</th><th>Token / 费用</th>
<th>产物 / 验收</th><th>详情</th></tr></thead><tbody id="tasks"></tbody></table></div>
<h2>明确登记的目标与验收记录</h2>
<p class="note">记录的操作人身份未认证；证据字节匹配不等于测试或语义独立验证。</p>
<div class="table-wrap"><table><thead><tr><th>项目 / 目标</th><th>标准 / 记录</th>
<th>最近步骤 / 声明下一步</th><th>关联消耗（窗口）</th><th>详情</th></tr></thead><tbody id="goals"></tbody></table></div>
<h2>授权历史的意图线索（候选推断，不是执行授权）</h2>
<p class="note">只展示显式授权的有界、脱敏线索；原生分支/用户范围不是全历史。角色与署名未认证，原始正文不回传。</p>
<div class="table-wrap"><table><thead><tr><th>目标 / 角色通道</th><th>授权 / 覆盖</th>
<th>当前线索</th><th>详情</th></tr></thead><tbody id="intents"></tbody></table></div>
<h2>恢复请求登记（不是执行授权）</h2>
<p class="note">限制由本地操作人声明，未核验原截止时间/预算，未连接执行器；署名未认证。</p>
<div class="table-wrap"><table><thead><tr><th>项目 / 请求</th><th>绑定阶段 / 状态</th>
<th>声明限制（不等于剩余预算）</th><th>只读检查 / 阻塞</th><th>详情</th></tr></thead><tbody id="repairs"></tbody></table></div>
<h2>显式登记的资源与保留计划</h2>
<p class="note">只统计登记资源，不是磁盘全量体检。72 小时到期只触发复核，所有计划仍为保留；无归档/删除按钮。</p>
<div class="table-wrap"><table><thead><tr><th>项目 / 登记路径</th><th>类型 / 当前元数据</th>
<th>保留策略 / 复核日期</th><th>计划 / 详情</th></tr></thead><tbody id="resources"></tbody></table></div>
<h2>本地最小反馈草案（未复现、未上传）</h2>
<p class="note">仅登记显式选择的合成案例；synthetic 是操作人声明，不是独立证明。正文不回传，无执行、上传或发布按钮。</p>
<div class="table-wrap"><table><thead><tr><th>项目 / 草案</th><th>状态 / 摘要</th>
<th>求证阶段</th><th>详情</th></tr></thead><tbody id="feedback"></tbody></table></div>
<h2>进程活动（不等于任务进展）</h2>
<p class="note">仅已知登记窗口；PID 探测包含权限/未知结果，不能核验原进程身份或活性。
心跳差值不是有效进展，未来时钟/缺失数据为未追踪；无控制或清场权。</p>
<div class="table-wrap"><table><thead><tr><th>项目目录</th><th>PID / 保守观察</th>
<th>登记状态 / 心跳</th></tr></thead><tbody id="activity"></tbody></table></div>
<h2>记录详情</h2><section id="intent-detail" hidden aria-live="polite"></section>
<pre id="detail">选择任务、目标或历史查看公开的只读投影。</pre>
<p class="note">每 5 秒读取有界窗口，无模型调用；历史耗时先验不是实时 ETA。修复、清场与上传未启用。</p>
<script>
const token=new URLSearchParams(location.search).get('token')||'';
const source=document.getElementById('source');
const tbody=document.getElementById('tasks');
const detail=document.getElementById('detail');
let selected='',selectedGoal='',selectedIntent='',selectedRepair='',selectedResource='',selectedFeedback='',busy=false,requestVersion=0,pending=null;
const shown=(v)=>v===null||v===undefined?'未追踪':String(v);
async function getJson(path,signal){
  const response=await fetch(path,{headers:token?{'Authorization':'Bearer '+token}:{},cache:'no-store',signal});
  const value=await response.json();
  if(!response.ok)throw new Error(value.error||('HTTP '+response.status));
  return value;
}
function cell(tr,text){const td=document.createElement('td');td.textContent=text;tr.appendChild(td);return td;}
function resources(item){
  const usage=item.usage,bill=item.billing;
  const amounts=bill.currency_totals.map(row=>shown(row.reported_amount)+' '+row.currency).join('；');
  return shown(usage.total_tokens)+' tokens ('+usage.token_coverage+')；费用 '+
    (amounts||'币种/费用未追踪')+' ('+bill.financial_coverage+')；币种缺失 '+bill.unknown_currency_observations+
    ' 条 / 金额缺失 '+bill.missing_amount_observations+' 条；CLI 完成观测 '+shown(usage.calls)+
    '；provider 请求观测 '+shown(bill.provider_requests.observed_total)+
    '；usage 响应记录 '+shown(bill.assistant_usage_messages.observed_total)+'（非请求数，非全程账单）';
}
function renderTask(task){
  const tr=document.createElement('tr');
  if(task.analysis.flags.length||task.status.toLowerCase().includes('fail'))tr.className='bad';
  cell(tr,task.project+' / '+task.kind);
  cell(tr,task.title||task.local_task_id);
  cell(tr,task.status+' / '+(task.flow.last_observed_stage||task.phase)+
    '; 声明下一步 '+shown(task.flow.next_stage)+' ('+task.flow.next_status+')');
  const priorInterval=(task.forecast||{}).prior_remaining_interval_seconds;
  cell(tr,shown(task.time.elapsed_wall_seconds)+' 秒；剩余 '+shown(task.time.remaining_seconds)+
    '; 历史耗时先验 '+shown(task.forecast.duration_range_seconds)+' (非实时 ETA)'+
    '；'+(Array.isArray(priorInterval)?'先验剩余区间 '+priorInterval.join('–')+
      ' 秒（非实时 ETA；remaining/eta 为 null）':
      '先验剩余区间 未追踪（'+shown((task.forecast||{}).prior_remaining_basis)+'）'));
  cell(tr,task.llm.map(m=>(m.provider||'?')+'/'+(m.model||'?')).join(', ')||'未追踪');
  cell(tr,resources(task.consumption||task));
  cell(tr,shown(task.delivery.present_files)+'/'+shown(task.delivery.declared_files)+
    ' 文件存在；目标记录 '+(task.goal_links.map(g=>g.acceptance).join(', ')||'未追踪'));
  const button=document.createElement('button');
  button.textContent=task.repair_preview&&task.repair_preview.applicability==='failure_observation'?
    '失败 / 恢复预览（只读）':'详情';
  button.addEventListener('click',()=>{selectedGoal='';selectedIntent='';selectedRepair='';selectedResource='';selectedFeedback='';selectedAttention='';selected=task.task_id;
    clearIntentPanel();detail.textContent=JSON.stringify(task,null,2);});
  cell(tr,'').appendChild(button);tbody.appendChild(tr);
}
function renderGoal(goal){
  const tr=document.createElement('tr');
  cell(tr,goal.project+' / '+goal.statement);
  cell(tr,goal.progress.matching_attestations+'/'+goal.progress.criteria+' 个证据匹配的记录；'+goal.acceptance);
  cell(tr,goal.flow?(shown(goal.flow.last_observed_stage)+' → '+shown(goal.flow.next_stage)+
    ' ('+goal.flow.next_status+'；非执行授权)'):'执行计划未追踪');
  cell(tr,resources(goal.consumption)+(goal.consumption.shared_execution_observations?
    '；存在共享调用，不可跨目标相加':'')+'；独立验收未验证');
  const button=document.createElement('button');button.textContent='详情';
  button.addEventListener('click',()=>{selected='';selectedIntent='';selectedRepair='';selectedResource='';selectedFeedback='';selectedAttention='';selectedGoal=goal.goal_id;
    clearIntentPanel();detail.textContent=JSON.stringify(goal,null,2);});
  cell(tr,'').appendChild(button);document.getElementById('goals').appendChild(tr);
}
function renderRepair(item){
  const tr=document.createElement('tr');
  cell(tr,item.project+' / '+item.repair_id);
  cell(tr,item.receipt.stage_label+' / '+item.status+'；非执行授权');
  const limits=item.limits;
  cell(tr,'声明截止 '+limits.requested_deadline_at+'；尝试上限 '+limits.max_attempts+
    '；耗时上限 '+limits.max_elapsed_seconds+' 秒；Token 上限 '+limits.max_total_tokens+
    '；费用上限 '+limits.max_cost.amount+' '+limits.max_cost.currency+'（未核验原限制）');
  const assessment=item.assessment;
  cell(tr,assessment?('不可执行 / '+assessment.decision+'；阻塞 '+assessment.blockers.join('；')):
    '检查不可用；不可执行');
  const button=document.createElement('button');button.textContent='请求详情（只读）';
  button.addEventListener('click',()=>{selected='';selectedGoal='';selectedIntent='';selectedResource='';selectedFeedback='';selectedAttention='';selectedRepair=item.repair_id;
    clearIntentPanel();detail.textContent=JSON.stringify(item,null,2);});
  cell(tr,'').appendChild(button);document.getElementById('repairs').appendChild(tr);
}
function renderResource(item){
  const tr=document.createElement('tr');
  cell(tr,item.project+' / '+item.relative_path_display);
  cell(tr,item.declared_kind+' / '+item.availability.state+'；文件字节 '+shown(item.availability.observed_file_bytes)+
    '（目录总量/内容未复验）');
  cell(tr,item.policy+'；复核日期 '+item.review_at+'；过期不是删除批准');
  const button=document.createElement('button');button.textContent='保留计划（只读）';
  button.addEventListener('click',()=>{selected='';selectedGoal='';selectedIntent='';selectedRepair='';
    selectedFeedback='';selectedAttention='';selectedResource=item.resource_id;clearIntentPanel();detail.textContent=JSON.stringify(item,null,2);});
  cell(tr,'保留；未授清场权 ').appendChild(button);document.getElementById('resources').appendChild(tr);
}
function safeFeedback(item){return item.status==='local_draft_only'&&item.state==='draft'?item:
  {...item,summary:null,duplicate_inspection:null};}
function feedbackMatches(item){
  const matches=item.duplicate_inspection;
  return matches?'内容元数据匹配候选 '+shown(matches.observed_matches)+'；'+matches.selection+
    '（非同根因、非完整去重；副本未复核）':'内容匹配不可用；不复用旧关联';
}
function renderFeedback(item){
  item=safeFeedback(item);
  const tr=document.createElement('tr');cell(tr,item.project+' / '+item.feedback_id);
  cell(tr,item.status+' / '+(item.summary||'摘要已撤回或当前来源定义不匹配'));
  cell(tr,'独立复现未完成；根因仅未验证假设；修复/回归/发布/原任务验收未求证；'+feedbackMatches(item));
  const button=document.createElement('button');button.textContent='草案元数据（只读）';
  button.addEventListener('click',()=>{selected='';selectedGoal='';selectedIntent='';selectedRepair='';selectedResource='';
    selectedAttention='';selectedFeedback=item.feedback_id;clearIntentPanel();detail.textContent=JSON.stringify(item,null,2);});
  cell(tr,'未授执行/上传权 ').appendChild(button);document.getElementById('feedback').appendChild(tr);
}
__INTENT_VIEW_SCRIPT__
__ATTENTION_VIEW_SCRIPT__
function clearIntentPanel(){
  const pane=document.getElementById('intent-detail');pane.replaceChildren();pane.hidden=true;
}
function clearView(note){
  ['tasks','goals','intents','repairs','resources','feedback','activity'].forEach(id=>document.getElementById(id).replaceChildren());
  clearIntentPanel();clearAttention();detail.textContent=note;
}
async function refresh(force=false){
  if(busy&&!force)return;
  const version=++requestVersion,chosen=source.value;
  clearView('正在读取有界快照；不展示旧的缓存记录。');
  document.getElementById('summary').textContent='等待读取的新快照；旧统计不是当前事实。';
  document.getElementById('error').textContent='';
  if(selectedAttention)detail.textContent='正在核对关注事实；不展示旧引用。';
  if(pending)pending.abort();
  const controller=new AbortController();pending=controller;busy=true;
  if(selectedFeedback)detail.textContent='正在核对反馈草案；不展示旧的缓存记录。';
  if(selectedResource)detail.textContent='正在核对保留计划；不展示旧的缓存记录。';
  if(selectedIntent)detail.textContent='正在重新核对历史授权；不展示旧的缓存线索。';
  if(selectedRepair)detail.textContent='正在重新核对请求状态；不展示旧的缓存记录。';
  try{
    const view=await getJson('/api/tasks?limit=200'+(chosen?'&source='+encodeURIComponent(chosen):''),controller.signal);
    if(version!==requestVersion||chosen!==source.value)return;
    admitAttention(view,chosen);
    if(!chosen){
      const options=[new Option('所有已接入项目','')];
      view.sources.forEach(s=>options.push(new Option(s.name,s.source_id)));
      source.replaceChildren(...options);
    }
    tbody.replaceChildren();view.tasks.forEach(renderTask);
    document.getElementById('goals').replaceChildren();view.goals.forEach(renderGoal);
    document.getElementById('intents').replaceChildren();
    view.intents.forEach(item=>renderIntent(item,view.goals,view.sources));
    (view.repairs||[]).forEach(renderRepair);
    (view.resources||[]).forEach(renderResource);
    (view.feedback||[]).forEach(renderFeedback);
    renderAttention(view);
    const activity=document.getElementById('activity');activity.replaceChildren();
    view.activity.processes.forEach(p=>{const tr=document.createElement('tr');
      cell(tr,p.workspace);cell(tr,p.pid+' / '+p.presence);
      cell(tr,p.registered_status+' / 心跳距今 '+shown(p.heartbeat_age_seconds)+' 秒');
      activity.appendChild(tr);});
    const task=view.tasks.find(t=>t.task_id===selected);
    if(selected)detail.textContent=task?JSON.stringify(task,null,2):'该任务不在本次窗口中；不代表已完成或已删除。';
    if(selectedGoal){const goal=view.goals.find(g=>g.goal_id===selectedGoal);
      detail.textContent=goal?JSON.stringify(goal,null,2):'目标不在当前已接入视图中；不代表已完成。';}
    if(selectedIntent){const item=view.intents.find(i=>i.intent_id===selectedIntent);
      if(item)showIntent(item,view.goals,view.sources);
      else{clearIntentPanel();detail.textContent='历史不在当前视图中；不代表已删除或仍有授权。';}}
    if(selectedRepair){const item=(view.repairs||[]).find(r=>r.repair_id===selectedRepair);
      detail.textContent=item?JSON.stringify(item,null,2):'请求不在当前视图中；不代表已执行、撤回或仍有效。';}
    if(selectedResource){const item=(view.resources||[]).find(r=>r.resource_id===selectedResource);
      detail.textContent=item?JSON.stringify(item,null,2):'资源登记不在当前视图中；不代表已删除或可清场。';}
    if(selectedFeedback){const item=(view.feedback||[]).find(r=>r.feedback_id===selectedFeedback);
      detail.textContent=item?JSON.stringify(safeFeedback(item),null,2):'草案不在当前视图中；不代表已上传、复现或仍有效。';}
    const mapping=view.execution_mapping||{};
    document.getElementById('summary').textContent='接入 '+view.sources.length+' 个来源；窗口观察到 '+
      view.observed_task_count+' 个任务；更新于 '+view.generated_at+
      '；执行映射：已验证 '+shown(mapping.verified_spawn_and_outcome)+'，缺 spawn 证据 '+
      shown(mapping.outcome_without_spawn_observation)+'（来源未验证，不是 0），spawn-only '+
      shown(mapping.spawn_only_active_observations)+'（不是存活），窗口 '+shown(mapping.coverage)+
      '，计数为下界 '+shown(mapping.unknown_not_zero)+'；不按标题/PID/时间戳推断，不代表验收或授权。';
    const problems=view.sources.flatMap(s=>s.coverage.filter(c=>c.error||c.malformed||c.truncated||c.incomplete)
      .map(c=>s.name+'/'+c.feed+': 数据不可读、损坏或窗口不完整'));
    document.getElementById('error').textContent=[...view.diagnostics,
      ...view.activity.diagnostics,...problems].join('；');
  }catch(error){
    if(version!==requestVersion)return;
    clearView('数据当前不可用，不能确认任务状态或历史授权。');
    document.getElementById('summary').textContent='数据不可用';
    document.getElementById('error').textContent='刷新失败: '+error.message;
  }finally{if(version===requestVersion){busy=false;pending=null;}}
}
document.getElementById('refresh').addEventListener('click',()=>refresh(true));
source.addEventListener('change',()=>{selected='';selectedGoal='';selectedIntent='';selectedRepair='';selectedResource='';selectedFeedback='';selectedAttention='';
  clearView('正在读取所选来源，旧项目的记录和线索已移除。');
  document.getElementById('summary').textContent='等待所选来源的新快照';
  document.getElementById('error').textContent='';refresh(true);});
refresh();setInterval(refresh,5000);
</script></body></html>"""


def page_html() -> str:
    return _PAGE.replace("__INTENT_VIEW_SCRIPT__", INTENT_SCRIPT).replace("__ATTENTION_VIEW_SCRIPT__", ATTENTION_SCRIPT)
