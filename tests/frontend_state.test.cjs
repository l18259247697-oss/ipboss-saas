const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../frontend/app.js'), 'utf8');

function element() {
  const classes = new Set(['hidden']);
  const attrs = {};
  const listeners = {};
  return {
    value: '', textContent: '', innerHTML: '', disabled: false, style: {}, children: [],
    classList: {add: c => classes.add(c), remove: c => classes.delete(c),
      contains: c => classes.has(c), toggle(c, force) {
        const on = force ?? !classes.has(c); on ? classes.add(c) : classes.delete(c); return on;
      }},
    setAttribute(k, v) { attrs[k] = String(v); },
    getAttribute(k) { return attrs[k] ?? null; },
    removeAttribute(k) { delete attrs[k]; },
    get src() { return attrs.src || ''; }, set src(v) { attrs.src = v; },
    get href() { return attrs.href || ''; }, set href(v) { attrs.href = v; },
    addEventListener(k, fn) { (listeners[k] ||= []).push(fn); },
    dispatch(k) { for (const fn of listeners[k] || []) fn({target: this}); },
    replaceChildren(...children) { this.children = children; },
    appendChild(child) { this.children.push(child); },
    pause() {}, load() {},
  };
}
const original = {id: 'finished', status: 'done', step: 6, progress: 100, message: 'complete',
  created_at: 1, params: {source_text: 'previous source', industry: 'previous industry',
    extra: 'previous request', use_clone: false, voice: 'voice-a', speed: 1.25},
  result: {transcript: 'source', rewritten: 'script', audio_url: '/voice.wav', video_url: '/final.mp4'}};
function workspace(storage = new Map(), username = 'alice', overrides = {}) {
  const elements = new Map();
  const get = id => { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); };
  const routes = {
    '/api/public': {brand: {}, voices: [{id: 'voice-a', name: 'A'}], avatars: [], credits_per_video: 10},
    '/api/me': {username, credits: 30, has_voice: true, voice_url: '/reference.wav',
      voice_ref_text: 'saved reference', voice_revision: 'revision-one'},
    '/api/jobs': [original], '/api/jobs/latest': original, '/api/jobs/finished': original,
    ...overrides,
  };
  const calls = [];
  const timers = [];
  const context = vm.createContext({console, document: {getElementById: get,
    querySelectorAll: () => [], createElement: () => element()},
    localStorage: {getItem: k => storage.get(k) ?? null,
      setItem: (k, v) => storage.set(k, String(v)), removeItem: k => storage.delete(k)},
    fetch: async (url, opts = {}) => { calls.push({url, method: opts.method || 'GET'});
      const body = routes[url]; if (body instanceof Error) throw body;
      return {ok: true, status: 200, json: async () => body}; },
    location: {reload() {}}, navigator: {}, alert() {},
    setTimeout: fn => {timers.push(fn); return timers.length;}, clearTimeout() {},
    window: {addEventListener() {}},
  });
  vm.runInContext(source, context);
  return {get, context, calls, routes, timers, run: code => vm.runInContext(code, context)};
}
const settle = () => new Promise(resolve => setImmediate(resolve));

test('saved task inputs restore even before any browser draft exists', async () => {
  const w = workspace(); await settle();
  assert.equal(w.get('sourceCopy').value, 'previous source');
  assert.equal(w.get('industry').value, 'previous industry');
  assert.equal(w.get('speed').value, '1.25');
  assert.equal(w.run('VOICEMODE'), 'preset');
});

test('draft and both source modes survive reload and same-account login, never another account', async () => {
  const storage = new Map(); const a = workspace(storage); await settle();
  a.get('sourceCopy').value = 'unfinished draft'; a.get('link').value = 'https://v.douyin.com/draft/';
  a.get('industry').value = 'draft industry'; a.get('extra').value = 'draft request';
  a.run("setSourceMode('text'); setVoiceMode('preset')");
  a.get('extra').dispatch('input');
  const restored = workspace(storage); await settle();
  assert.equal(restored.get('sourceCopy').value, 'unfinished draft');
  assert.equal(restored.get('link').value, 'https://v.douyin.com/draft/');
  assert.equal(restored.get('extra').value, 'draft request');
  assert.equal(restored.run('SOURCEMODE'), 'text');
  const other = workspace(storage, 'bob', {'/api/jobs': [], '/api/jobs/latest': null}); await settle();
  assert.equal(other.get('sourceCopy').value, '');
  assert.equal(other.get('industry').value, '');
});

