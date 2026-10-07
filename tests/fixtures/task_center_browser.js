'use strict';
// Minimal DOM + isolated VM: executes the delivered inline script, never fetches a real URL.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));

class Element {
  constructor(tag) { this.tagName=tag; this.children=[]; this.listeners={}; this.value=''; this.hidden=false; this._text=''; }
  set textContent(value) { this._text=String(value); this.children=[]; }
  get textContent() { return this._text+this.children.map(child=>child.textContent).join('\n'); }
  set innerHTML(value) { throw new Error('HTML injection is forbidden'); }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...children) { this._text=''; this.children=children; }
  addEventListener(type, listener) { this.listeners[type]=listener; }
  emit(type) { if(this.listeners[type])this.listeners[type](); }
  descendants(tag) {
    return this.children.flatMap(child=>[...(child.tagName===tag?[child]:[]),...child.descendants(tag)]);
  }
}
const ids={};
for(const id of ['source','tasks','goals','intents','repairs','resources','feedback','activity','detail','intent-detail','summary','error','refresh']) {
  ids[id]=new Element(id==='source'?'select':'section');
}
ids['intent-detail'].hidden=true;
const requests=[],intervals=[];
const context=vm.createContext({
  document:{getElementById:id=>ids[id],createElement:tag=>new Element(tag)},
  location:{search:'?token=fixture-token'},URLSearchParams,AbortController,
  Option:function(text,value){const item=new Element('option');item.textContent=text;item.value=value;return item;},
  fetch:(path,options)=>new Promise((resolve,reject)=>requests.push({path,options,resolve,reject})),
  setInterval:(callback,delay)=>intervals.push({callback,delay})
});
vm.runInContext(input.script,context,{timeout:1000});
const evaluate=script=>vm.runInContext(script,context,{timeout:1000});
const tick=()=>new Promise(resolve=>setImmediate(resolve));
async function respond(index,view) {
  requests[index].resolve({ok:true,status:200,json:async()=>view});
  await tick();await tick();
}
function selectIntent() { ids.intents.descendants('button')[0].emit('click'); }
function output() {
  return {intents:ids.intents.textContent,repairs:ids.repairs.textContent,resources:ids.resources.textContent,feedback:ids.feedback.textContent,panel:ids['intent-detail'].textContent,
    panel_hidden:ids['intent-detail'].hidden,detail:ids.detail.textContent,error:ids.error.textContent,
    summary:ids.summary.textContent,all:allText(),
    requests:requests.map(r=>({path:r.path,aborted:r.options.signal.aborted,
                              authorization:r.options.headers.Authorization||'',method:r.options.method||'GET'})),
    tags:Object.values(ids).flatMap(node=>node.descendants('script').concat(node.descendants('img'))).length};
}
function allText() {return Object.values(ids).map(node=>node.textContent).join('\n');}

