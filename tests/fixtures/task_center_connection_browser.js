'use strict';
// Bounded DOM + VM for the delivered global shell script. No real network.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));

class Element {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.listeners = {};
    this.value = ''; this.hidden = false; this._text = '';
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join('\n'); }
  set innerHTML(value) { throw new Error('HTML injection is forbidden'); }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...children) { this._text = ''; this.children = children; }
  addEventListener(type, listener) { this.listeners[type] = listener; }
  emit(type, event) { if (this.listeners[type]) this.listeners[type](event || {}); }
  descendants(tag) {
    return this.children.flatMap(c => [...(c.tagName === tag ? [c] : []), ...c.descendants(tag)]);
  }
}
const IDS = ['source', 'tasks', 'goals', 'intents', 'repairs', 'resources', 'feedback',
  'attention', 'attention-summary', 'activity', 'detail', 'intent-detail', 'summary',
  'error', 'refresh', 'connection', 'token-form', 'token-input', 'connect'];
const ids = {};
for (const id of IDS) ids[id] = new Element(id === 'source' ? 'select' : 'section');
ids['intent-detail'].hidden = true;
ids['token-form'].hidden = false;

const requests = [], intervals = [], timeouts = [], historyCalls = [];
const documentListeners = {};
let visible = true;
const query = input.query !== undefined ? input.query : '?token=fixture-token';
const href = 'http://127.0.0.1:8731/' + query;

function abortError() { const error = new Error('aborted'); error.name = 'AbortError'; return error; }
const context = vm.createContext({
  document: {
    getElementById: id => ids[id], createElement: tag => new Element(tag),
    addEventListener: (type, listener) => { documentListeners[type] = listener; },
    get visibilityState() { return visible ? 'visible' : 'hidden'; },
  },
  location: { search: query, href, pathname: '/', hash: '' },
  history: { replaceState: function () { historyCalls.push(Array.from(arguments)); } },
  URL, URLSearchParams, AbortController, Date, Promise, Error, JSON, String,
  Option: function (text, value) {
    const item = new Element('option'); item.textContent = text; item.value = value; return item;
  },
  fetch: (path, options) => new Promise((resolve, reject) => {
    const record = { path, options: options || {}, aborted: false, settled: false };
    record.resolve = value => { if (!record.settled) { record.settled = true; resolve(value); } };
    record.reject = error => { if (!record.settled) { record.settled = true; reject(error); } };
    const signal = record.options.signal;
    if (signal) {
      if (signal.aborted) { record.aborted = true; record.reject(abortError()); }
      else signal.addEventListener('abort', () => {
        record.aborted = true; record.reject(abortError());
      });
    }
    requests.push(record);
  }),
  setInterval: (callback, delay) => { intervals.push({ callback, delay, cleared: false }); return intervals.length; },
  clearInterval: handle => { if (intervals[handle - 1]) intervals[handle - 1].cleared = true; },
  setTimeout: (callback, delay) => { const t = { callback, delay, cleared: false }; timeouts.push(t); return t; },
  clearTimeout: t => { if (t) t.cleared = true; },
  console: { log: () => {}, error: () => {} },
});
vm.runInContext(input.script, context, { timeout: 1000 });
const evaluate = script => vm.runInContext(script, context, { timeout: 1000 });
const tick = () => new Promise(resolve => setImmediate(resolve));
async function settle() { await tick(); await tick(); await tick(); }
async function respond(index, value, status) {
  const code = status || 200;
  requests[index].resolve({ ok: code >= 200 && code < 300, status: code, json: async () => value });
  await settle();
}
async function fail(index, message) { requests[index].reject(new Error(message)); await settle(); }

