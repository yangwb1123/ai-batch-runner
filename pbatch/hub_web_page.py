"""Credential-free ABR landing page for the SDK-backed read-only browser facade."""

from __future__ import annotations

import base64
import hashlib

_STYLE = """body{font:1rem system-ui;margin:2rem auto;padding:0 1rem;max-width:70rem;color:#222}
button{font:inherit;padding:.6rem;margin:.2rem}form{display:inline}table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:.6rem;border-bottom:1px solid #ccc;overflow-wrap:anywhere}
.wrap{overflow:auto}p{line-height:1.6}#notice{min-height:3rem}@media(max-width:600px){body{margin:1rem auto}td{min-width:7rem}}"""

_SCRIPT = r"""'use strict';
const $=id=>document.getElementById(id);
let busy=false;
function clear(){for(const id of ['instances','sessions'])$(id).replaceChildren();}
const text=value=>String(value??'未追踪').replace(/[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]/g,' ').slice(0,256);
async function read(path,signal){
  const response=await fetch(path+'?limit=100',{credentials:'same-origin',cache:'no-store',redirect:'error',signal});
  if(!response.ok)throw Error('HTTP '+response.status);
  const reader=response.body.getReader(),parts=[];let size=0;
  while(true){const {done,value}=await reader.read();if(done)break;size+=value.byteLength;
    if(size>2*1024*1024||parts.length>=1024){await reader.cancel();throw Error('limit');}parts.push(value);}
  const bytes=new Uint8Array(size);let offset=0;
  for(const part of parts){bytes.set(part,offset);offset+=part.byteLength;}
  const result=JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(bytes));
  if(!Array.isArray(result.data)||result.data.length>100)throw Error('schema');
  return result.data;
}
function rows(id,items,fields){
  for(const item of items){const row=document.createElement('tr');
    for(const field of fields){const cell=document.createElement('td');cell.textContent=text(item[field]);row.appendChild(cell);}
    $(id).appendChild(row);}
}
async function refresh(){
  if(busy)return;busy=true;clear();$('refresh').disabled=true;
  $('notice').textContent='正在重新鉴权读取；旧记录已清除。';
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),12000);
  try{
    const instances=await read('/web/api/instances',controller.signal);
    const sessions=await read('/web/api/sessions',controller.signal);
    rows('instances',instances,['instance_id','name','project_id','online']);
    rows('sessions',sessions,['session_id','instance_id','name','status']);
    $('notice').textContent='已读取授权窗口：实例 '+instances.length+'，会话 '+sessions.length+
      '。每类最多 100 条，不代表完整清单；在线仅为心跳推断，执行成功不等于验收。';
  }catch(error){clear();$('notice').textContent='当前数据不可用：请登录并确认 Hub 授权。可能是会话过期、权限不足或服务故障；不保留旧事实。';}
  finally{clearTimeout(timer);busy=false;$('refresh').disabled=false;}
}
$('refresh').addEventListener('click',refresh);
refresh();
"""


def page():
    return ("<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'><title>abr · 工作台</title>"
            "<style>" + _STYLE + "</style></head><body><h1>abr · 工作台</h1>"
            "<p>通过 Snaplink 登录，仅查看明确授权的实例与会话。本阶段不提供执行、取消或修复操作。</p>"
            "<form action='/auth/login' method='post'><button>使用 Snaplink 登录</button></form>"
            "<form action='/auth/logout' method='post'><button>退出本地 abr 会话</button></form>"
            "<button id='refresh'>刷新授权窗口</button><p id='notice' role='status' aria-live='polite'>未读取。</p>"
            "<h2>实例</h2><div class='wrap'><table><thead><tr><th>ID</th><th>名称</th><th>项目</th><th>心跳在线推断</th>"
            "</tr></thead><tbody id='instances'></tbody></table></div>"
            "<h2>会话</h2><div class='wrap'><table><thead><tr><th>ID</th><th>实例</th><th>声明名称</th><th>记录状态</th>"
            "</tr></thead><tbody id='sessions'></tbody></table></div>"
            "<p>登录不自动授予项目权限。费用、独立验收及审计入账未在此页面追踪，不显示为零或通过。"
            "退出仅清除本地 abr 会话，不注销 Snaplink 全局登录，不取消已发出的任务。</p>"
            "<script>" + _SCRIPT + "</script></body></html>")


def security_policy(login_origin):
    def digest(value):
        return base64.b64encode(hashlib.sha256(value.encode()).digest()).decode("ascii")
    return ("default-src 'none'; script-src 'sha256-" + digest(_SCRIPT) + "'; style-src 'sha256-" +
            digest(_STYLE) + "'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self' " + login_origin)
