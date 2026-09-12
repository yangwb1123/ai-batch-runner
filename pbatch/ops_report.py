"""HTML report projection for the operations console."""

from __future__ import annotations

import html as html_mod
from pathlib import Path

def _breaker_label(pb_state: dict) -> str:
    active = [str(item)[:48] for item in
              (pb_state.get("active_providers") or [])[:4]]
    scope = f" providers={','.join(active)}" if active else ""
    if pb_state.get("active"):
        return (f"ACTIVE cooldown {pb_state.get('cooldown_remaining_s', 0)}s "
                f"(step {pb_state.get('step', 0)}){scope}")
    count = pb_state.get("provider_count", 0)
    count_label = f" providers={count}" if count > 1 else ""
    return (f"ok (streak {pb_state.get('streak', 0)}/"
            f"{pb_state.get('threshold', '-')}){count_label}")


def _runs_table(rows: list, esc, status_class: dict) -> str:
    return "\n".join(
        f"<tr class='{status_class.get(row['status'], '')}'><td>{esc(row['time'])}"
        f"</td><td>{esc(row['agent'])}</td><td>{esc(row['status'])}</td>"
        f"<td>#{row['attempt']}</td><td>{esc(row['task'])}</td>"
        f"<td>{esc('; '.join(row['outputs']))}</td></tr>"
        for row in rows) or "<tr><td colspan=6>no runs yet</td></tr>"


_MOBILE_PANEL_SCRIPT = """<script>
const mobileControl=__ENABLED__;
const mobileToken=new URLSearchParams(location.search).get('token')||'';
const mobileApi=(path)=>path+(mobileToken?(path.includes('?')?'&':'?')+'token='+encodeURIComponent(mobileToken):'');
const sessionSelect=document.getElementById('session-select');
const sessionDetail=document.getElementById('session-detail');
const sessionPrompt=document.getElementById('session-prompt');
const sessionName=document.getElementById('session-name');
const createButton=document.getElementById('session-create');
const sendButton=document.getElementById('session-send');
const cancelButton=document.getElementById('session-cancel');
let selectedSession='';
let selectedControllable=false;
function updateControls() {
  const enabled=mobileControl && selectedControllable;
  sessionPrompt.disabled=!enabled;
  sendButton.disabled=!enabled;
  cancelButton.disabled=!enabled;
}
async function mobileJson(path, options) {
  const response=await fetch(mobileApi(path), options||{});
  const value=await response.json();
  if (!response.ok) throw new Error(value.error||('HTTP '+response.status));
  return value;
}
async function refreshSessions() {
  try {
    const value=await mobileJson('/api/sessions?limit=50');
    const rows=value.sessions||[];
    const previous=selectedSession;
    sessionSelect.innerHTML='';
    rows.forEach((row)=>{
      const option=document.createElement('option');
      option.value=row.session_id;
      option.dataset.controllable=row.controllable?'1':'0';
      option.textContent=(row.name||row.session_id)+' · '+(row.status||'unknown')+
        (row.controllable?' · 可控':' · 只读');
      sessionSelect.appendChild(option);
    });
    if (rows.some((row)=>row.session_id===previous)) sessionSelect.value=previous;
    selectedSession=sessionSelect.value||'';
    selectedControllable=sessionSelect.selectedOptions[0]?.dataset.controllable==='1';
    updateControls();
    await refreshDetail();
  } catch (error) { sessionDetail.textContent='会话加载失败: '+error.message; }
}
async function refreshDetail() {
  selectedSession=sessionSelect.value||selectedSession;
  selectedControllable=sessionSelect.selectedOptions[0]?.dataset.controllable==='1';
  updateControls();
  if (!selectedSession) return;
  try {
    const value=await mobileJson('/api/sessions/'+encodeURIComponent(selectedSession));
    sessionDetail.textContent=JSON.stringify(value,null,2).slice(-16000);
  } catch (error) { sessionDetail.textContent='详情加载失败: '+error.message; }
}
sessionSelect.addEventListener('change',refreshDetail);
if (mobileControl) {
  createButton.addEventListener('click',async()=>{
    createButton.disabled=true;
    try {
      await mobileJson('/api/sessions',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({name:sessionName.value})});
      sessionName.value=''; await refreshSessions();
    } catch (error) { alert(error.message); } finally { createButton.disabled=false; }
  });
  sendButton.addEventListener('click',async()=>{
    if (!selectedSession||!sessionPrompt.value.trim()) return;
    sendButton.disabled=true;
    try {
      await mobileJson('/api/sessions/'+encodeURIComponent(selectedSession)+'/turn',{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({prompt:sessionPrompt.value})});
      sessionPrompt.value=''; await refreshDetail();
    } catch (error) { alert(error.message); } finally { sendButton.disabled=false; }
  });
  cancelButton.addEventListener('click',async()=>{
    if (!selectedSession) return;
    try { await mobileJson('/api/sessions/'+encodeURIComponent(selectedSession)+'/cancel',
      {method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); }
    catch (error) { alert(error.message); }
  });
}
refreshSessions();
setInterval(refreshSessions,5000);
</script></section>"""


