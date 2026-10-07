const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../pages/技能管理/index.html'), 'utf8');
const script = html.match(/<script>\s*([\s\S]*?)<\/script>/)[1];
const fixtures = JSON.parse(fs.readFileSync(0, 'utf8'));
const elements = new Map();
class Element {
  constructor(tag = 'div') { this.tag = tag; this.dataset = {}; this.children = []; this.value = ''; this.checked = false; this.hidden = false; this.disabled = false; this.textContent = ''; }
  set id(value) { this._id = value; elements.set(value, this); }
  get id() { return this._id; }
  set innerHTML(value) { for (const match of value.matchAll(/<(\w+)[^>]*id="([^"]+)"/g)) { const e = new Element(match[1]); e.id = match[2]; this.append(e); } }
  append(...items) { this.children.push(...items); if (this.tag === 'select' && this.children.length) this.value ||= String(this.children[0].value); }
  prepend(item) { this.children.unshift(item); }
  insertBefore(item, reference) { const index = this.children.indexOf(reference); if(index < 0) this.append(item); else this.children.splice(index, 0, item); }
  replaceChildren(...items) { this.children = []; this.value = ''; this.append(...items); }
  setAttribute(key, value) { this[key] = value; }
  focus() { this.focused = true; }
  get childElementCount() { return this.children.length; }
}
for (const match of html.matchAll(/<(\w+)[^>]*id="([^"]+)"/g)) { const e = new Element(match[1]); e.id = match[2]; }
const selectors = new Map();
const document = {
  getElementById: id => elements.get(id),
  createElement: tag => new Element(tag),
  querySelector: selector => selector.startsWith('#') ? elements.get(selector.slice(1)) : (selectors.get(selector) || (selectors.set(selector, new Element()), selectors.get(selector))),
  querySelectorAll: () => [...elements.values()].filter(e => ['button', 'input', 'select', 'textarea'].includes(e.tag)),
};
async function run(envelope, fallback = false) {
  elements.clear(); selectors.clear();
  for (const match of html.matchAll(/<(\w+)[^>]*id="([^"]+)"/g)) { const e = new Element(match[1]); e.id = match[2]; }
  const calls = [];
  let next = fixtures.list;
  const wrap = data => envelope ? {status: 'ok', data} : data;
  const bridge = {ready: async () => {}, apiGet: async endpoint => { assert.equal(endpoint, 'api'); calls.push({method: 'GET'}); return wrap(next); }, apiPost: async (endpoint, payload) => { assert.equal(endpoint, 'api'); calls.push({method: 'POST', ...payload}); return wrap(next); }};
  const context = vm.createContext({document, window: {AstrBotPluginPage: fallback ? null : bridge}, console,
    prompt: () => {throw Error('sandbox forbids prompt');}, confirm: () => {throw Error('sandbox forbids confirm');},
    fetch: async (endpoint, options) => { assert.equal(endpoint, '/api/plug/task-skills/api'); assert.equal(options.credentials, 'same-origin'); const payload = options.body ? JSON.parse(options.body) : null; if(payload) assert.equal(options.headers['X-Task-CSRF'], fixtures.list.csrf); calls.push({method: payload ? 'POST' : 'GET', csrf: options.headers?.['X-Task-CSRF'], ...payload}); return {ok: !next.error, json: async () => wrap(next)}; }});
  vm.runInContext(script, context);
  const flush = async () => { for(let i = 0; i < 8; i++) await Promise.resolve(); };
  await flush();
  const $ = id => elements.get(id);
  const click = async id => { assert.equal($(id).disabled, false, id + ' disabled'); await $(id).onclick(); await flush(); };
  assert.match($('count').textContent, /1 个技能/);
  assert.equal($('approve').disabled, true);
  assert.equal($('settingsPanel').hidden, true);
  await click('settingsTab'); assert.equal($('settingsPanel').hidden, false); assert.equal($('libraryPanel').hidden, true);
  assert.equal($('settingsTab')['aria-pressed'], 'true'); assert.equal($('sectionTitle').textContent, '插件设置');
  await click('reviewTab'); assert.equal($('settingsPanel').hidden, true); assert.equal($('libraryPanel').hidden, false);
  assert.equal($('reviewFilter').value, 'pending'); assert.equal($('sectionTitle').textContent, '审核工作台');
  await click('libraryTab'); assert.equal($('reviewFilter').value, 'all');
  await click('refresh'); assert.equal(calls.at(-1).method, 'GET');
  $('search').value = 'not-found'; $('search').oninput(); assert.equal($('list').children[0].tag, 'p');
  $('search').value = ''; $('search').oninput();
  $('reviewFilter').value = 'approved'; $('reviewFilter').onchange(); assert.equal($('list').children[0].tag, 'p');
  $('reviewFilter').value = 'all'; $('reviewFilter').onchange();
  $('list').children[0].onclick(); assert.match($('body').textContent, /操作步骤/);
  assert.equal($('verification').value, '');
  next = fixtures.approve; await click('approve'); assert.equal(calls.at(-1).action, 'approve'); assert.equal(calls.at(-1).revision, 1); assert.equal(calls.at(-1).csrf, fixtures.list.csrf, JSON.stringify({fallback,calls,status: $('status').textContent}));
  assert.equal(calls.at(-1).verification, '');
  next = fixtures.approve_publish; await click('approve_publish'); assert.equal(calls.at(-1).action, 'approve_publish'); assert.equal(calls.at(-1).verification, '');
  $('verification').value = '好'; next = fixtures.approve; await click('approve'); assert.equal(calls.at(-1).verification, '好');
  $('verification').value = 'short'; next = fixtures.approve_publish; await click('approve_publish'); assert.equal(calls.at(-1).verification, 'short');
  $('verification').value = 'password=private-review-secret'; next = {error: '批准失败：可选备注不能包含凭据。'}; await click('approve'); assert.match($('status').textContent, /可选备注/); assert.doesNotMatch($('status').textContent, /private-review-secret/);
  await click('publish'); const n = calls.length; await click('cancelAction'); assert.equal(calls.length, n);
  next = fixtures.publish; await click('publish'); await click('confirmAction'); assert.equal(calls.at(-1).action, 'publish');
  await click('edit'); assert.equal($('editor').hidden, false);
  $('editor').value = '{'; await click('save'); assert.match($('status').textContent, /JSON 无效/);
  $('editor').value = JSON.stringify(fixtures.edit.records[0].skill); next = fixtures.edit; await click('save'); assert.equal(calls.at(-1).action, 'edit'); assert.equal(calls.at(-1).revision, 1);
  next = fixtures.rollback; await click('rollback'); await click('confirmAction'); assert.equal(calls.at(-1).revision, 1); assert.equal(calls.at(-1).action, 'rollback');
  next = fixtures.reject; await click('reject'); assert.equal(calls.at(-1).action, 'reject'); assert.equal(calls.at(-1).revision, 3); assert.equal(calls.at(-1).verification, '');
  $('verification').value = 'Tested actual output against the requested result.';
  next = {error: '版本冲突：请刷新'}; await click('approve'); assert.match($('status').textContent, /版本冲突/);
  next = fixtures.config; await click('saveConfig'); assert.equal(calls.at(-1).action, 'config'); assert.equal(Object.keys(calls.at(-1).config).length, 7);
  next = fixtures.delete; await click('delete'); await click('confirmAction'); assert.equal(calls.at(-1).action, 'delete'); assert.equal($('tools').hidden, true);
  assert.match($('list').children[0].textContent, /暂无技能/); assert.equal($('filteredCount').textContent, '显示 0 / 0');
  const baseSkill = fixtures.list.records[0].skill;
  const legacy = {...baseSkill, name: 'legacy-task', description: '旧版未分类技能用于兼容测试'};
  for (const key of ['tags', 'conditions', 'tools', 'workflow']) delete legacy[key];
  next = {...fixtures.list, records: [
    {skill: {...baseSkill, tags: ['文档', '验证', '文档']}, revision: 7, review_status: 'pending'},
    {skill: {...baseSkill, name: 'approved-task', tags: ['文档']}, revision: 2, review_status: 'approved'},
    {skill: {...baseSkill, name: 'rejected-task', tags: ['验证']}, revision: 3, review_status: 'rejected'},
    {skill: legacy},
    {skill: {...baseSkill, name: 'empty-tags-task', tags: []}, review_status: 'pending'},
  ], stats: {skills: 5, revisions: 5}};
  await click('refresh');
  assert.equal($('overview').textContent, '全部 5 · 待审核 3 · 已批准 1 · 已拒绝 1 · 分类 2 · 未分类 2');
  assert.equal($('categoryFilter').children.find(e => e.value === 'tag:文档').textContent, '文档（2）');
  assert.equal($('filteredCount').textContent, '显示 5 / 5');
  $('list').children[0].onclick(); $('verification').value = '保留备注';
  $('categoryFilter').value = 'tag:文档'; $('categoryFilter').onchange();
  assert.equal($('filteredCount').textContent, '显示 2 / 5');
  $('reviewFilter').value = 'pending'; $('reviewFilter').onchange();
  $('search').value = ' CHECK-TASK '; $('search').oninput();
  assert.equal($('filteredCount').textContent, '显示 1 / 5');
  assert.match($('categoryDetail').textContent, /文档 \/ 验证/);
  $('search').value = 'not-found'; $('search').oninput();
  assert.equal($('filteredCount').textContent, '显示 0 / 5');
  assert.equal($('title').textContent, baseSkill.name); assert.equal($('verification').value, '保留备注');
  assert.equal($('approve').disabled, false);
  await click('clearFilters'); assert.match($('list').children[0].className, /selected/);
  $('categoryFilter').value = 'untagged'; $('categoryFilter').onchange();
  assert.equal($('filteredCount').textContent, '显示 2 / 5');
  $('search').value = 'legacy-task'; $('search').oninput(); assert.equal($('filteredCount').textContent, '显示 1 / 5');
  $('list').children[0].onclick(); assert.equal($('categoryDetail').textContent, '技能分类：未分类');
  await click('clearFilters'); $('list').children[0].onclick();
  await click('edit'); const changedSkill = {...baseSkill, tags: ['新分类', '验证']};
  $('editor').value = JSON.stringify(changedSkill);
  next = {...fixtures.list, records: [{skill: changedSkill, revision: 8, review_status: 'pending', history: [{revision: 7, skill: baseSkill}]}]};
  await click('save'); assert.equal(calls.at(-1).revision, 7); assert.equal(JSON.stringify(calls.at(-1).skill.tags), JSON.stringify(['新分类', '验证']));
  assert.equal($('publish').disabled, true); assert.equal($('verification').value, '');
  assert.match($('description').textContent, /当前版本 8 · 待审核/);
  assert.equal($('filteredCount').textContent, '显示 1 / 1');
  assert.match($('list').children[0].className, /selected/);
  $('categoryFilter').value = 'tag:新分类'; $('categoryFilter').onchange();
  await click('refresh'); assert.equal($('categoryFilter').value, 'tag:新分类');
  next = fixtures.delete; await click('refresh'); assert.equal($('categoryFilter').value, 'all');
  assert.equal($('tools').hidden, true); assert.equal($('categoryDetail').textContent, '');
  assert.throws(() => vm.runInContext("get_payload({status:'error',message:'路由不存在'})", context), /路由不存在/);
  assert.throws(() => vm.runInContext('get_payload({})', context), /响应格式无效/);
  new vm.Script(script);
}
(async () => { await run(false); await run(true); await run(false, true); console.log('frontend: all buttons, combined category filters, legacy/empty tags, counts, selection, page sections, GET/POST and sandbox contract passed'); })().catch(error => { console.error(error); process.exitCode = 1; });
