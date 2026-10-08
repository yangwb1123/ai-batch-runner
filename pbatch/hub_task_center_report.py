"""Credential-free Web observer shell; data requires the existing per-request Hub user grant.

A transient bearer is not a grant, login flow, server logout or upload approval.
Only a small literal display projection is rendered; no raw case/history bodies.
"""
from __future__ import annotations

import base64
import hashlib

_STYLE = """body{font-family:system-ui;margin:1rem;max-width:1400px;color:#222}
table{border-collapse:collapse;width:100%}td,th{padding:.5rem;border-bottom:1px solid #ddd;text-align:left}
.wrap{overflow:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere}button,input,select{font:inherit;padding:.5rem;margin:.25rem}
.note{color:#555}#error{color:#a21}td{overflow-wrap:anywhere}@media(max-width:600px){body{margin:.5rem}td{min-width:6rem}}"""

_SCRIPT = r"""'use strict';
const $=id=>document.getElementById(id);
const MAX=2*1024*1024, method='hub-native-task-facts-v1', phase='hub_recorded_state';
let bearer='',epoch=0,busy=false,queued=false,selected='',current=[];
const full=(value,prefix,n)=>typeof value==='string'&&new RegExp('^'+prefix+'-[a-f0-9]{'+n+'}$').test(value);
const text=(value,max=256)=>String(value??'未追踪').replace(/[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]/g,' ').slice(0,max);
const object=value=>value!==null&&typeof value==='object'&&!Array.isArray(value);
const exact=(value,fields)=>{if(!object(value)||Object.keys(value).sort().join('|')!==fields.split(' ').sort().join('|'))throw Error('schema');};
const noAuthority=value=>{exact(value,'control resume merge cleanup feedback_upload budget_or_deadline_reset');if(Object.values(value).some(v=>v!==false))throw Error('authority');};
const integer=value=>Number.isSafeInteger(value)&&value>=0;
const detailJSON=value=>JSON.stringify(value,null,2).replace(/[\x7f-\x9f\u202a-\u202e\u2066-\u2069]/g,
  char=>'\\u'+char.charCodeAt(0).toString(16).padStart(4,'0'));
function noBodies(value){
  const queue=[value];let count=0;
  const forbidden=new Set(['prompt','payload','payload_json','canonical_json','body','text','expected','observed','hypothesis','original_path','private_path',
    'data_json','request_json','argv','workdir','result','artifact_ref','actor','idempotency_key','device_id','_targets','gpu_assignment']);
  while(queue.length){const item=queue.pop();if(++count>50000)throw Error('budget');
    if(item&&typeof item==='object')for(const [key,child] of Object.entries(item)){
      if(forbidden.has(key))throw Error('private fields');queue.push(child);
    }
  }
}
function taskBrief(item,sources,tenant){
  exact(item,'task_id source_id project kind title run_id local_task_id status phase hub goal goal_links time flow lineage plan llm usage billing efficiency forecast artifacts delivery analysis observations capabilities');
  const compute=method==='hub-compute-task-facts-v1';
  if(!full(item.task_id,'tc',32)||!sources.has(item.source_id)||
    !(compute?item.kind==='hub_compute_task':['hub_session','hub_turn'].includes(item.kind)))throw Error('identity');
  const hub=item.hub,source=sources.get(item.source_id);noAuthority(item.capabilities);
  exact(hub,compute?'tenant_id instance_id session_id compute_task_id reported_generation registered_at':
    'tenant_id instance_id session_id turn_id reported_generation session_frozen project_label'+(item.kind==='hub_turn'?' registered_at':''));
  if(hub.tenant_id!==tenant||hub.instance_id!==source.hub.instance_id||!Number.isSafeInteger(hub.reported_generation)||hub.reported_generation<1||
    typeof hub.session_id!=='string'||!hub.session_id)throw Error('scope');
  if(compute?!full(hub.compute_task_id,'compute',32):
    (typeof hub.session_frozen!=='boolean'||(item.kind==='hub_turn'?(typeof hub.turn_id!=='string'||!hub.turn_id):hub.turn_id!==null)))throw Error('type');
  if(typeof item.title!=='string'||typeof item.status!=='string'||item.status.length>32||item.phase!==phase||
    item.usage.total_tokens!==null||item.usage.calls!==null||item.time.elapsed_wall_seconds!==null||item.time.remaining_seconds!==null||
    item.delivery.acceptance!=='untracked'||item.goal.statement!==null||item.plan!==null||
    item.billing.currency_totals.length||item.billing.provider_requests.observed_total!==null)throw Error('unsupported facts');
  return {record_id:item.task_id,kind:item.kind,source_id:item.source_id,instance_id:hub.instance_id,session_id:hub.session_id,
    turn_id:hub.turn_id??null,compute_task_id:hub.compute_task_id??null,
    reported_generation:hub.reported_generation,session_frozen:hub.session_frozen??null,
    title:text(item.title),reported_status:text(item.status,32),last_recorded_at:item.time.last_observed_at,
    elapsed_wall_seconds:null,total_tokens:null,provider_requests:null,cost:null,acceptance:'untracked',
    live_liveness:'not_verified',capabilities:'none; viewing is not execution authorization'};
}
function attentionBrief(item,sources){
  exact(item,'attention_id source_id project target reason_code summary status basis evidence root_cause operator_action capabilities');
  exact(item.target,'kind record_id');noAuthority(item.capabilities);
  if(!full(item.attention_id,'at',32)||!sources.has(item.source_id)||item.root_cause!=='not_verified'||
    item.operator_action!=='not_selected_or_authorized'||item.status!=='observed_attention_fact')throw Error('attention');
  if(item.target.kind==='tasks'){
    if(!full(item.target.record_id,'tc',32))throw Error('attention target');
    exact(item.evidence,'record_ref reported_status phase partial_window');
    if(typeof item.evidence.record_ref!=='string'||!new RegExp('^[a-f0-9]{32}$').test(item.evidence.record_ref)||
      item.evidence.phase!==phase||typeof item.evidence.partial_window!=='boolean')throw Error('attention evidence');
  }else if(item.target.kind==='sources'){
    if(item.target.record_id!==item.source_id||item.reason_code!=='source_feed_gap')throw Error('attention source');
    exact(item.evidence,'feeds omitted_feed_refs live_process_liveness');
  }else throw Error('untracked attention');
  return {record_id:item.attention_id,kind:'attention',source_id:item.source_id,target:item.target,
    reason_code:text(item.reason_code,80),summary:text(item.summary),root_cause:'not_verified',
    operator_action:'not_selected_or_authorized',reference_scope:'admitted metadata only; target may be outside task display window'};
}
function admit(value,chosen){
  exact(value,'schema_version generated_at window_only sources tasks goals intents repairs resources feedback diagnostics observed_task_count omitted_tasks status_counts activity scope attention');noBodies(value);
  const scope=value.scope;
  if(value.schema_version!==1||value.window_only!==true||scope.method!==method||scope.grants_execution!==false||
    scope.complete_inventory!==false||scope.authorization!=='hub_user_resource_grant_per_request'||scope.source_filter!==chosen||
    typeof scope.tenant_id!=='string'||!scope.tenant_id||typeof scope.partial_window!=='boolean')throw Error('scope');
  if(!Array.isArray(value.sources)||value.sources.length>64||!Array.isArray(value.tasks)||value.tasks.length>100||
    !integer(value.observed_task_count)||!integer(value.omitted_tasks)||value.observed_task_count-value.omitted_tasks!==value.tasks.length)throw Error('window');
  for(const kind of ['goals','intents','repairs','resources','feedback'])if(!Array.isArray(value[kind])||value[kind].length||scope.metadata_groups[kind]!=='untracked_not_imported')throw Error('untracked');
  const sources=new Map(),identities=new Set();
  for(const item of value.sources){
    exact(item,'source_id name project origin workspace data_dir hub coverage');
    exact(item.hub,'tenant_id instance_id reported_generation last_heartbeat_at liveness');
    if(!full(item.source_id,'src',24)||sources.has(item.source_id)||item.name!==item.source_id||
      (chosen&&item.source_id!==chosen)||item.hub.tenant_id!==scope.tenant_id||item.workspace!==null||item.data_dir!==null)throw Error('source');
    sources.set(item.source_id,item);
  }
  const tasks=value.tasks.map(item=>taskBrief(item,sources,scope.tenant_id));
  const attention=value.attention;
  if(attention.schema_version!==1||attention.method!=='bounded-attention-facts-v1'||attention.window_only!==true||
    attention.grants_execution!==false||attention.zero_items_proves_safety_or_no_missing_work!==false||
    !Array.isArray(attention.items)||attention.items.length>128||!integer(attention.observed_items)||!integer(attention.omitted_items)||
    attention.observed_items-attention.omitted_items!==attention.items.length)throw Error('attention counts');
  const cues=attention.items.map(item=>attentionBrief(item,sources));
  for(const item of [...tasks,...cues]){if(identities.has(item.record_id))throw Error('duplicate');identities.add(item.record_id);}
  return {sources:[...sources.values()],tasks,cues,tenant:scope.tenant_id,partial:scope.partial_window,
    observed:value.observed_task_count,omitted:value.omitted_tasks,attention:attention.observed_items};
}
function clear(note){
  current=[];$('tasks').replaceChildren();$('attention').replaceChildren();$('detail').textContent=note;
  $('summary').textContent='事实不可用；未知不是零，不保留旧记录。';
}
function invalidate(note){++epoch;clear(note);$('error').textContent='';}
function cell(row,value){const node=document.createElement('td');node.textContent=text(value);row.appendChild(node);return node;}
function render(value){
  current=[...value.tasks,...value.cues];
  const chosen=$('source').value;$('source').replaceChildren();
  const all=document.createElement('option');all.value='';all.textContent='授权来源窗口';$('source').appendChild(all);
  for(const item of value.sources){const option=document.createElement('option');option.value=item.source_id;
    option.textContent=text(item.hub.instance_id)+' / '+item.source_id;$('source').appendChild(option);}
  if(chosen&&!value.sources.some(item=>item.source_id===chosen)){const option=document.createElement('option');option.value=chosen;
    option.textContent=chosen+'（本次窗口不可用）';$('source').appendChild(option);} $('source').value=chosen;
  for(const item of current){const row=document.createElement('tr');cell(row,item.source_id);cell(row,item.record_id);
    cell(row,item.reported_status??item.reason_code);cell(row,item.title??item.summary);
    const button=document.createElement('button');button.textContent='观察引用（只读）';
    button.addEventListener('click',()=>{selected=item.record_id;$('detail').textContent=detailJSON(item);});
    cell(row,'').appendChild(button);$(item.kind==='attention'?'attention':'tasks').appendChild(row);}
  $('summary').textContent='tenant='+text(value.tenant)+'；窗口任务='+value.observed+'；任务显示遗漏='+value.omitted+
    '；关注事实='+value.attention+'；窗口不完整='+value.partial+'；非全程清单/验收或执行授权。';
  if(selected){const item=current.find(item=>item.record_id===selected);$('detail').textContent=item?detailJSON(item):
    '所选完整 ID 不在本次窗口；不是已完成、已解决、已删除或获准干预。';}
}
async function bounded(response){
  const length=response.headers.get('Content-Length');if(length&&(!/^[0-9]{1,9}$/.test(length)||Number(length)>MAX))throw Error('byte limit');
  const reader=response.body.getReader(),parts=[];let size=0;
  while(true){const {done,value}=await reader.read();if(done)break;size+=value.byteLength;
    if(size>MAX||parts.length>=1024){await reader.cancel();throw Error('byte limit');}parts.push(value);}
  const bytes=new Uint8Array(size);let offset=0;for(const part of parts){bytes.set(part,offset);offset+=part.byteLength;}
  return JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(bytes));
}
async function refresh(force=false){
  if(!bearer)return;if(busy&&!force)return;
  invalidate('正在重新鉴权读取；不展示旧事实。');
  if(busy){queued=true;return;}busy=true;
  const version=epoch,token=bearer,chosen=$('source').value,controller=new AbortController();
  const timer=setTimeout(()=>controller.abort(),12000);
  try{
    const response=await fetch('/api/v1/agent/task-center?'+new URLSearchParams({limit:'100',source:chosen}),
      {method:'GET',headers:{'Authorization':'Bearer '+token},credentials:'omit',cache:'no-store',redirect:'error',signal:controller.signal});
    if(!response.ok){if(version===epoch&&token===bearer&&[401,403].includes(response.status))bearer='';throw Error('HTTP '+response.status);}
    const envelope=await bounded(response);
    if(version!==epoch||token!==bearer||chosen!==$('source').value)return;
    exact(envelope,'data');render(admit(envelope.data,chosen));
  }catch(error){if(version===epoch){clear('当前事实不可用；没有旧事实兜底。');$('error').textContent='读取/鉴权/格式失败；不是恢复或干预授权。';}}
  finally{clearTimeout(timer);busy=false;if(queued){queued=false;refresh(true);}}
}
$('credentials').addEventListener('submit',event=>{event.preventDefault();const token=$('token').value.trim();$('token').value='';
  invalidate('凭据范围改变；旧事实已清除。');selected='';$('source').value='';
  if(!token||/\s/.test(token)||new TextEncoder().encode(token).length>16384){bearer='';return;}bearer=token;refresh(true);});
$('token').addEventListener('input',()=>{bearer='';selected='';queued=false;invalidate('正在输入新凭据；旧事实已清除。');});
$('clear-token').addEventListener('click',()=>{bearer='';$('token').value='';selected='';queued=false;invalidate('本页凭据已清除；不是服务端撤销或任务取消。');});
$('source').addEventListener('change',()=>{selected='';refresh(true);});
$('refresh').addEventListener('click',()=>refresh(true));
setInterval(()=>refresh(false),5000);
"""