test('reference draft restores only for the same recording revision', async () => {
  const storage = new Map(); const a = workspace(storage); await settle();
  a.get('voiceRefText').value = 'unfinished reference'; a.get('voiceRefText').dispatch('input');
  const same = workspace(storage); await settle();
  assert.equal(same.get('voiceRefText').value, 'unfinished reference');
  const changed = workspace(storage, 'alice', {'/api/me': {username: 'alice', has_voice: true,
    voice_url: '/new.wav', voice_ref_text: 'new reference', voice_revision: 'revision-two'}});
  await settle(); assert.equal(changed.get('voiceRefText').value, 'new reference');
});

test('failed or empty task cannot display another task media', async () => {
  const w = workspace(); await settle();
  w.run('renderResult({})');
  assert.equal(w.get('vVideo').getAttribute('src'), null);
  assert.equal(w.get('dlVideo').getAttribute('href'), null);
  assert.equal(w.get('vTranscript').textContent, '');
  assert.equal(w.get('resultArea').classList.contains('hidden'), true);
});

test('generation completion does not imply effect approval', async () => {
  const w = workspace(); await settle();
  w.run("renderResult({video_url:'/pending.mp4', media_checks_passed:true, quality_review:'pending'})");
  assert.equal(w.get('qualityStatus').classList.contains('hidden'), false);
  assert.match(w.get('qualityStatus').textContent, /效果待验收/);
  w.run("renderResult({video_url:'/legacy.mp4', media_checks_passed:true})");
  assert.match(w.get('qualityStatus').textContent, /未记录效果验收结果/);
});

test('final video exposes loading and failure state instead of silent black player', async () => {
  const w = workspace(); await settle();
  w.run("renderResult({video_url:'/new.mp4'})");
  assert.equal(w.get('vVideoStatus').textContent, '视频加载中…');
  w.get('vVideo').dispatch('canplay');
  assert.equal(w.get('vVideoStatus').classList.contains('hidden'), true);
  w.get('vVideo').dispatch('waiting');
  assert.equal(w.get('vVideoStatus').textContent, '视频缓冲中…');
  w.get('vVideo').dispatch('error');
  assert.match(w.get('vVideoStatus').textContent, /加载失败/);
  assert.equal(w.get('vVideoStatus').classList.contains('error'), true);
});

test('quality warning follows selected task across reload, failed task and placeholder', async () => {
  const revision = {...original, result: {...original.result, quality_review: 'needs_revision'}};
  const overrides = {'/api/jobs': [revision], '/api/jobs/finished': revision};
  const storage = new Map();
  const first = workspace(storage, 'alice', overrides); await settle();
  assert.match(first.get('qualityStatus').textContent, /效果需修改/);
  const reloaded = workspace(storage, 'alice', overrides); await settle();
  assert.match(reloaded.get('qualityStatus').textContent, /效果需修改/);
  reloaded.run("renderResult({video_url:'/new.mp4', quality_review:'pending'})");
  assert.match(reloaded.get('qualityStatus').textContent, /效果待验收/);
  for (const result of [{}, {video_url:'/placeholder.mp4', placeholder:true}]) {
    reloaded.run(`renderResult(${JSON.stringify(result)})`);
    assert.equal(reloaded.get('qualityStatus').textContent, '');
    assert.equal(reloaded.get('qualityStatus').classList.contains('hidden'), true);
  }
});

test('history distinguishes effect review from pipeline completion', async () => {
  const w = workspace(); await settle();
  w.run(`jobHistory = [
    {id:'pending', status:'done', created_at:3, industry:'shop', quality_review:'pending'},
    {id:'revision', status:'done', created_at:2, industry:'shop', quality_review:'needs_revision'},
    {id:'failed', status:'failed', created_at:1, industry:'shop'}
  ]; activeJobId = 'pending'; renderHistory()`);
  const labels = w.get('jobHistory').children.map(option => option.textContent);
  assert.match(labels[0], /待验收/);
  assert.match(labels[1], /需修改/);
  assert.match(labels[2], /失败/);
});

test('history failure keeps authenticated workspace and draft visible', async () => {
  const w = workspace(new Map(), 'alice', {'/api/jobs': new Error('offline'), '/api/jobs/latest': new Error('offline')});
  await settle(); assert.equal(w.get('appMain').classList.contains('hidden'), false);
  assert.equal(w.get('authMask').classList.contains('hidden'), true);
  assert.match(w.get('workspaceNotice').textContent, /offline/);
  assert.equal(w.get('goBtn').disabled, true);
});

test('late task response cannot overwrite a newer task selection', async () => {
  const w = workspace(); await settle();
  let release;
  w.context.fetch = async url => ({ok: true, json: async () => url.endsWith('/old')
    ? new Promise(resolve => {release = resolve;}) : {...original, id: 'new', result: {rewritten: 'new result'}}});
  const pending = w.run("selectJob('old')"); await settle();
  await w.run("selectJob('new')");
  release({...original, id: 'old'}); await pending;
  assert.equal(w.get('vRewritten').textContent, 'new result');
});

