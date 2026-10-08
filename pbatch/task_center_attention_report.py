"""Text-only attention rendering from the existing shared JSON response. No actions or extra fetches."""

ATTENTION_SCRIPT = """
let selectedAttention='';
function clearAttention(){
  document.getElementById('attention').replaceChildren();
  document.getElementById('attention-summary').textContent='等待新快照；不保留旧关注事实。';
}
function showAttention(item){
  selected='';selectedGoal='';selectedIntent='';selectedRepair='';selectedResource='';selectedFeedback='';
  clearIntentPanel();selectedAttention=item.attention_id;detail.textContent=JSON.stringify(item,null,2);
}
function renderAttention(view){
  const value=view.attention;
  if(!value){clearAttention();document.getElementById('attention-summary').textContent='关注事实未追踪；不推断零或安全。';return;}
  document.getElementById('attention-summary').textContent='已观察 '+shown(value.observed_items)+
    ' 项；引用遗漏 '+shown(value.omitted_items)+'；来源诊断 '+shown(value.coverage.snapshot_diagnostics)+
    ' 条（不是不同缺陷/根因数；零项不证明完整覆盖或无需人工判断）';
  value.items.forEach(item=>{
    const tr=document.createElement('tr');
    cell(tr,item.project+' / '+item.source_id);
    cell(tr,item.target.kind+' / '+item.target.record_id);
    cell(tr,item.reason_code+'；'+item.summary+'；非干预授权');
    const button=document.createElement('button');button.textContent='观察引用（只读）';
    button.addEventListener('click',()=>{if(!busy)showAttention(item);});
    cell(tr,'').appendChild(button);document.getElementById('attention').appendChild(tr);
  });
  if(selectedAttention){const item=value.items.find(row=>row.attention_id===selectedAttention);
    detail.textContent=item?JSON.stringify(item,null,2):'关注引用不在本次窗口；不代表已解决、仍有效或可干预。';}
}
"""