def _script(compute=False, source_sessions=False):
    if source_sessions and not compute:
        raise ValueError("source-session Web profile needs explicit compute selection")
    result = (_SCRIPT.replace("method='hub-native-task-facts-v1', phase='hub_recorded_state'",
                             "method='hub-compute-task-facts-v1', phase='hub_compute_recorded_state'")
                    .replace("/api/v1/agent/task-center?", "/api/v1/agent/compute-task-center?")) if compute else _SCRIPT
    if source_sessions:
        from .hub_task_source_sessions_report import script
        result = script(result)
    return result


def page_html(*, compute=False, source_sessions=False) -> str:
    title = "Hub 源会话引用" if source_sessions else "Hub 计算任务" if compute else "Hub 任务中心"
    notice = ("显式身份引用窗口，需实例/计算/会话三类读权限；不是执行父子关系、本地映射或计费合并。" if source_sessions else
              "只读计算任务元数据；命令/结果/设备放置未读取，不合并原生或本地记录。" if compute else
              "只读原生 session/turn 元数据，不是全机或计算任务清单。")
    return ("<!doctype html><html lang='zh'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'><title>" + title + " · 只读</title>"
        "<style>" + _STYLE + "</style></head><body><h1>" + title + " · 认证只读</h1>"
        "<p class='note'>" + notice + "未知费用/耗时不是零；运行/成功不等于验收。</p>"
        "<form id='credentials'><label>临时 Bearer（仅本页内存，不保存，不放 URL）"
        "<input id='token' type='password' autocomplete='off' maxlength='16384'></label>"
        "<button type='submit'>读取授权窗口</button><button type='button' id='clear-token'>清除本页凭据</button></form>"
        "<label>来源 <select id='source'><option value=''>授权来源窗口</option></select></label>"
        "<button id='refresh'>刷新</button><p id='error' role='status'></p><p id='summary' role='status'>未读取；不推断零或安全。</p>"
        "<h2>任务记录</h2><div class='wrap'><table><thead><tr><th>来源</th><th>完整身份</th><th>记录状态</th><th>声明标签</th><th>引用</th></tr></thead><tbody id='tasks'></tbody></table></div>"
        "<h2>需关注事实（不是根因或必做操作）</h2><div class='wrap'><table><thead><tr><th>来源</th><th>完整身份</th><th>事实代码</th><th>说明</th><th>引用</th></tr></thead><tbody id='attention'></tbody></table></div>"
        "<h2>当前显示引用</h2><pre id='detail'>仅选择当前窗口中公开的引用；无额外正文读取。</pre>"
        "<p class='note'>无派发、取消、修复、清场、上传或升级控件；不复位原期限/预算。未接入的目标/历史/恢复/资源/反馈是未追踪，不是空清单。"
        "显示详情是有意限缩的公开投影，不是完整 JSON 或独立验收。</p><script>" + _script(compute, source_sessions) + "</script></body></html>")


def security_policy(*, compute=False, source_sessions=False) -> str:
    def digest(value):
        return base64.b64encode(hashlib.sha256(value.encode("utf-8")).digest()).decode("ascii")
    return ("default-src 'none'; script-src 'sha256-" + digest(_script(compute, source_sessions)) + "'; style-src 'sha256-" + digest(_STYLE) +
            "'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