test('older selected result survives reload even when the newest job failed', async () => {
  const storage = new Map();
  const failed = {id: 'failed', status: 'failed', created_at: 2, result: {}, params: {}};
  const routes = {'/api/jobs': [failed, original], '/api/jobs/failed': failed};
  const w = workspace(storage, 'alice', routes); await settle();
  assert.equal(w.get('vVideo').getAttribute('src'), null);
  await w.run("selectJob('finished')");
  const restored = workspace(storage, 'alice', routes); await settle();
  assert.equal(restored.get('vVideo').getAttribute('src'), '/final.mp4');
  assert.equal(restored.get('jobHistory').value, 'finished');
});

test('rejected generation keeps the prior result and does not create or poll a new job', async () => {
  const w = workspace(); await settle();
  w.run('confirmGeneration = async () => true');
  w.context.fetch = async () => ({ok: false, status: 402, json: async () => ({detail: 'insufficient credits'})});
  await w.run('startJob()');
  assert.equal(w.get('vVideo').getAttribute('src'), '/final.mp4');
  assert.equal(w.get('dlVideo').getAttribute('href'), '/final.mp4');
  assert.match(w.get('jobError').textContent, /insufficient credits/);
  assert.equal(w.get('goBtn').disabled, false);
});

test('unknown submission preserves result and stops blind repeat submissions', async () => {
  const w = workspace(); await settle();
  w.run('confirmGeneration = async () => true');
  w.context.fetch = async () => {throw new Error('connection lost');};
  await w.run('startJob()');
  assert.equal(w.get('vVideo').getAttribute('src'), '/final.mp4');
  assert.equal(w.get('goBtn').disabled, true);
  assert.equal(w.get('restoreRetry').classList.contains('hidden'), false);
});

test('pending request key is reused for the same body and rotated after edits', async () => {
  const w = workspace(); await settle();
  const first = w.run('requestKeyFor({source_text: "same", industry: "shop"})');
  const replay = w.run('requestKeyFor({source_text: "same", industry: "shop"})');
  const changed = w.run('requestKeyFor({source_text: "changed", industry: "shop"})');
  assert.equal(replay, first);
  assert.notEqual(changed, first);
  w.run('clearPendingRequest(' + JSON.stringify(changed) + ')');
});

test('cancelled confirmation does not submit or erase results and draft', async () => {
  const w = workspace(); await settle();
  w.get('extra').value = 'unsent request'; w.get('extra').dispatch('input');
  w.run('confirmGeneration = async () => false');
  await w.run('startJob()');
  assert.equal(w.calls.filter(call => call.method === 'POST').length, 0);
  assert.equal(w.get('vVideo').getAttribute('src'), '/final.mp4');
  assert.equal(w.get('extra').value, 'unsent request');
});

test('running checkpoint restores without any generation POST', async () => {
  const running = {...original, status: 'running', step: 5, progress: 82,
    result: {transcript: 'source', rewritten: 'script', audio_url: '/voice.wav'}};
  const w = workspace(new Map(), 'alice', {'/api/jobs': [running], '/api/jobs/finished': running});
  await settle();
  assert.equal(w.get('goBtn').disabled, true);
  assert.equal(w.get('vAudio').getAttribute('src'), '/voice.wav');
  assert.equal(w.get('vVideo').getAttribute('src'), null);
  assert.equal(w.calls.filter(call => call.method === 'POST').length, 0);
});

test('closing an unchanged older tab does not overwrite a newer draft from another tab', async () => {
  const storage = new Map();
  const older = workspace(storage); await settle();
  const newer = workspace(storage); await settle();
  newer.get('extra').value = 'latest edit in the other tab'; newer.get('extra').dispatch('input');
  older.run('logout()');
  const restored = workspace(storage); await settle();
  assert.equal(restored.get('extra').value, 'latest edit in the other tab');
});

test('successful polling clears stale connection error while preserving the checkpoint', async () => {
  const w = workspace(); await settle();
  w.routes['/api/jobs/finished'] = new Error('offline');
  w.run("poll('finished')"); await settle();
  assert.match(w.get('jobError').textContent, /offline/);
  w.routes['/api/jobs/finished'] = {...original, status: 'waiting', can_resume: true};
  await w.timers.at(-1)();
  assert.equal(w.get('jobError').textContent, '');
  assert.equal(w.get('vVideo').getAttribute('src'), '/final.mp4');
});
