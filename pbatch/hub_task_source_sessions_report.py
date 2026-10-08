"""Explicit source-session Web profile; literal identity references, no follow-up fetch/control."""
from __future__ import annotations

from .hub_task_source_sessions import METHOD, BASIS, SCOPES

_CHECKS = r"""
function sourceReferenceBrief(ref,hub,tenant){
  exact(ref,'schema_version reference_id basis state target liveness parent_execution_verified goal_binding_verified deduplication_authority billing_addition_authority grants_execution');
  if(ref.schema_version!==1||!full(ref.reference_id,'hr',32)||ref.basis!==REF_BASIS||ref.liveness!=='not_verified'||
    ['parent_execution_verified','goal_binding_verified','deduplication_authority','billing_addition_authority','grants_execution'].some(key=>ref[key]!==false))throw Error('reference authority');
  const target=ref.target;
  if(ref.state==='unresolved_in_authorized_window'){
    if(target!==null)throw Error('unresolved reference');
  }else if(ref.state==='observed_recorded_reference'){
    exact(target,'namespace source_id task_id tenant_id instance_id session_id reported_generation last_recorded_at');
    if(target.namespace!=='hub-native-task-facts-v1'||!full(target.source_id,'src',24)||!full(target.task_id,'tc',32)||
      target.tenant_id!==tenant||target.instance_id!==hub.instance_id||target.session_id!==hub.session_id||
      target.reported_generation!==hub.reported_generation||!Number.isFinite(target.last_recorded_at)||target.last_recorded_at<0)throw Error('reference identity');
  }else throw Error('reference state');
  return {schema_version:1,reference_id:ref.reference_id,state:ref.state,basis:REF_BASIS,target,
    parent_execution_verified:false,goal_binding_verified:false,deduplication_authority:false,billing_addition_authority:false,
    grants_execution:false,liveness:'not_verified'};
}
function referenceScope(scope,value){
  if(JSON.stringify(scope.required_scopes)!==JSON.stringify(REF_SCOPES)||scope.sessions!=='bounded_source_session_reference_metadata_not_native_task_inventory')throw Error('reference scopes');
  const summary=scope.source_session_references;
  exact(summary,'basis native_rows_observed native_window_partial compute_window_partial task_references_observed matched_recorded_references unresolved_references unresolved_means count_basis local_ledger_mapping target_native_display_presence parent_execution_verified deduplication_authority billing_addition_authority');
  if(summary.basis!==REF_BASIS||['native_rows_observed','task_references_observed','matched_recorded_references','unresolved_references'].some(key=>!integer(summary[key]))||
    summary.native_rows_observed>512||summary.task_references_observed!==value.observed_task_count||value.observed_task_count>512||
    summary.matched_recorded_references+summary.unresolved_references!==summary.task_references_observed||
    typeof summary.native_window_partial!=='boolean'||typeof summary.compute_window_partial!=='boolean'||
    scope.partial_window!==(summary.native_window_partial||summary.compute_window_partial)||
    (summary.native_window_partial&&summary.native_rows_observed!==512)||(summary.matched_recorded_references&&!summary.native_rows_observed)||
    ['parent_execution_verified','deduplication_authority','billing_addition_authority'].some(key=>summary[key]!==false)||
    summary.local_ledger_mapping!=='untracked_not_read_or_imported'||summary.target_native_display_presence!=='not_checked')throw Error('reference counts/authority');
  let matched=0;const targets=new Set();
  for(const task of value.tasks){
    const ref=sourceReferenceBrief(task.source_session_reference,task.hub,scope.tenant_id);
    if(ref.target){++matched;targets.add(ref.target.task_id);}
  }
  if(matched>summary.matched_recorded_references||value.tasks.length-matched>summary.unresolved_references||targets.size>summary.native_rows_observed)throw Error('retained reference counts');
}
"""


def script(base: str) -> str:
    replacements = {
        "hub-compute-task-facts-v1": METHOD,
        "/api/v1/agent/compute-task-center?": "/api/v1/agent/compute-task-center/source-sessions?",
        "analysis observations capabilities');": "analysis observations capabilities source_session_reference');",
        "const scope=value.scope;": "const scope=value.scope;referenceScope(scope,value);",
        "live_liveness:'not_verified',capabilities:": "live_liveness:'not_verified',source_session_reference:sourceReferenceBrief(item.source_session_reference,hub,tenant),capabilities:",
    }
    for old, new in replacements.items():
        if old not in base:
            raise ValueError("source-session Web template contract changed")
        base = base.replace(old, new)
    import json
    return base + "\nconst REF_BASIS=" + json.dumps(BASIS) + ";const REF_SCOPES=" + json.dumps(SCOPES) + ";\n" + _CHECKS
