'use strict';
// Delivered local page script with explicit synthetic responses, no real fetch.
const assert=require('node:assert/strict'), vm=require('node:vm');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
class Element {
  constructor(tag){this.tagName=tag;this.children=[];this.listeners={};this.value='';this.hidden=false;this._text='';}
  set textContent(value){this._text=String(value);this.children=[];}
  get textContent(){return this._text+this.children.map(c=>c.textContent).join('\n');}
  set innerHTML(value){throw new Error('HTML injection refused');}
  appendChild(child){this.children.push(child);return child;}
  replaceChildren(...children){this._text='';this.children=children;}
  addEventListener(type,listener){this.listeners[type]=listener;}
  emit(type){if(this.listeners[type])this.listeners[type]();}
  descendants(tag){return this.children.flatMap(c=>[...(c.tagName===tag?[c]:[]),...c.descendants(tag)]);}
}
const ids={};
for(const id of ['source','tasks','goals','intents','repairs','resources','feedback','attention',
 'attention-summary','activity','detail','intent-detail','summary','error','refresh']){
 ids[id]=new Element(id==='source'?'select':'section');
}
const requests=[],timers=[];
const context=vm.createContext({document:{getElementById:id=>ids[id],createElement:tag=>new Element(tag)},
 location:{search:'?token=owned-synthetic-token'},URLSearchParams,AbortController,
 Option:function(text,value){const item=new Element('option');item.textContent=text;item.value=value;return item;},
 fetch:(path,options)=>new Promise((resolve,reject)=>requests.push({path,options,resolve,reject})),
 setInterval:(callback,delay)=>timers.push({callback,delay})});
vm.runInContext(input.script,context,{timeout:1000});
const evaluate=script=>vm.runInContext(script,context,{timeout:1000});
const tick=()=>new Promise(resolve=>setImmediate(resolve));
async function respond(index,view){requests[index].resolve({ok:true,status:200,json:async()=>view});await tick();await tick();}
function all(){return Object.values(ids).map(e=>e.textContent).join('\n');}
function output(){return {all:all(),detail:ids.detail.textContent,error:ids.error.textContent,
 summary:ids.summary.textContent,attention:ids.attention.textContent,
 attention_summary:ids['attention-summary'].textContent,rows:ids.attention.children.length,
 requests:requests.map(r=>({path:r.path,method:r.options.method||'GET',aborted:r.options.signal.aborted})),
 tags:Object.values(ids).flatMap(e=>e.descendants('script').concat(e.descendants('img'))).length};}
async function main(){
 assert.equal(requests.length,1);assert.equal(timers[0].delay,5000);
 await respond(0,input.views[0]);
 assert(ids.attention.children.length>0);ids.attention.descendants('button')[0].emit('click');
 assert(ids.detail.textContent.includes(input.old_marker));
 if(input.source){ids.source.value=input.source;ids.source.emit('change');}
 else evaluate('refresh(true)');
 assert(!all().includes(input.old_marker));assert.equal(requests.length,2);
 await respond(1,input.views[1]);
 if(input.mode==='refuse'||input.mode.startsWith('late_')){
  assert.equal(ids.summary.textContent,'数据不可用','malformed attention was admitted');
  for(const id of ['tasks','goals','intents','repairs','resources','feedback','activity','attention']){
   assert.equal(ids[id].children.length,0,'invalid attention published '+id+' facts');
  }
  assert(!all().includes(input.old_marker));assert(!all().includes(input.private_marker));
  if(input.mode.startsWith('late_')){
   evaluate('refresh(true)');evaluate('refresh(true)');
   await respond(3,input.views[1]);
   if(input.mode==='late_success')await respond(2,input.views[0]);
   else{requests[2].reject(new Error(input.private_marker));await tick();await tick();}
   assert.equal(ids.summary.textContent,'数据不可用');assert(!all().includes(input.old_marker));
   assert(!all().includes(input.private_marker));
  }
 }else{
  assert.equal(ids.error.textContent,input.expected_error||'');
  assert.equal(ids.attention.children.length,input.expected_rows);
  if(input.mode==='legacy'){
   assert(ids['attention-summary'].textContent.includes('未追踪'));
   assert(!ids['attention-summary'].textContent.includes('已观察 0'));
  }else if(input.expected_rows){ids.attention.descendants('button')[0].emit('click');}
 }
 const result=output();assert.equal(result.tags,0);assert(result.requests.every(r=>r.method==='GET'));
 console.log(JSON.stringify(result));
}
main().catch(error=>{console.error(error.stack);process.exitCode=1;});
