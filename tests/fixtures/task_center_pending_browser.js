'use strict';
// Executes the delivered local-page script; only declared in-memory responses.
const assert=require('node:assert/strict');
const vm=require('node:vm');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
class Element {
  constructor(tag){this.tagName=tag;this.children=[];this.listeners={};this.value='';this.hidden=false;this._text='';}
  set textContent(value){this._text=String(value);this.children=[];}
  get textContent(){return this._text+this.children.map(child=>child.textContent).join('\n');}
  set innerHTML(value){throw new Error('HTML assignment refused');}
  appendChild(child){this.children.push(child);return child;}
  replaceChildren(...children){this._text='';this.children=children;}
  addEventListener(event,listener){this.listeners[event]=listener;}
  emit(event){if(this.listeners[event])this.listeners[event]();}
  descendants(tag){return this.children.flatMap(c=>[...(c.tagName===tag?[c]:[]),...c.descendants(tag)]);}
}
const ids={};
for(const id of ['source','tasks','goals','intents','repairs','resources','feedback','attention',
  'attention-summary','activity','detail','intent-detail','summary','error','refresh']){
  ids[id]=new Element(id==='source'?'select':'section');
}
const requests=[],timers=[];
const context=vm.createContext({document:{getElementById:id=>ids[id],createElement:tag=>new Element(tag)},
  location:{search:''},URLSearchParams,AbortController,
  Option:function(text,value){const e=new Element('option');e.textContent=text;e.value=value;return e;},
  fetch:(path,options)=>new Promise((resolve,reject)=>requests.push({path,options,resolve,reject})),
  setInterval:(callback,delay)=>timers.push({callback,delay})});
vm.runInContext(input.script,context,{timeout:1000});
const evaluate=script=>vm.runInContext(script,context,{timeout:1000});
const tick=()=>new Promise(resolve=>setImmediate(resolve));
async function respond(index,view){requests[index].resolve({ok:true,status:200,json:async()=>view});await tick();await tick();}
function text(){return Object.values(ids).map(e=>e.textContent).join('\n');}
function result(){return {tasks:ids.tasks.textContent,goals:ids.goals.textContent,activity:ids.activity.textContent,
  detail:ids.detail.textContent,summary:ids.summary.textContent,error:ids.error.textContent,all:text(),
  tags:Object.values(ids).flatMap(e=>e.descendants('script').concat(e.descendants('img'))).length,
  requests:requests.map(r=>({path:r.path,method:r.options.method||'GET',aborted:r.options.signal.aborted}))};}
async function main(){
  assert.equal(requests.length,1);assert.equal(timers[0].delay,5000);
  await respond(0,input.views[0]);
  assert(text().includes(input.old_marker));
  if(input.select)ids[input.select].descendants('button')[0].emit('click');
  if(input.timer)timers[0].callback();else evaluate('refresh(true)');
  assert.equal(requests.length,2);
  const pending=result();
  for(const key of ['tasks','goals','activity'])assert.equal(pending[key],'','old '+key+' remained during refresh');
  assert(!pending.all.includes(input.old_marker),'old facts/detail/summary remained during refresh');
  assert(pending.summary.includes('等待'),'summary still claimed an old completed read');
  if(input.mode==='late_success'||input.mode==='late_error'){
    evaluate('refresh(true)');assert.equal(requests.length,3);
    await respond(2,input.views[1]);
    if(input.mode==='late_success')await respond(1,input.views[0]);
    else{requests[1].reject(new Error('old request failed'));await tick();await tick();}
    assert(!text().includes(input.old_marker));assert(text().includes(input.new_marker));
  }else if(input.mode==='failure'){
    requests[1].reject(new Error('fixture unavailable'));await tick();await tick();
    assert(!text().includes(input.old_marker));
  }else if(input.mode==='complete'){
    await respond(1,input.views[1]);
    assert(!text().includes(input.old_marker));assert(text().includes(input.new_marker));
  }
  const output={pending,final:result()};
  assert(output.final.requests.every(r=>r.method==='GET'));assert.equal(output.final.tags,0);
  console.log(JSON.stringify(output));
}
main().catch(error=>{console.error(error.stack);process.exitCode=1;});
