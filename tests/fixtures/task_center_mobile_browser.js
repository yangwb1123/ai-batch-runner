'use strict';
// Executes the desktop then the mobile inline scripts in one bounded DOM VM.
// No real network, no storage, no model calls: responses are declared in-memory.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));

class Element {
  constructor(tag) {
    this.tagName = tag; this.className = ''; this.children = []; this.listeners = {};
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
  'error', 'refresh', 'connection', 'token-form', 'token-input', 'connect',
  'mobile-tasks-view', 'mobile-tasks-heading', 'mobile-note', 'mobile-warnings',
  'mobile-tasks'];
const ids = {};
for (const id of IDS) ids[id] = new Element(id === 'source' ? 'select' : 'section');
ids['intent-detail'].hidden = true;
ids['token-form'].hidden = false;

const requests = [], intervals = [], timeouts = [], historyCalls = [];
const query = '?token=fixture-token';
const documentListeners = {};
let visible = true;

function abortError() { const error = new Error('aborted'); error.name = 'AbortError'; return error; }
const context = vm.createContext({
  document: {
    getElementById: id => ids[id] || null,
    createElement: tag => new Element(tag),
    addEventListener: (type, listener) => { documentListeners[type] = listener; },
    get visibilityState() { return visible ? 'visible' : 'hidden'; },
  },
  location: { search: query, href: 'http://127.0.0.1:8731/' + query, pathname: '/', hash: '' },
  history: { replaceState: function () { historyCalls.push(Array.from(arguments)); } },
  URL, URLSearchParams, AbortController, Date, Promise, Error, JSON, String, Number,
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
context.window = context;
vm.runInContext(input.scripts[0], context, { timeout: 1000 });
vm.runInContext(input.scripts[1], context, { timeout: 1000 });
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
function task(marker, options) {
  const opts = options || {};
  const row = { task_id: opts.task_id || 'tc-' + 'a'.repeat(32),
    source_id: 'src-' + 'a'.repeat(24), project: 'owned-project', kind: 'direction',
    title: marker, status: opts.status || 'RUNNING', phase: opts.phase || 'execution',
    local_task_id: 'owned-task', analysis: { flags: opts.flags || [] },
    flow: { last_observed_stage: opts.stage === undefined ? 'implement' : opts.stage,
      next_stage: null, next_status: 'untracked' },
    time: { elapsed_wall_seconds: opts.elapsed === undefined ? 12.5 : opts.elapsed,
      remaining_seconds: null },
    forecast: { duration_range_seconds: opts.range === undefined ? null : opts.range },
    llm: opts.llm === undefined ? [{ agent: 'codex', provider: 'openai-codex',
      model: 'gpt-6.1-sol', thinking: 'max', origin: 'recorded_configuration' }] : opts.llm,
    goal_links: [], delivery: { present_files: null, declared_files: null,
      acceptance: 'untracked', merge_status: opts.merge || '' },
    ...resources() };
  if (opts.scheduling !== false) row.scheduling = opts.scheduling || {
    queue_wait_seconds: 3.5, execution_wall_seconds: 12, execution_status: 'PASSED' };
  return row;
}
function view(marker, tasks) {
  return { schema_version: 1,
    sources: [{ source_id: 'src-' + 'a'.repeat(24), name: 'owned-project', coverage: [] }],
    tasks: tasks || [task(marker)], goals: [], intents: [], repairs: [], resources: [],
    feedback: [], activity: { processes: [], diagnostics: [] }, diagnostics: [],
    observed_task_count: (tasks || [task(marker)]).length, generated_at: marker };
}
function output() {
  return { desktop: ids.tasks.textContent, desktop_rows: ids.tasks.children.length,
    mobile: ids['mobile-tasks'].textContent, mobile_count: ids['mobile-tasks'].children.length,
    mobile_warnings: ids['mobile-warnings'].textContent,
    detail: ids.detail.textContent, summary: ids.summary.textContent,
    connection: ids['connection'].textContent, error: ids.error.textContent,
    form_hidden: ids['token-form'].hidden, warning: ids['mobile-warnings'].textContent,
    requests: requests.map(r => ({ path: r.path, aborted: r.aborted,
      authorization: (r.options.headers || {}).Authorization || '',
      method: r.options.method || 'GET' })),
    tags: Object.values(ids).flatMap(node => node.descendants('script').concat(node.descendants('img'))).length,
    all: Object.values(ids).map(node => node.textContent).join('\n') };
}
function mobileButton() {
  const buttons = ids['mobile-tasks'].descendants('button');
  assert(buttons.length > 0, 'mobile card must expose a detail button');
  return buttons[0];
}

async function main() {
  const mode = input.mode;
  assert.equal(requests.length, 1);
  assert.equal(intervals[0].delay, 5000);
  if (mode === 'render') {
    await respond(0, input.views[0]);
    assert(ids.tasks.textContent.includes(input.views[0].tasks[0].title));
    assert(ids['mobile-tasks'].children.length === input.views[0].tasks.length);
    console.log(JSON.stringify(output())); return;
  }
  if (mode === 'detail') {
    await respond(0, input.views[0]);
    mobileButton().emit('click');
    const selected = JSON.parse(ids.detail.textContent);
    assert.equal(selected.task_id, input.views[0].tasks[0].task_id);
    assert.equal(requests.length, 1);
    console.log(JSON.stringify(output())); return;
  }
  if (mode === 'pending') {
    await respond(0, input.views[0]);
    assert(ids['mobile-tasks'].children.length > 0);
    evaluate('refresh(true)');
    const pending = output();
    assert.equal(pending.mobile_count, 0);
    assert.equal(pending.desktop_rows, 0);
    assert(!pending.all.includes(input.old_marker));
    await respond(1, input.views[1]);
    assert(!output().all.includes(input.old_marker));
    assert(output().mobile.includes(input.new_marker));
    console.log(JSON.stringify({ pending, final: output() })); return;
  }
  if (mode === 'source_switch' || mode === 'late_reply') {
    await respond(0, input.views[0]);
    evaluate('refresh(true)');
    assert.equal(requests.length, 2);
    if (mode === 'source_switch') {
      ids.source.value = input.source;
      ids.source.emit('change');
    } else {
      evaluate('refresh(true)');
    }
    assert.equal(requests.length, 3);
    assert.equal(requests[1].aborted, true);
    const cleared = output();
    assert.equal(cleared.mobile_count, 0);
    assert.equal(cleared.desktop_rows, 0);
    assert(!cleared.all.includes(input.old_marker));
    await respond(2, input.views[1]);
    requests[1].resolve({ ok: true, status: 200, json: async () => input.views[0] });
    await settle();
    const final = output();
    assert(!final.all.includes(input.old_marker));
    assert(final.mobile.includes(input.new_marker));
    console.log(JSON.stringify({ cleared, final })); return;
  }
  if (mode === 'denial') {
    await respond(0, input.views[0]);
    intervals[0].callback(); await settle();
    assert.equal(requests.length, 2);
    evaluate('refresh(true)'); await settle();
    assert.equal(requests.length, 3);
    assert.equal(requests[1].aborted, true);
    await respond(2, { error: 'unauthorized' }, 401);
    const denied = output();
    assert.equal(denied.mobile_count, 0);
    assert.equal(denied.desktop_rows, 0);
    assert(!denied.all.includes(input.old_marker));
    assert.equal(denied.form_hidden, false);
    intervals[0].callback(); await settle();
    assert.equal(requests.length, 3, 'denied polling must stop');
    requests[1].resolve({ ok: true, status: 200, json: async () => input.views[0] });
    await settle();
    assert(!output().all.includes(input.old_marker));
    ids['token-input'].value = 'fresh-token';
    ids['token-form'].emit('submit', { preventDefault: function () {} });
    await settle();
    await respond(3, input.views[1]);
    assert(output().mobile.includes(input.new_marker));
    console.log(JSON.stringify({ denied, final: output() })); return;
  }
  if (mode === 'timeout') {
    await respond(0, input.views[0]);
    evaluate('refresh(true)');
    assert.equal(requests.length, 2);
    const timer = timeouts[timeouts.length - 1];
    assert(timer && !timer.cleared);
    timer.callback(); await settle();
    const timedOut = output();
    assert.equal(timedOut.mobile_count, 0);
    assert.equal(timedOut.desktop_rows, 0);
    assert(!timedOut.all.includes(input.old_marker));
    requests[1].resolve({ ok: true, status: 200, json: async () => input.views[0] });
    await settle();
    assert(!output().all.includes(input.old_marker));
    console.log(JSON.stringify({ timed_out: timedOut, final: output() })); return;
  }
  throw new Error('unknown fixture mode: ' + mode);
}
main().catch(error => { console.error(error.stack); process.exitCode = 1; });