function resources() {
  return { usage: { total_tokens: null, token_coverage: 'unavailable', calls: null },
    billing: { currency_totals: [], financial_coverage: 'unavailable',
      unknown_currency_observations: 0, missing_amount_observations: 0,
      provider_requests: { observed_total: null },
      assistant_usage_messages: { observed_total: null } } };
}
function view(marker, scheduling) {
  const task = { task_id: 'tc-' + 'a'.repeat(32), source_id: 'src-' + 'a'.repeat(24),
    project: 'owned-project', kind: 'agent', title: marker, status: 'running',
    phase: 'execution', local_task_id: 'owned-task', analysis: { flags: [] },
    flow: { last_observed_stage: null, next_stage: null, next_status: 'untracked' },
    time: { elapsed_wall_seconds: null, remaining_seconds: null },
    forecast: { duration_range_seconds: null }, llm: [], goal_links: [],
    delivery: { present_files: null, declared_files: null }, ...resources() };
  if (scheduling !== undefined) task.scheduling = scheduling;
  return { schema_version: 1,
    sources: [{ source_id: task.source_id, name: 'owned-project', coverage: [] }],
    tasks: [task], goals: [], intents: [], repairs: [], resources: [], feedback: [],
    activity: { processes: [], diagnostics: [] }, diagnostics: [],
    observed_task_count: 1, generated_at: marker };
}
function snapshot() {
  return { connection: ids['connection'].textContent, tasks: ids['tasks'].textContent,
    detail: ids['detail'].textContent, summary: ids['summary'].textContent,
    error: ids['error'].textContent, form_hidden: ids['token-form'].hidden,
    input_value: ids['token-input'].value,
    requests: requests.map(r => ({ path: r.path, aborted: r.aborted,
      authorization: (r.options.headers || {}).Authorization || '',
      method: r.options.method || 'GET' })),
    all: Object.values(ids).map(node => node.textContent).join('\n') };
}

