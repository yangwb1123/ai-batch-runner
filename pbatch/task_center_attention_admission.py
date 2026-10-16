"""Closed local-Web attention admission over supplied metadata only, before DOM mutation."""
from __future__ import annotations

import json

from .task_attention import MAX_ITEMS, METHOD, _METADATA, _TASK_STATES

_PROFILE = {
    "method": METHOD, "max_items": MAX_ITEMS,
    "task_states": {state: choice[0] for state, choice in _TASK_STATES.items()},
    "metadata_states": {state: [kind, choice[0]] for kind, (_, _, choices) in _METADATA.items()
                        for state, choice in choices.items()},
}

ADMISSION_SCRIPT = r"""
const localAttentionProfile=__LOCAL_ATTENTION_PROFILE__;
const attentionKinds={tasks:'tc',goals:'g',intents:'ih',repairs:'rq',resources:'rr',feedback:'fd',sources:'src'};
const attentionOwn=(value,key)=>Object.prototype.hasOwnProperty.call(value,key);
function attentionAssert(valid){
  if(!valid)throw new Error('关注投影不支持或与当前窗口冲突；未保留旧事实。');
}
function attentionObject(value,fields){
  attentionAssert(value!==null&&typeof value==='object'&&!Array.isArray(value));
  const keys=Object.keys(value);
  attentionAssert(keys.length===fields.length&&fields.every(key=>attentionOwn(value,key)));
  return value;
}
function attentionCount(value,maximum=1000000000){
  attentionAssert(Number.isSafeInteger(value)&&value>=0&&value<=maximum);return value;
}
function attentionText(value,maximum=1024){
  attentionAssert(typeof value==='string'&&value.length<=maximum);
  for(let i=0;i<value.length;i++){
    const code=value.charCodeAt(i);
    if(code>=0xd800&&code<=0xdbff){
      const next=value.charCodeAt(++i);attentionAssert(next>=0xdc00&&next<=0xdfff);
    }else attentionAssert(code<0xdc00||code>0xdfff);
  }
  return value;
}
function attentionId(value,prefix,digits=32){
  attentionAssert(typeof value==='string'&&value.length===prefix.length+1+digits&&
    new RegExp('^'+prefix+'-[a-f0-9]{'+digits+'}$').test(value));return value;
}
function attentionTotals(value,allowed,observed){
  attentionAssert(value!==null&&typeof value==='object'&&!Array.isArray(value));
  const keys=Object.keys(value);attentionAssert(keys.length<=allowed.length);
  attentionAssert(keys.every(key=>allowed.includes(key)&&attentionCount(value[key])>0));
  attentionAssert(keys.reduce((sum,key)=>sum+value[key],0)===observed);
}
function attentionEvidence(item){
  const value=item.evidence,kind=item.target.kind;
  if(kind==='tasks'){
    attentionObject(value,['record_ref','reported_status','phase','partial_window']);
    attentionText(value.record_ref,240);attentionText(value.reported_status,256);attentionText(value.phase,80);
    attentionAssert(typeof value.partial_window==='boolean'&&
      attentionOwn(localAttentionProfile.task_states,value.reported_status.toLowerCase())&&
      localAttentionProfile.task_states[value.reported_status.toLowerCase()]===item.reason_code);
  }else if(kind==='sources'){
    attentionObject(value,['feeds','omitted_feed_refs','live_process_liveness']);
    attentionAssert(item.reason_code==='source_feed_gap'&&value.live_process_liveness==='not_verified'&&
      Array.isArray(value.feeds)&&value.feeds.length>0&&value.feeds.length<=8);
    attentionCount(value.omitted_feed_refs);
    value.feeds.forEach(feed=>{
      attentionObject(feed,['feed','truncated','incomplete','malformed','read_error_observed']);
      attentionText(feed.feed,80);
      attentionAssert([feed.truncated,feed.incomplete].every(v=>v===null||typeof v==='boolean'));
      if(feed.malformed!==null)attentionCount(feed.malformed);
      attentionAssert(typeof feed.read_error_observed==='boolean'&&
        Boolean(feed.truncated||feed.incomplete||feed.malformed||feed.read_error_observed));
    });
  }else{
    attentionObject(value,['recorded_state','record_version','additional_payload_reverification_performed']);
    attentionText(value.recorded_state,128);attentionAssert(attentionCount(value.record_version)>0);
    const state=localAttentionProfile.metadata_states[value.recorded_state];
    attentionAssert(attentionOwn(localAttentionProfile.metadata_states,value.recorded_state)&&
      state[0]===kind&&state[1]===item.reason_code&&value.additional_payload_reverification_performed===false);
  }
}
function attentionItem(item,sources,ids,references){
  attentionObject(item,['attention_id','source_id','project','target','reason_code','summary','status',
    'basis','evidence','root_cause','operator_action','capabilities']);
  attentionId(item.attention_id,'at');attentionId(item.source_id,'src',24);
  attentionAssert(sources.has(item.source_id)&&!ids.has(item.attention_id));ids.add(item.attention_id);
  attentionObject(item.target,['kind','record_id']);
  attentionAssert(attentionOwn(attentionKinds,item.target.kind));
  attentionId(item.target.record_id,attentionKinds[item.target.kind],item.target.kind==='sources'?24:32);
  attentionAssert(item.target.kind!=='sources'||item.target.record_id===item.source_id);
  attentionText(item.reason_code,80);attentionText(item.project,240);attentionText(item.summary,1024);
  attentionAssert(item.status==='observed_attention_fact'&&item.root_cause==='not_verified'&&
    item.operator_action==='not_selected_or_authorized'&&item.basis==='already_admitted_bounded_metadata_not_live_preflight');
  const capabilities=['control','resume','merge','cleanup','feedback_upload','budget_or_deadline_reset'];
  attentionObject(item.capabilities,capabilities);
  attentionAssert(capabilities.every(key=>item.capabilities[key]===false));
  attentionEvidence(item);
  const reference=JSON.stringify([item.source_id,item.target.kind,item.target.record_id,item.reason_code]);
  attentionAssert(!references.has(reference));references.add(reference);
}
function attentionCoverage(value,view){
  attentionObject(value,['task_candidates','source_records','snapshot_diagnostics','task_window_partial','basis']);
  attentionAssert(attentionCount(value.task_candidates)===view.observed_task_count&&
    attentionCount(value.source_records,64)===view.sources.length&&
    attentionCount(value.snapshot_diagnostics)===view.diagnostics.length&&typeof value.task_window_partial==='boolean'&&
    value.basis==='admitted_candidate_windows_before_task_limit_and_selected_metadata_not_lifetime_or_complete_inventory');
}
function admitAttention(view,chosen=''){
  // Missing legacy projection is untracked. Null/false/unsupported is not legacy.
  if(!attentionOwn(view,'attention'))return;
  attentionAssert(view.schema_version===1&&view.window_only===true&&Array.isArray(view.sources)&&
    view.sources.length<=64&&Array.isArray(view.diagnostics));
  attentionCount(view.observed_task_count);
  const sources=new Set();
  view.sources.forEach(source=>{
    attentionAssert(source!==null&&typeof source==='object');attentionId(source.source_id,'src',24);
    attentionAssert(!sources.has(source.source_id)&&(!chosen||source.source_id===chosen));sources.add(source.source_id);
  });
  const value=attentionObject(view.attention,['schema_version','method','items','observed_items','omitted_items',
    'counts_by_reason','counts_by_kind','window_only','coverage','ordering','grants_execution',
    'zero_items_proves_safety_or_no_missing_work','note']);
  attentionAssert(value.schema_version===1&&value.method===localAttentionProfile.method&&value.window_only===true&&
    value.grants_execution===false&&value.zero_items_proves_safety_or_no_missing_work===false&&
    value.ordering==='stable_full_attention_id_not_severity_recency_or_efficiency_ranking');
  attentionText(value.note,4096);
  attentionAssert(Array.isArray(value.items)&&value.items.length<=localAttentionProfile.max_items);
  const observed=attentionCount(value.observed_items),omitted=attentionCount(value.omitted_items);
  attentionAssert(observed===value.items.length+omitted);
  const reasons=Object.values(localAttentionProfile.task_states).concat(
    Object.values(localAttentionProfile.metadata_states).map(value=>value[1]),['source_feed_gap']);
  attentionTotals(value.counts_by_reason,reasons,observed);
  attentionTotals(value.counts_by_kind,Object.keys(attentionKinds),observed);
  attentionCoverage(value.coverage,view);
  const ids=new Set(),references=new Set(),counts={};let previous='';
  value.items.forEach(item=>{
    attentionItem(item,sources,ids,references);
    attentionAssert(previous<item.attention_id);previous=item.attention_id;
    for(const [axis,key] of [['counts_by_kind',item.target.kind],['counts_by_reason',item.reason_code]]){
      const bucket=axis+':'+key;counts[bucket]=(counts[bucket]||0)+1;
      attentionAssert(attentionOwn(value[axis],key)&&counts[bucket]<=value[axis][key]);
    }
  });
}
""".replace("__LOCAL_ATTENTION_PROFILE__", json.dumps(_PROFILE, ensure_ascii=True, sort_keys=True))
