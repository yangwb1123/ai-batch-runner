'use strict';
const fs=require('fs'),vm=require('vm');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const nodes=new Map(),created=[],calls=[],pending=[],timers=[];
class Element{
  constructor(tag='node'){this.tag=tag;this.children=[];this.value='';this.textContent='';this.listeners={};}
  set innerHTML(_value){throw Error('HTML must never be interpreted');}
  addEventListener(type,listener){this.listeners[type]=listener;}
  appendChild(child){this.children.push(child);return child;}
  replaceChildren(){this.children=[];this.textContent='';}
}
for(const id of ['credentials','token','clear-token','source','refresh','error','summary','detail','tasks','attention'])nodes.set(id,new Element());
const document={getElementById:id=>nodes.get(id),createElement:tag=>{created.push(tag);return new Element(tag);}};
let phase=0,mode=input.mode;
function response(value,status=200){
  const bytes=new TextEncoder().encode(JSON.stringify(value));let read=false;
  return {ok:status===200,status,headers:{get:()=>String(bytes.length)},body:{getReader:()=>({read:async()=>{
    if(read)return {done:true};read=true;return {done:false,value:bytes};},cancel:async()=>{}})}};
}
function nextReply(){
  if(phase++===0)return response({data:input.initial});
  if(mode==='failure'||mode==='revoked')return response({error:{message:'PRIVATE-SERVER-ERROR'}},mode==='revoked'?403:503);
  if(mode==='malformed')return response({data:input.malformed});
  return response({data:input.next});
}
const context=vm.createContext({document,console,TextDecoder,TextEncoder,Uint8Array,URLSearchParams,AbortController,
  setInterval:callback=>{timers.push(callback);return 1;},setTimeout:()=>1,clearTimeout:()=>{},
  fetch:async(path,options)=>{calls.push({path,method:options.method,credentials:options.credentials,redirect:options.redirect,
    authorization_present:Boolean(options.headers.Authorization),keys:Object.keys(options.headers)});
    if((mode==='late'||mode==='late_error')&&calls.length===1)return await new Promise((resolve,reject)=>pending.push({resolve,reject}));
    return nextReply();}});
vm.runInContext(input.script,context,{timeout:2000});
const node=id=>nodes.get(id);
const tick=async()=>{for(let i=0;i<6;i++)await new Promise(resolve=>setImmediate(resolve));};
function emit(id,type){return node(id).listeners[type]({preventDefault(){}});}
function login(token='OWNED-SYNTHETIC-TOKEN'){node('token').value=token;emit('credentials','submit');}
function selectFirst(){const row=node('tasks').children[0];if(row)row.children.at(-1).children[0].listeners.click();}
async function run(){
  login();await tick();
  let cleared=false;
  if(mode==='late'||mode==='late_error'){
    node('source').value=input.source;emit('source','change');
    cleared=node('tasks').children.length===0;
    if(mode==='late')pending[0].resolve(response({data:input.initial}));else pending[0].reject(Error('OLD-PRIVATE-ERROR'));
    phase=1;await tick();
  }else{
    selectFirst();
    if(mode!=='normal'){
      if(mode==='credential'){
        node('token').value='OWNED-SECOND-TOKEN';emit('token','input');
        cleared=node('tasks').children.length===0&&!node('detail').textContent.includes('quoted');login('OWNED-SECOND-TOKEN');
      }else if(mode==='source'){
        node('source').value=input.source;emit('source','change');cleared=node('tasks').children.length===0;
      }else{
        emit('refresh','click');cleared=node('tasks').children.length===0&&!node('detail').textContent.includes('quoted');
      }
      await tick();
    }
  }
  if(mode==='revoked'){const count=calls.length;timers[0]();await tick();if(calls.length!==count)throw Error('auth failure must not loop');}
  const beforeClear={tasks:node('tasks').children.length,attention:node('attention').children.length,
    summary:node('summary').textContent,detail:node('detail').textContent,error:node('error').textContent};
  emit('clear-token','click');await tick();
  return {calls,created,cleared,beforeClear,afterClear:{tasks:node('tasks').children.length,attention:node('attention').children.length,
    detail:node('detail').textContent},storedTokenInput:node('token').value};
}
run().then(result=>process.stdout.write(JSON.stringify(result))).catch(error=>{console.error(error);process.exitCode=1;});
