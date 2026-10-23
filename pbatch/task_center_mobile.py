"""Task-first mobile cards for the read-only global task view.

The desktop table stays authoritative and keeps its IDs; at phone widths the
table is hidden and these cards render from the same bounded JSON snapshot. All
values are inserted with ``textContent``; unknown values stay untracked.
"""
from __future__ import annotations

MOBILE_STYLE = """
#mobile-tasks-view{display:none}
@media(max-width:600px){
#mobile-tasks-view{display:block}
.desktop-tasks{display:none}
.m-card{border:1px solid #ccc;border-radius:.5rem;padding:.6rem;margin:.6rem 0;
  overflow-wrap:anywhere;max-width:100%}
.m-card h3{font-size:1rem;margin:0 0 .3rem}
.m-card.bad{background:#fff3f3;border-color:#c66}
.m-line{margin:.15rem 0}
.m-more{margin-top:.3rem}
.m-card button{margin-top:.4rem;max-width:100%}
#mobile-warnings{color:#a21;overflow-wrap:anywhere}
}
"""

MOBILE_MARKUP = """
<section id="mobile-tasks-view" aria-labelledby="mobile-tasks-heading">
<h2 id="mobile-tasks-heading">任务（手机视图）</h2>
<p class="note" id="mobile-note">任务优先：项目、状态、执行/合并、阶段、耗时与记录配置；未知保持未追踪。</p>
<p id="mobile-warnings" role="status"></p>
<div id="mobile-tasks"></div>
</section>
"""

MOBILE_SCRIPT = """
(function(){
'use strict';
var container=document.getElementById('mobile-tasks');
var warnings=document.getElementById('mobile-warnings');
var count=document.getElementById('task-count');
if(!container||!warnings)return;
function text(value){return value===null||value===undefined||value===''?'未追踪':String(value);}
function seconds(value){return value===null||value===undefined?'未追踪':String(value)+' 秒';}
function element(tag,name,value){
  var node=document.createElement(tag);
  if(name)node.className=name;
  if(value!==undefined)node.textContent=value;
  return node;
}
function line(parent,label,value){parent.appendChild(element('p','m-line',label+'：'+value));}
function models(task){
  var rows=Array.isArray(task.llm)?task.llm:[];
  if(!rows.length)return '模型配置未追踪；thinking 未追踪（不推断请求配置）';
  var visible=rows.slice(0,3);
  var text=visible.map(function(item){
    var provider=item&&item.provider?item.provider:'?';
    var model=item&&item.model?item.model:'?';
    var thinking=item&&item.thinking;
    var recorded=thinking===null||thinking===undefined||thinking===''?
      '未追踪（不推断请求配置）':
      String(thinking)+'（记录的请求配置，非 provider 确认的强度或时长）';
    return provider+'/'+model+'；thinking '+recorded;
  }).join('；');
  return text+(rows.length>visible.length?'；等 '+rows.length+' 条记录（有界）':'');
}
function execution(task){
  var scheduling=task.scheduling||{};
  var delivery=task.delivery||{};
  var status=scheduling.execution_status;
  var acceptance=delivery.acceptance==='untracked'?'未追踪':text(delivery.acceptance);
  return (status?'执行结果 '+status+'（执行完成不等于合并/验收）':
    '执行结果 未追踪（不推断成功）')+
    '；合并记录 '+text(delivery.merge_status)+'；目标验收 '+acceptance;
}
function timing(task){
  var time=task.time||{};
  var scheduling=task.scheduling||{};
  var prior=(task.forecast||{}).prior_remaining_interval_seconds;
  var parts=['任务累计 '+seconds(time.elapsed_wall_seconds),
    '调度排队 '+seconds(scheduling.queue_wait_seconds),
    '调度执行 '+seconds(scheduling.execution_wall_seconds)];
  if(Array.isArray(prior))parts.push('先验剩余区间 '+prior.join('–')+' 秒（非实时 ETA）');
  else parts.push('先验剩余区间 未追踪（非实时 ETA）');
  return parts.join('；');
}
function flagState(task){
  var flags=task.analysis&&Array.isArray(task.analysis.flags)?task.analysis.flags:[];
  var status=String(task.status||'').toLowerCase();
  var execution=String((task.scheduling||{}).execution_status||'').toLowerCase();
  var bad=flags.length>0||/fail|block|reject|error|pause/.test(status)||
    /fail|block|reject|error|pause/.test(execution);
  return {bad:bad,
    text:flags.length?flags.join('；'):(bad?'失败或阻塞状态（本页仅记录观测）':'无已记录告警')};
}
function warningText(items){
  var rows=items.filter(function(item){return flagState(item.task||{}).bad;})
    .map(function(item){return text((item.task||{}).title||(item.task||{}).local_task_id);});
  return rows.length?'需关注：'+rows.join('；')+'（只读观测，不是预检或干预授权）':'';
}
function card(item){
  var task=item.task||{};
  var flag=flagState(task);
  var node=element('article','m-card'+(flag.bad?' bad':''));
  node.appendChild(element('h3','m-title',text(task.title||task.local_task_id)));
  line(node,'项目 / 类型',text(task.project)+' / '+text(task.kind));
  line(node,'状态 / 阶段',text(task.status)+' / '+text(task.phase)+
    ((task.flow||{}).last_observed_stage?
      '；最近步骤 '+text(task.flow.last_observed_stage):''));
  line(node,'执行 / 合并',execution(task));
  line(node,'模型配置',models(task));
  line(node,'耗时',timing(task));
  if(flag.bad)node.appendChild(element('p','m-warn',
    '关注：'+flag.text+'（只读观测，不是干预授权）'));
  var more=element('details','m-more');
  more.appendChild(element('summary',null,'用量、费用、产物与验收（未追踪项不补零）'));
  line(more,'Token / 费用',text(item.resources));
  var delivery=task.delivery||{};
  line(more,'产物 / 验收',text(delivery.present_files)+'/'+
    text(delivery.declared_files)+' 文件存在；目标验收 '+
    (delivery.acceptance==='untracked'?'未追踪':text(delivery.acceptance)));
  line(more,'声明下一步',text((task.flow||{}).next_stage)+'（'+
    text((task.flow||{}).next_status)+'；不是执行授权）');
  node.appendChild(more);
  var button=element('button','m-detail','详情（只读）');
  button.addEventListener('click',function(){
    var api=window.pbatchTaskCenter;
    if(api&&typeof api.showTaskDetail==='function')api.showTaskDetail(task.task_id,task);
  });
  node.appendChild(button);
  return node;
}
function render(payload){
  container.replaceChildren();
  var items=Array.isArray(payload.tasks)?payload.tasks:[];
  items.forEach(function(item){container.appendChild(card(item));});
  warnings.textContent=warningText(items);
  if(count)count.textContent='本页 '+items.length+' 项任务（有界窗口）';
}
container.replaceChildren();
warnings.textContent='';
window.pbatchMobileView=function(kind,payload){
  if(kind==='clear'){
    container.replaceChildren();warnings.textContent='';
    if(count)count.textContent='等待新的任务快照';
    return;
  }
  if(kind==='tasks')render(payload||{});
};
})();
"""