def _mobile_panel(control_enabled: bool) -> str:
    """Return a dependency-free responsive session picker and command form."""
    disabled = "" if control_enabled else " disabled"
    note = ("指令已启用：只会发送到服务端创建的精确会话。"
            if control_enabled else "当前为只读模式；启动 ops serve --control 才能发送指令。")
    header = f"""<section id='mobile-control'>
<h2>移动端会话</h2><p class='meta'>{note}</p>
<select id='session-select' aria-label='session'><option>加载中…</option></select>
<input id='session-name' placeholder='新会话名称（可选）'{disabled}>
<button id='session-create'{disabled}>新建会话</button>
<pre id='session-detail' class='session-detail'>选择会话查看详情</pre>
<textarea id='session-prompt' rows='4' placeholder='发送指令…'{disabled}></textarea>
<button id='session-send'{disabled}>发送指令</button>
<button id='session-cancel'{disabled}>停止当前指令</button>
"""
    return header + _MOBILE_PANEL_SCRIPT.replace(
        "__ENABLED__", "true" if control_enabled else "false")


def _report_head(cwd: str, summary: dict, pb_label: str,
                 statuses: str, agents: str) -> str:
    """Render the bounded report header and responsive styles."""
    name = html_mod.escape(str(Path(cwd).name), quote=True)
    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>pi-batch ops · {name}</title>
<style>
body{{font-family:system-ui,sans-serif;margin:2rem;max-width:1100px}}
h1{{font-size:1.3rem}} h2{{font-size:1rem;margin-top:2rem}}
.chip{{display:inline-block;margin:.15rem;padding:.1rem .5rem;border-radius:999px;
background:#f0f0f0;font-size:.85rem}} .ok{{color:#0a7d2f}}.bad{{color:#c0392b}}
.warn{{color:#b26a00}} table{{border-collapse:collapse;width:100%;font-size:.85rem}}
td,th{{border:1px solid #ddd;padding:.3rem .5rem;text-align:left;
overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:340px}}
tr.ok td:first-child{{}} tr.bad{{background:#fff3f3}} tr.warn{{background:#fff8e8}}
.meta{{color:#666;font-size:.85rem}}
pre.log{{background:#f6f6f6;padding:.5rem;overflow-x:auto;font-size:.75rem;
max-height:24em}}
#mobile-control{{border:1px solid #ddd;border-radius:.6rem;padding:1rem;margin-top:1rem}}
#mobile-control select,#mobile-control textarea{{display:block;width:100%;box-sizing:border-box;
margin:.5rem 0;padding:.6rem;font:inherit}}#mobile-control button{{padding:.6rem .9rem;
margin:.25rem .25rem .25rem 0}}.session-detail{{max-height:20em;overflow:auto;background:#f6f6f6;
white-space:pre-wrap;word-break:break-word}}
</style></head><body>
<h1>pi-batch · 运营控制台 · {name}</h1>
<p class="meta">生成于 {html_mod.escape(str(summary['generated_at']))} · 共 {summary['total']} 条 run
· 被拒 {summary['rejected_count']}（自我修复诊断见 {html_mod.escape(str(summary['self_healing']['rejected_artifacts_in']))}）</p>
<p class="meta">Provider breaker: {pb_label}</p>
<h2>状态分布</h2><div>{statuses}</div>
<h2>按 Agent</h2><div>{agents}</div>
<h2>最近 Runs（含重试 attempt）</h2>
"""


def _report_tail(rows_html: str, log_escaped: str, mobile_panel: str) -> str:
    """Render the bounded report tables and mobile control panel."""
    return f"""<table><thead><tr><th>时间</th><th>agent</th><th>状态</th><th>#</th><th>任务</th>
<th>产物</th></tr></thead><tbody>{rows_html}</tbody></table>
<h2>运营日志（尾部，ops log / api/log 可过滤）</h2>
<pre class="log">{log_escaped}</pre>
{mobile_panel}
<p class="meta">刷新本页即可看到最新状态；移动端会话每 5 秒轮询一次。</p>
</body></html>"""


def report_html(data_dir: str, cwd: str, *, control_enabled: bool = False) -> str:
    from .ops import _recent_log_lines, overview, runs
    summary = overview(data_dir)
    rows = runs(data_dir, limit=50)
    log_lines, _log_error = _recent_log_lines(cwd, tail=30)
    status_class = {"succeeded": "ok", "failed": "bad", "rejected": "warn",
                    "cancelled": "warn"}

    def esc(value: object) -> str:
        return html_mod.escape(str(value), quote=True)

    rows_html = _runs_table(rows, esc, status_class)
    log_escaped = esc("\n".join(log_lines) or
                       _log_error or "(no --log-file log found)")
    pb_label = html_mod.escape(
        _breaker_label(summary.get("provider_breaker") or {}), quote=True)
    statuses = " ".join(
        f"<span class='chip {status_class.get(k, '')}'>{esc(k)}={v}</span>"
        for k, v in summary["status_counts"].items()) or "—"
    agents = " ".join(
        f"<span class='chip'>{esc(k)}={v}</span>"
        for k, v in summary["by_agent"].items()) or "—"
    mobile_panel = _mobile_panel(control_enabled)
    return _report_head(cwd, summary, pb_label, statuses, agents) + _report_tail(
        rows_html, log_escaped, mobile_panel)
