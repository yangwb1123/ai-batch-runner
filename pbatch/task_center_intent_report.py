"""Text-only browser rendering of the shared intent projection; no history reader or controls."""
from __future__ import annotations


INTENT_SCRIPT = """
const intentStates={authorized:'已授权（仅线索投影）',revoked:'已撤销',
  stale_contract:'契约已变，需重新授权',goal_unavailable:'目标不可用，线索暂停'};
const hintKinds={goal:'目标候选',constraints:'约束候选',focus:'当前重点候选',next:'下一步候选'};
function textNode(tag,text){const node=document.createElement(tag);node.textContent=text;return node;}
function intentProject(item,goals,sources){
  const project=sources.find(s=>s.source_id===item.source_id);
  const goal=goals.find(g=>g.goal_id===item.goal_id);
  return (project?project.name:item.source_id)+' / '+(goal?goal.statement:item.goal_id)+' / '+item.channel;
}
function safeIntent(item){return item.status==='authorized'?item:{...item,analysis:null};}
function intentCoverage(item){
  const count=shown(item.coverage.messages)+' 条保留消息（角色为文件声明）';
  if(item.origin==='explicit_local_native_user_range_import'){
    const row=[...item.imports].reverse().find(r=>r.export_origin);
    if(!row)return count+'；范围证据未追踪，不声明完整历史';
    const range=row.export_origin.selection;
    return count+'；最近包用户范围 '+shown(range.first_user)+'–'+shown(range.last_user)+
      ' / 所选分支用户 '+shown(range.branch_user_messages)+'；原生文件用户 '+shown(range.native_user_messages)+
      '，记录 '+shown(range.native_records)+'（绑定快照，非全历史）';
  }
  if(item.origin==='explicit_local_native_user_import'){
    const row=[...item.imports].reverse().find(r=>r.adapter);
    return count+'；显式原生用户投影，非完整 session'+(row?
      '；所选用户 '+shown(row.adapter.user_messages)+'，其他分支未选用户 '+shown(row.adapter.excluded_user_messages)+
      '，未复制记录 '+shown(row.adapter.excluded_records):'；覆盖证据未追踪');
  }
  const claim=item.coverage.history_complete===true?'导出者声称完整（未独立审计）':
    item.coverage.history_complete===false?'导出者标为部分':'完整性未追踪';
  return count+'；'+claim;
}
function hintPreview(item){
  item=safeIntent(item);
  if(!item.analysis)return '线索暂停；不展示过去缓存的推断';
  return Object.entries(hintKinds).map(([key,label])=>{
    const rows=item.analysis.hints[key]||[];
    return label+': '+(rows.length?rows[rows.length-1].excerpt:'未提取到（不代表不存在）');
  }).join('；');
}
function showIntent(item,goals,sources){
  item=safeIntent(item);
  detail.textContent=JSON.stringify(item,null,2);
  const pane=document.getElementById('intent-detail');pane.replaceChildren();pane.hidden=false;
  pane.appendChild(textNode('h3',intentProject(item,goals,sources)));
  pane.appendChild(textNode('p',(intentStates[item.status]||item.status)+'；角色/操作人未认证；不授予执行或发布权限。'));
  pane.appendChild(textNode('p',intentCoverage(item)));
  renderIntentReceipts(pane,item);
  renderIntentAudit(pane,item);
  renderIntentAccounting(pane,item);
  if(!item.analysis){pane.appendChild(textNode('p','当前不展示候选线索；历史审计和已记录消耗仍保留。'));return;}
  pane.appendChild(textNode('p','以下仅为有界、脱敏的词法候选；不是确认后的目标、执行计划或语义漂移判决。'));
  Object.entries(hintKinds).forEach(([key,label])=>{
    const section=document.createElement('section');section.appendChild(textNode('h4',label));
    const rows=item.analysis.hints[key]||[];
    if(!rows.length)section.appendChild(textNode('p','未提取到线索，不等于用户没有该要求。'));
    rows.forEach(row=>renderHint(section,row));pane.appendChild(section);
  });
  const omitted=item.analysis.omitted_hints;
  pane.appendChild(textNode('p','本次投影省略候选 '+shown(omitted)+' 条；语义漂移未评估。'));
  renderIntentRevisions(pane,item.analysis.revisions||[]);
}
function renderHint(parent,hint){
  const block=document.createElement('div');block.className='intent-hint';
  block.appendChild(textNode('p',hint.excerpt));
  block.appendChild(textNode('p','状态 '+shown(hint.status)+' / 置信度 '+shown(hint.confidence)+
    '；'+(hint.excerpt_is_redacted_and_bounded===true?'片段已脱敏且有界':'片段边界未追踪')));
  const citation=document.createElement('details');citation.appendChild(textNode('summary','引用（不读取原始文件）'));
  citation.appendChild(textNode('pre',citationText(hint.citation||{})));
  block.appendChild(citation);parent.appendChild(block);
}
function citationText(cite){
  return '历史 '+shown(cite.history_id)+' / 消息 '+shown(cite.message_id)+' / 角色声明 '+shown(cite.role)+
    '\\n时间 '+shown(cite.ts)+'；字符位置 '+shown(cite.offset)+'–'+shown(cite.end_offset)+
    '\\n消息 SHA-256 '+shown(cite.message_sha256)+'\\n片段 SHA-256 '+shown(cite.fragment_sha256);
}
function renderIntentReceipts(parent,item){
  const section=document.createElement('details');section.appendChild(textNode('summary','导入覆盖与回执（不相加，不代表全历史）'));
  item.imports.forEach(row=>{
    const origin=row.export_origin,adapter=row.adapter;
    let note='导入于 '+shown(row.at)+'；新消息 '+shown(row.new_messages)+'；导出字节 '+shown(row.export_bytes)+
      '；导出 SHA-256 '+shown(row.export_sha256);
    if(origin){const range=origin.selection;
      note+='\\n'+origin.format+' / 用户范围 '+shown(range.first_user)+'–'+shown(range.last_user)+
        '，分支用户 '+shown(range.branch_user_messages)+'，文件用户 '+shown(range.native_user_messages)+
        '，文件记录 '+shown(range.native_records)+'；原生快照字节 '+shown(origin.input_bytes)+
        ' / SHA-256 '+shown(origin.input_sha256)+'\\n导出署名 '+shown(origin.by)+'（未认证） / '+shown(origin.reason)+
        '；导出时间 '+shown(origin.at)+'；重放时间 '+shown(origin.replayed_at)+
        '（仅导入时重放，当前原生文件未重新核验）';
    }else if(adapter){
      note+='\\n'+adapter.format+' / '+adapter.selection+'；所选用户 '+shown(adapter.user_messages)+
        '，未选用户 '+shown(adapter.excluded_user_messages)+'，未复制记录 '+shown(adapter.excluded_records)+
        '（不是完整会话，也不是当前原生文件复验）';
    }else note+='\\n规范化导出声明，未独立核验原生历史。';
    if(row.workspace_identity==='declared_cwd_not_authenticated')note+='\\n工作空间为原生文件声明，未认证其归属。';
    section.appendChild(textNode('p',note));
  });parent.appendChild(section);
}
function renderIntentAudit(parent,item){
  const section=document.createElement('details');section.appendChild(textNode('summary','授权/撤销审计（署名未认证）'));
  item.audit.forEach(row=>section.appendChild(textNode('p','v'+shown(row.version)+' / '+shown(row.action)+
    ' / '+shown(row.at)+' / '+shown(row.by)+' / '+shown(row.reason))));
  parent.appendChild(section);
}
function renderIntentAccounting(parent,item){
  const usage=item.accounting;
  parent.appendChild(textNode('p','已记录本地导入 '+shown(usage.import_operations)+' 次 / '+shown(usage.recorded_import_seconds)+
    ' 秒；保留文本 '+shown(usage.retained_text_bytes)+' 字节；此确定性导入模型请求 '+shown(usage.llm_requests)+
    ' / Token '+shown(usage.total_tokens)+'；费用 '+shown(usage.cost_total)+
    '。仅此导入/投影范围，不是原 Agent 消耗或所有监测的全程账单。'));
}
function renderIntentRevisions(parent,rows){
  const section=document.createElement('details');section.appendChild(textNode('summary','重述/转向提示（未确认，非漂移判决）'));
  if(!rows.length)section.appendChild(textNode('p','未检测到词法提示，不等于没有语义变化。'));
  rows.forEach(row=>{
    section.appendChild(textNode('p',shown(row.kind)+' / '+shown(row.status)+' / 语义漂移 '+shown(row.semantic_drift)));
    section.appendChild(textNode('pre','前引用\\n'+citationText(row.previous||{})+'\\n后引用\\n'+citationText(row.current||{})));
  });parent.appendChild(section);
}
function renderIntent(item,goals,sources){
  const tr=document.createElement('tr');
  if(item.status!=='authorized')tr.className='bad';
  cell(tr,intentProject(item,goals,sources));
  cell(tr,(intentStates[item.status]||item.status)+'；'+intentCoverage(item));
  cell(tr,hintPreview(item));
  const button=document.createElement('button');button.textContent='线索 / 引用 / 审计';
  button.addEventListener('click',()=>{if(busy)return;
    selected='';selectedGoal='';selectedRepair='';selectedResource='';selectedFeedback='';selectedAttention='';selectedIntent=item.intent_id;showIntent(item,goals,sources);});
  cell(tr,'').appendChild(button);document.getElementById('intents').appendChild(tr);
}
"""
