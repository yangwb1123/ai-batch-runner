"""Responsive global task view. Data is fetched as JSON, never embedded as HTML."""
from __future__ import annotations


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
@media(max-width:600px){body{margin:.5rem}td,th{min-width:6rem}}
</style></head><body>
<h1>全局任务中心 · 只读</h1>
<p class="note">当前显示账本事实，不将模型自述当成验收。不启动 Agent，不删除现场，不上报数据。</p>
<label>项目 <select id="source"><option value="">所有已接入项目</option></select></label>
<button id="refresh">刷新</button><span id="summary"></span>
<p id="error" role="status"></p>
<div class="table-wrap"><table><thead><tr><th>项目 / 类型</th><th>任务</th>
<th>最近状态 / 步骤</th><th>时长</th><th>模型配置</th><th>Token / 费用</th>
<th>产物 / 验收</th><th>详情</th></tr></thead><tbody id="tasks"></tbody></table></div>
<h2>明确登记的目标与验收记录</h2>
<p class="note">记录的操作人身份未认证；证据字节匹配不等于测试或语义独立验证。</p>
<div class="table-wrap"><table><thead><tr><th>项目 / 目标</th><th>标准 / 记录</th>
<th>最近步骤 / 声明下一步</th><th>关联消耗（窗口）</th><th>详情</th></tr></thead><tbody id="goals"></tbody></table></div>
<h2>授权历史的意图线索（候选推断，不是执行授权）</h2>
<p class="note">只展示本机显式导入的有界历史；角色与完整性为导出声明。原始正文不回传。</p>
<div class="table-wrap"><table><thead><tr><th>目标 / 角色通道</th><th>授权 / 覆盖</th>
<th>当前线索</th><th>详情</th></tr></thead><tbody id="intents"></tbody></table></div>
<h2>进程活动（不等于任务进展）</h2>
<div class="table-wrap"><table><thead><tr><th>项目目录</th><th>PID / 存在性</th>
<th>登记状态 / 心跳</th></tr></thead><tbody id="activity"></tbody></table></div>
<h2>任务详情</h2><pre id="detail">选择任务查看时间、调用、产物和证据引用。</pre>
<p class="note">每 5 秒读取有界窗口，无模型调用；历史耗时先验不是实时 ETA。修复、清场与上传未启用。</p>
<script>
const token=new URLSearchParams(location.search).get('token')||'';
const source=document.getElementById('source');
const tbody=document.getElementById('tasks');
const detail=document.getElementById('detail');
let selected='',selectedGoal='',selectedIntent='',busy=false;
const shown=(v)=>v===null||v===undefined?'未追踪':String(v);
async function getJson(path){
  const response=await fetch(path,{headers:token?{'Authorization':'Bearer '+token}:{},cache:'no-store'});
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
  cell(tr,shown(task.time.elapsed_wall_seconds)+' 秒；剩余 '+shown(task.time.remaining_seconds)+
    '; 历史耗时先验 '+shown(task.forecast.duration_range_seconds)+' (非实时 ETA)');
  cell(tr,task.llm.map(m=>(m.provider||'?')+'/'+(m.model||'?')).join(', ')||'未追踪');
  cell(tr,resources(task.consumption||task));
  cell(tr,shown(task.delivery.present_files)+'/'+shown(task.delivery.declared_files)+
    ' 文件存在；目标记录 '+(task.goal_links.map(g=>g.acceptance).join(', ')||'未追踪'));
  const button=document.createElement('button');button.textContent='详情';
  button.addEventListener('click',()=>{selectedGoal='';selectedIntent='';selected=task.task_id;
    detail.textContent=JSON.stringify(task,null,2);});
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
  button.addEventListener('click',()=>{selected='';selectedIntent='';selectedGoal=goal.goal_id;
    detail.textContent=JSON.stringify(goal,null,2);});
  cell(tr,'').appendChild(button);document.getElementById('goals').appendChild(tr);
}
function renderIntent(item){
  const tr=document.createElement('tr');
  cell(tr,item.goal_id+' / '+item.channel);
  cell(tr,item.status+' / '+item.coverage.messages+' 条；完整性为操作人声明');
  const hints=item.analysis?Object.values(item.analysis.hints).flat().map(h=>h.excerpt):[];
  cell(tr,hints.slice(-3).join('；')||'线索未评估或已撤销授权');
  const button=document.createElement('button');button.textContent='详情';
  button.addEventListener('click',()=>{selected='';selectedGoal='';selectedIntent=item.intent_id;
    detail.textContent=JSON.stringify(item,null,2);});
  cell(tr,'').appendChild(button);document.getElementById('intents').appendChild(tr);
}
async function refresh(){
  if(busy)return;busy=true;
  try{
    const view=await getJson('/api/tasks?limit=200'+(source.value?'&source='+encodeURIComponent(source.value):''));
    if(!source.value){
      const options=[new Option('所有已接入项目','')];
      view.sources.forEach(s=>options.push(new Option(s.name,s.source_id)));
      source.replaceChildren(...options);
    }
    tbody.replaceChildren();view.tasks.forEach(renderTask);
    document.getElementById('goals').replaceChildren();view.goals.forEach(renderGoal);
    document.getElementById('intents').replaceChildren();view.intents.forEach(renderIntent);
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
      detail.textContent=item?JSON.stringify(item,null,2):'历史不在当前视图中；不代表已删除。';}
    document.getElementById('summary').textContent='接入 '+view.sources.length+' 个来源；窗口观察到 '+
      view.observed_task_count+' 个任务；更新于 '+view.generated_at;
    const problems=view.sources.flatMap(s=>s.coverage.filter(c=>c.error||c.malformed||c.truncated||c.incomplete)
      .map(c=>s.name+'/'+c.feed+': 数据不可读、损坏或窗口不完整'));
    document.getElementById('error').textContent=[...view.diagnostics,
      ...view.activity.diagnostics,...problems].join('；');
  }catch(error){
    tbody.replaceChildren();document.getElementById('activity').replaceChildren();
    document.getElementById('goals').replaceChildren();document.getElementById('intents').replaceChildren();
    detail.textContent='数据当前不可用，不能确认任务状态。';
    document.getElementById('summary').textContent='数据不可用';
    document.getElementById('error').textContent='刷新失败: '+error.message;
  }finally{busy=false;}
}
document.getElementById('refresh').addEventListener('click',refresh);
source.addEventListener('change',()=>{selected='';selectedGoal='';selectedIntent='';refresh();});
refresh();setInterval(refresh,5000);
</script></body></html>"""


def page_html() -> str:
    return _PAGE