async function main() {
  const mode = input.mode;
  if (mode === 'legacy') {
    assert.equal(requests.length, 1);
    assert.equal(intervals[0].delay, 5000);
    assert.equal(requests[0].options.headers.Authorization, 'Bearer legacy-token');
    assert(!requests[0].path.includes('token'));
    assert(historyCalls.length >= 1 && !String(historyCalls[0][2]).includes('token'));
    await respond(0, view('LIVE-A'));
    assert(ids.tasks.textContent.includes('LIVE-A'));
    assert(ids['connection'].textContent.includes('已连接'));
    assert(ids['connection'].textContent.includes('最近成功刷新'));
    console.log(JSON.stringify(snapshot())); return;
  }
  if (mode === 'denial') {
    await respond(0, view('LIVE-A'));
    intervals[0].callback(); await settle();
    assert.equal(requests.length, 2);
    evaluate('refresh(true)'); await settle();
    assert.equal(requests.length, 3);
    assert.equal(requests[1].aborted, true);
    await respond(2, { error: 'unauthorized' }, 401);
    const denied = snapshot();
    assert.equal(ids.tasks.children.length, 0);
    assert(!denied.all.includes('LIVE-A'));
    assert(ids['connection'].textContent.includes('认证'));
    assert.equal(ids['token-form'].hidden, false);
    assert.equal(ids['token-input'].value, '');
    intervals[0].callback(); await settle();
    assert.equal(requests.length, 3, 'denied polling must stop until reconnect');
    requests[1].resolve({ ok: true, status: 200, json: async () => view('LATE-OLD') });
    await settle();
    assert(!snapshot().all.includes('LATE-OLD'));
    ids['token-input'].value = 'fresh-token';
    ids['token-form'].emit('submit', { preventDefault: function () {} });
    await settle();
    assert.equal(requests.length, 4);
    assert.equal(requests[3].options.headers.Authorization, 'Bearer fresh-token');
    assert(!requests[3].path.includes('token'));
    await respond(3, view('FRESH-B'));
    assert(ids.tasks.textContent.includes('FRESH-B'));
    assert(ids['connection'].textContent.includes('已连接'));
    console.log(JSON.stringify({ denied, final: snapshot() })); return;
  }
  if (mode === 'timeout') {
    assert.equal(requests.length, 1);
    assert.equal(timeouts.length, 1);
    timeouts[0].callback(); await settle();
    assert.equal(requests[0].aborted, true);
    assert.equal(ids.tasks.children.length, 0);
    assert(ids['connection'].textContent.includes('超时'));
    assert(ids['detail'].textContent.includes('不能确认任务状态'));
    assert.equal(ids['summary'].textContent, '数据不可用');
    const timedOut = snapshot();
    intervals[0].callback(); await settle();
    assert.equal(requests.length, 2);
    await respond(1, view('RECOVERED'));
    assert(ids.tasks.textContent.includes('RECOVERED'));
    console.log(JSON.stringify({ timed_out: timedOut, final: snapshot() })); return;
  }
  if (mode === 'network') {
    await respond(0, view('LIVE-A'));
    intervals[0].callback(); await settle();
    await fail(1, 'offline');
    assert.equal(ids.tasks.children.length, 0);
    assert(ids['connection'].textContent.includes('连接中断'));
    assert(ids['connection'].textContent.includes('最近成功刷新'));
    assert(ids['error'].textContent.includes('刷新失败: offline'));
    assert.equal(ids['summary'].textContent, '数据不可用');
    const stale = snapshot();
    intervals[0].callback(); await settle();
    assert.equal(requests.length, 3);
    await respond(2, view('BACK-ONLINE'));
    assert(ids.tasks.textContent.includes('BACK-ONLINE'));
    assert(ids['connection'].textContent.includes('已连接'));
    console.log(JSON.stringify({ stale: stale, final: snapshot() })); return;
  }
  if (mode === 'foreground') {
    await respond(0, view('LIVE-A'));
    documentListeners.visibilitychange();
    await settle();
    assert.equal(requests.length, 2);
    await respond(1, view('FOREGROUND-B'));
    visible = false; documentListeners.visibilitychange(); await settle();
    assert.equal(requests.length, 2, 'hidden documents must not refresh');
    console.log(JSON.stringify(snapshot())); return;
  }
  if (mode === 'no_token') {
    assert.equal(requests.length, 1, 'one bounded unauthenticated probe');
    assert.equal(intervals[0].delay, 5000);
    assert.equal(requests[0].options.headers.Authorization || '', '');
    assert.equal(ids['token-form'].hidden, false);
    assert(ids['connection'].textContent.includes('未连接'));
    await respond(0, { error: 'unauthorized' }, 401);
    assert.equal(ids['token-form'].hidden, false);
    assert(ids['connection'].textContent.includes('认证'));
    intervals[0].callback(); await settle();
    assert.equal(requests.length, 1, 'denied probe must stop polling');
    ids['token-input'].value = 'typed-token';
    ids['token-form'].emit('submit', { preventDefault: function () {} });
    await settle();
    assert.equal(requests.length, 2);
    assert.equal(requests[1].options.headers.Authorization, 'Bearer typed-token');
    assert(!requests[1].path.includes('token'));
    assert.equal(ids['token-input'].value, '');
    assert(historyCalls.length === 0);
    await respond(1, view('NEW-SESSION'));
    assert.equal(ids['token-form'].hidden, true);
    assert(ids['connection'].textContent.includes('已连接'));
    console.log(JSON.stringify(snapshot())); return;
  }
  if (mode === 'scheduling') {
    await respond(0, view('SCHED-A', { queue_wait_seconds: 3.5, execution_wall_seconds: 12, execution_status: 'PASSED' }));
    assert(ids.tasks.textContent.includes('排队 3.5 秒'));
    assert(ids.tasks.textContent.includes('执行 12 秒'));
    const first = snapshot();
    intervals[0].callback(); await settle();
    await respond(1, view('SCHED-B'));
    assert(ids.tasks.textContent.includes('排队 未追踪'));
    assert(!ids.tasks.textContent.includes('3.5'));
    console.log(JSON.stringify({ first: first, final: snapshot() })); return;
  }
  throw new Error('unknown fixture mode: ' + mode);
}
main().catch(error => { console.error(error.stack); process.exitCode = 1; });