async function main() {
  assert.equal(requests.length,1);
  assert.equal(intervals[0].delay,5000);
  if(input.mode==='switch'||input.mode==='stale_error') {
    ids.source.value=input.source;
    ids.source.emit('change');
    assert.equal(requests.length,2);
    assert.equal(requests[0].options.signal.aborted,true);
    assert.equal(ids.intents.children.length,0);
    assert.equal(ids['intent-detail'].hidden,true);
    await respond(1,input.views[1]);
    if(input.mode==='switch')await respond(0,input.views[0]);
    else {requests[0].reject(new Error('old request failed'));await tick();await tick();}
    assert(!allText().includes(input.old_marker));
    assert(ids.intents.textContent.includes(input.new_marker));
    console.log(JSON.stringify(output()));return;
  }
  await respond(0,input.views[0]);
  if(input.mode.startsWith('feedback')) {
    ids.feedback.descendants('button')[0].emit('click');
    const item=JSON.parse(ids.detail.textContent);
    assert.equal(item.execution_authority,'not_granted');
    assert.equal(item.copy.current_copy_reverified,false);
    assert.equal(item.capabilities.feedback_upload,false);
    assert.equal(item.stages.independent_reproduction,'not_performed');
    assert.equal(requests.length,1);
    if(input.mode==='feedback_select_other') {
      ids.tasks.descendants('button')[0].emit('click');
      evaluate('refresh(true)');await respond(1,input.views[1]);
      assert(!ids.detail.textContent.includes('feedback_id'));
    }else if(input.mode==='feedback_switch'||input.mode==='feedback_stale_error') {
      evaluate('refresh(true)');
      ids.source.value=input.source;ids.source.emit('change');
      assert.equal(requests[1].options.signal.aborted,true);
      assert.equal(ids.feedback.children.length,0);
      await respond(2,input.views[1]);
      if(input.mode==='feedback_switch')await respond(1,input.views[0]);
      else {requests[1].reject(new Error('old feedback request failed'));await tick();await tick();}
      assert(!allText().includes(input.old_marker));
    }else if(input.mode!=='feedback') {
      evaluate('refresh(true)');
      assert.equal(ids.feedback.children.length,0);
      assert(ids.detail.textContent.includes('不展示旧的缓存记录'));
      if(input.mode==='feedback_failure'){requests[1].reject(new Error('transport unavailable'));await tick();await tick();}
      else await respond(1,input.views[1]);
    }
  }else if(input.mode.startsWith('resource')) {
    ids.resources.descendants('button')[0].emit('click');
    const item=JSON.parse(ids.detail.textContent);
    assert.equal(item.cleanup_authority,'not_granted');
    assert.equal(item.plan.decision,'preserve');
    assert.equal(item.archive_verified,false);
    assert.equal(item.estimated_reclaimable_bytes,null);
    assert.equal(requests.length,1);
    if(input.mode==='resource_switch'||input.mode==='resource_stale_error') {
      evaluate('refresh(true)');
      ids.source.value=input.source;ids.source.emit('change');
      assert.equal(requests[1].options.signal.aborted,true);
      assert.equal(ids.resources.children.length,0);
      await respond(2,input.views[1]);
      if(input.mode==='resource_switch')await respond(1,input.views[0]);
      else {requests[1].reject(new Error('old resource request failed'));await tick();await tick();}
      assert(!allText().includes(input.old_marker));
    }else if(input.mode!=='resource') {
      evaluate('refresh(true)');
      assert.equal(ids.resources.children.length,0);
      assert(ids.detail.textContent.includes('不展示旧的缓存记录'));
      if(input.mode==='resource_failure') {requests[1].reject(new Error('transport unavailable'));await tick();await tick();}
      else await respond(1,input.views[1]);
    }
  }else if(input.mode.startsWith('request')) {
    ids.repairs.descendants('button')[0].emit('click');
    const item=JSON.parse(ids.detail.textContent);
    assert.equal(item.execution_authority,'not_granted');
    assert.equal(item.original_limits_verified,false);
    assert.equal(item.assessment.ready_to_execute,false);
    assert.equal(item.assessment.grants_execution,false);
    assert.equal(item.assessment.live_preflight_performed,false);
    assert.equal(requests.length,1);
    if(input.mode!=='request'){
      evaluate('refresh(true)');
      assert.equal(ids.repairs.children.length,0);
      assert(ids.detail.textContent.includes('不展示旧的缓存记录'));
      if(input.mode==='request_failure') {requests[1].reject(new Error('transport unavailable'));await tick();await tick();}
      else await respond(1,input.views[1]);
    }
  }else if(input.mode==='repair') {
    const button=ids.tasks.descendants('button')[0];
    assert.equal(button.textContent,'失败 / 恢复预览（只读）');
    button.emit('click');
    assert.equal(ids['intent-detail'].hidden,true);
    const task=JSON.parse(ids.detail.textContent);
    assert.equal(task.repair_preview.authority.grants_execution,false);
    assert.equal(task.repair_preview.authority.live_preflight_performed,false);
    assert.equal(requests.length,1);
  }else if(input.mode==='render') {
    selectIntent();
    assert.equal(ids['intent-detail'].hidden,false);
  }else if(input.mode==='refresh'||input.mode==='failure'||input.mode==='missing') {
    selectIntent();
    assert(ids['intent-detail'].textContent.includes(input.old_marker));
    evaluate('refresh(true)');
    assert.equal(ids.intents.children.length,0);
    assert.equal(ids['intent-detail'].hidden,true);
    assert(!allText().includes(input.old_marker));
    if(input.mode==='failure') {requests[1].reject(new Error('transport unavailable'));await tick();await tick();}
    else await respond(1,input.views[1]);
    assert(!allText().includes(input.old_marker));
  }else if(input.mode==='select_other') {
    selectIntent();
    ids[input.other].descendants('button')[0].emit('click');
    assert.equal(ids['intent-detail'].hidden,true);
    assert.equal(ids['intent-detail'].textContent,'');
  }else throw new Error('unknown fixture mode');
  assert.equal(output().tags,0);
  console.log(JSON.stringify(output()));
}
main().catch(error=>{console.error(error.stack);process.exitCode=1;});
