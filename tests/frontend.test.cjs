'use strict';

// These tests execute the complete application with controlled DOM/network seams.
// They verify state, asynchronous ordering and registered edit handlers. They do
// not verify browser layout, native validation or browser-specific file decoding.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

const root = path.resolve(__dirname, '..');
const application = fs.readFileSync(path.join(root, 'web/app.js'), 'utf8');
const documentHTML = fs.readFileSync(path.join(root, 'web/index.html'), 'utf8');

class FixtureElement {
  constructor(document, id = '') {
    this.document = document;
    this.id = id;
    this.value = '';
    this.textContent = '';
    this.hidden = false;
    this.disabled = false;
    this.open = false;
    this.isConnected = true;
    this.dataset = {};
    this.files = [];
    this.options = [];
    this.listeners = new Map();
    this.ownedIDs = new Set();
    this.children = new Map();
    this.classes = new Set();
    this.classList = {
      toggle: (name, enabled) => {
        if (enabled) this.classes.add(name);
        else this.classes.delete(name);
      },
    };
  }

  set innerHTML(html) {
    this.html = html;
    for (const id of this.ownedIDs) {
      const element = this.document.elements.get(id);
      if (element?.owner === this) {
        element.isConnected = false;
        this.document.elements.delete(id);
      }
    }
    this.ownedIDs.clear();
    // Materialize only ID-addressed controls. HTML/layout parsing is deliberately
    // outside this fixture's scope, and app source is never rewritten/extracted.
    for (const match of html.matchAll(/\bid="([^"]+)"/g)) {
      const element = new FixtureElement(this.document, match[1]);
      element.owner = this;
      this.document.elements.set(element.id, element);
      this.ownedIDs.add(element.id);
    }
  }

  get innerHTML() { return this.html || ''; }
  querySelector(selector) { return this.children.get(selector) || this.document.querySelector(selector); }
  querySelectorAll() { return []; }
  closest() { return this.parent || null; }
  contains(element) {
    while (element) {
      if (element === this) return true;
      element = element.parent || element.owner;
    }
    return false;
  }
  setAttribute(name, value) { this[name] = value; }
  removeAttribute(name) { delete this[name]; }
  setCustomValidity(message) { this.validationMessage = message; }
  focus() { this.document.activeElement = this; }
  scrollIntoView() {}
  showModal() {}
  add(option) { this.options.push(option); }
  replaceChildren(...children) { this.innerHTML = ''; this.options = children; }

  addEventListener(name, callback) {
    if (!this.listeners.has(name)) this.listeners.set(name, new Set());
    this.listeners.get(name).add(callback);
  }

  removeEventListener(name, callback) { this.listeners.get(name)?.delete(callback); }

  async emit(name) {
    const event = { type: name, target: this, currentTarget: this, preventDefault() {} };
    await Promise.all([...this.listeners.get(name) || []].map(callback => callback(event)));
  }

  dispatchEvent(event) { return this.emit(event.type); }
}

function fixture({ stored = {}, fetch: customFetch, view = 'check' } = {}) {
  const storage = new Map(Object.entries(stored));
  const delays = [];
  const intervals = [];
  const downloads = [];
  const blobs = new Map();
  const windowListeners = new Map();
  const document = {
    elements: new Map(), body: { classList: { toggle() {} }, append() {} },
    querySelector(selector) { return selector.startsWith('#') ? this.elements.get(selector.slice(1)) || null : null; },
    querySelectorAll() { return []; },
    getElementById(id) { return this.elements.get(id) || null; },
    createElement(tag) {
      assert.equal(tag, 'a', 'The fixture only supports download anchors');
      const anchor = new FixtureElement(this);
      anchor.click = () => downloads.push({ filename: anchor.download, blob: blobs.get(anchor.href) });
      anchor.remove = () => { anchor.isConnected = false; };
      return anchor;
    },
  };
  const initial = new FixtureElement(document);
  initial.innerHTML = documentHTML;
  const location = { hash: `#${view}` };
  const context = vm.createContext({
    document, location, console, TextDecoder, Uint8Array, Event, Blob, crypto: webcrypto,
    URL: class extends URL {
      static createObjectURL(blob) { const url = `blob:fixture-${blobs.size}`; blobs.set(url, blob); return url; }
      static revokeObjectURL(url) { blobs.delete(url); }
    },
    Option: class { constructor(text, value) { this.text = text; this.value = value; } },
    localStorage: {
      getItem: key => storage.get(key) ?? null,
      setItem: (key, value) => { storage.set(key, value); },
    },
    window: { addEventListener: (name, callback) => windowListeners.set(name, callback), matchMedia: () => ({ matches: false }) },
    setInterval(callback) { intervals.push(callback); },
    setTimeout(callback, milliseconds) { delays.push(milliseconds); queueMicrotask(callback); return 1; },
    clearTimeout() {},
    fetch: async (url, options = {}) => {
      const body = options.body ? JSON.parse(options.body) : undefined;
      if (customFetch) {
        const response = await customFetch(url, body);
        if (response) return response;
      }
      const defaults = {
        '/api/status': { model: { model: 'qwen3:4b', available: true, models: ['qwen3:4b'] }, counts: { sources: 0, reports: 0 } },
        '/api/sources': { sources: [] },
        '/api/source-packs': { packs: [] },
        '/api/examples': { examples: [] },
        '/api/evaluations': { runs: [] },
      };
      assert.ok(Object.hasOwn(defaults, url), `Unexpected API request ${url}`);
      return json(defaults[url]);
    },
  });
  // Execute every statement, including startup and normal listener registration.
  vm.runInContext(application, context, { filename: 'web/app.js' });
  return {
    document, storage, delays, intervals, downloads,
    evaluate: expression => vm.runInContext(expression, context),
    expose: (name, value) => { context[name] = value; },
    element: id => document.getElementById(id),
    async navigate(view) { location.hash = `#${view}`; await windowListeners.get('hashchange')(); },
  };
}

function json(body, status = 200) { return { ok: status < 400, status, json: async () => body }; }
function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}
const settle = () => new Promise(resolve => setImmediate(resolve));

function textFile(contents, name = 'article.txt') {
  const bytes = Buffer.from(contents, 'utf8');
  return { name, size: bytes.length, arrayBuffer: async () => Uint8Array.from(bytes).buffer };
}

test('same candidate IDs in different retrievals cannot share verdicts or rewrites', async () => {
  const app = fixture();
  await settle();
  app.evaluate(`
    globalThis.candidate = {id:'candidate-1-1',rank:1,source_title:'Article',source_excerpt:'Source',query_excerpt:'Query'};
    globalThis.first = {id:'retrieval-a',report_id:'report-a'};
    globalThis.second = {id:'retrieval-b',report_id:'report-b'};
    state.report={id:'report-b',text:'Query'};
    state.candidateJudgments[candidateKey(first,candidate)]={id:'judgment-a',relation:'paraphrase',explanation:'FIRST VERDICT'};
    state.candidateRewrites[candidateKey(first,candidate)]={text:'FIRST REWRITE',original_text:'Query'};
  `);
  const first = app.evaluate('candidateHTML(candidate,first)');
  const second = app.evaluate('candidateHTML(candidate,second)');
  assert.ok(first.includes('FIRST VERDICT') && first.includes('FIRST REWRITE'));
  assert.ok(!second.includes('FIRST VERDICT') && !second.includes('FIRST REWRITE'));
});

test('a delayed saved snapshot cannot restore a positive after a newer failed recheck', async () => {
  const response = deferred();
  const app = fixture({ fetch: url => url === '/api/candidate-judgments/retrieval-a' ? response.promise : undefined });
  await settle();
  const loading = app.evaluate("loadCandidateJudgments('retrieval-a',true)");
  await settle();
  app.evaluate("mergeCandidateOutcomes('retrieval-a',[{candidate_id:'candidate-1-1',error:'NEW FAILED RECHECK'}])");
  response.resolve(json({ judgments: [{ candidate_id: 'candidate-1-1', judgment: { id: 'old', relation: 'paraphrase' } }] }));
  await loading;
  assert.equal(app.evaluate("state.candidateErrors['retrieval-a:candidate-1-1']"), 'NEW FAILED RECHECK');
  assert.equal(app.evaluate("state.candidateJudgments['retrieval-a:candidate-1-1']"), undefined);
});

test('registered Apply replaces only grounded Unicode range and preserves surrounding newlines', async () => {
  const app = fixture();
  await settle();
  const card = new FixtureElement(app.document);
  const field = new FixtureElement(app.document);
  const apply = new FixtureElement(app.document);
  const discard = new FixtureElement(app.document);
  card.children.set('.candidate-rewrite-text', field);
  card.children.set('[data-apply-candidate]', apply);
  card.children.set('[data-discard-candidate]', discard);
  field.value = 'Новая формулировка';
  app.expose('testCard', card);
  app.expose('testPrefix', '😀 Авторское начало.\n\n');
  app.expose('testSuffix', '\n\nСобственный вывод автора.');
  app.evaluate(`
    globalThis.prefix=testPrefix;
    globalThis.original='Заимствованный фрагмент.';
    globalThis.suffix=testSuffix;
    state.text=prefix+original+suffix;
    state.report={id:'report-a',text:state.text,matches:[],stats:{}};
    globalThis.retrieval={id:'retrieval-a',report_id:'report-a'};
    globalThis.candidate={id:'candidate-1-1'};
    state.retrievals['report-a']=retrieval;
    state.candidateJudgments['retrieval-a:candidate-1-1']={id:'latest',relation:'paraphrase'};
    state.candidateRewrites['retrieval-a:candidate-1-1']={judgment_id:'older',text:'Новая формулировка',original_text:original,
      replacement_start:Array.from(prefix).length,replacement_end:Array.from(prefix+original).length};
    bindCandidateRewrite(testCard,candidate,retrieval);
  `);
  const before = app.evaluate('state.text');
  await apply.emit('click');
  assert.equal(app.evaluate('state.text'), before, 'A rewrite tied to an older verdict must be rejected');
  app.evaluate("state.candidateRewrites['retrieval-a:candidate-1-1'].judgment_id='latest'");
  await apply.emit('click');
  assert.equal(app.evaluate('state.text'), '😀 Авторское начало.\n\nНовая формулировка\n\nСобственный вывод автора.');
  assert.equal(app.storage.get('istok.draft.v1'), app.evaluate('state.text'));
});

for (const view of ['check', 'sources']) {
  test(`registered ${view} file handler preserves a newer edit while file read is pending`, async () => {
    const app = fixture();
    await settle();
    if (view === 'sources') { await app.navigate('sources'); await settle(); }
    const target = app.element(view === 'check' ? 'check-text' : 'source-text');
    const input = app.element(view === 'check' ? 'text-file' : 'source-file');
    const reading = deferred();
    input.files = [{ name: 'article.txt', size: 10, arrayBuffer: () => reading.promise }];
    target.value = 'Исходный текст';
    const pending = input.emit('change');
    target.value = 'Новая правка';
    await target.emit('input');
    // Even an edit followed by undo is newer user intent than the file read.
    target.value = 'Исходный текст';
    await target.emit('input');
    reading.resolve(Uint8Array.from(Buffer.from('Файл должен остаться неприменённым')).buffer);
    await pending;
    assert.equal(target.value, 'Исходный текст');
    if (view === 'check') assert.equal(app.storage.get('istok.draft.v1'), 'Исходный текст');
  });
}

test('file import accepts Cyrillic/emoji and rejects oversize before changing the registered editor', async () => {
  const app = fixture();
  await settle();
  const input = app.element('text-file');
  const target = app.element('check-text');
  input.files = [textFile('Кириллица 😀\nНовая строка')];
  await input.emit('change');
  assert.equal(target.value, 'Кириллица 😀\nНовая строка');
  input.files = [textFile('я'.repeat(200001))];
  await input.emit('change');
  assert.equal(target.value, 'Кириллица 😀\nНовая строка');
});

test('registered review save preserves a newer typed comment and its draft across rerenders', async () => {
  const response = deferred();
  const app = fixture({ fetch: url => url === '/api/report-reviews' ? response.promise : undefined });
  await settle();
  for (const id of ['finding-comment', 'finding-save-status', 'quote-button', 'paraphrase-button', 'judge-button']) {
    app.document.elements.set(id, new FixtureElement(app.document, id));
  }
  const field = app.element('finding-comment');
  field.value = 'Первый комментарий';
  const reviewButton = new FixtureElement(app.document);
  reviewButton.dataset.findingDecision = 'confirmed';
  const section = new FixtureElement(app.document);
  section.querySelectorAll = () => [reviewButton];
  reviewButton.parent = section;
  app.document.querySelectorAll = selector => selector === '[data-finding-decision]' ? [reviewButton] : [];
  app.evaluate("state.report={id:'report-a',text:'Article',reviews:{}};state.selectedMatch='match-a';bindComparison({id:'match-a'})");
  const saving = reviewButton.emit('click');
  await settle();
  field.value = 'Новый комментарий во время сохранения';
  await field.emit('input');
  response.resolve(json({ review: { comment: 'Первый комментарий', decision: 'confirmed' } }));
  await saving;
  assert.equal(field.value, 'Новый комментарий во время сохранения');
  assert.equal(app.evaluate("state.report.reviews['match-a'].comment"), 'Первый комментарий');
  assert.equal(app.evaluate("state.findingDrafts['report-a:match-a']"), 'Новый комментарий во время сохранения');
  assert.equal(JSON.parse(app.storage.get('istok.review-drafts.v1')).findings['report-a:match-a'], field.value);
  assert.ok(app.evaluate("matchReviewHTML({id:'match-a'})").includes('Новый комментарий во время сохранения'));
});

test('persisted job observer survives a transient connection failure and clears ID only after completion', async () => {
  let requests = 0;
  const app = fixture({
    stored: { 'istok.retrieval-jobs.v1': JSON.stringify({ 'report-a': { id: 'job-a', status: 'running' } }) },
    fetch(url) {
      if (url !== '/api/jobs/job-a') return;
      requests++;
      if (requests === 1) throw new TypeError('Temporary local connection failure');
      assert.equal(JSON.parse(app.storage.get('istok.retrieval-jobs.v1'))['report-a'].id, 'job-a');
      return json({ job: { id: 'job-a', status: 'complete', result: { id: 'retrieval-a', report_id: 'report-a', candidates: [] } } });
    },
  });
  await settle();
  assert.equal(requests, 2);
  assert.equal(app.evaluate("state.retrievals['report-a'].id"), 'retrieval-a');
  assert.deepEqual(JSON.parse(app.storage.get('istok.retrieval-jobs.v1')), {});
  assert.ok(app.delays.includes(1000), 'Retry must use bounded backoff instead of discarding the job');
});

const modalStatus = () => ({
  model: { provider: 'modal', configured: true, can_submit: true, available: true,
    model: 'fixture-qwen', models: ['fixture-qwen', 'fixture-qwen-other'],
    local_inference_disabled: true, synthetic_only: true },
  embedding: { provider: 'modal', available: false, model: 'fixture-embedding' },
  counts: { sources: 0, reports: 0 },
});

test('Modal configuration stays distinct from GPU readiness and uses its own saved model choice', async () => {
  const calls = [];
  const app = fixture({
    stored: { 'istok.model.v1': 'qwen3:4b', 'istok.model.modal.v1': 'fixture-qwen-other' },
    fetch(url, body) {
      calls.push({ url, body });
      if (url === '/api/status') return json(modalStatus());
    },
  });
  await settle();
  assert.equal(app.evaluate('selectedModel()'), 'fixture-qwen-other');
  assert.equal(app.evaluate('modelAvailable()'), true);
  assert.ok(app.element('model-label').textContent.includes('сервер настроен'));
  assert.ok(app.element('model-dot').classes.has('muted'), 'Configured is not a green GPU-ready signal');
  assert.ok(app.element('model-description').textContent.includes('без запроса к GPU'));
  await app.element('model-settings').emit('click');
  await app.element('refresh-model').emit('click');
  app.intervals[0]();
  await settle();
  assert.ok(calls.every(call => call.body === undefined), 'Opening settings and status refresh must never submit compute');
  app.element('model-select').value = 'fixture-qwen';
  await app.element('model-select').emit('change');
  assert.equal(app.storage.get('istok.model.modal.v1'), 'fixture-qwen');
  assert.equal(app.storage.get('istok.model.v1'), 'qwen3:4b');
});

test('registered Modal compare persists its exact request before POST and observes the job', async () => {
  let submitted;
  let polls = 0;
  const app = fixture({ fetch(url, body) {
    if (url === '/api/status') return json(modalStatus());
    if (url === '/api/model-jobs') {
      submitted = body;
      const saved = JSON.parse(app.storage.get('istok.model-jobs.v1'))[body.request_id];
      assert.equal(saved.path, '/api/compare');
      assert.deepEqual(saved.body, body.body);
      assert.match(body.request_id, /^[a-f0-9]{32}$/);
      return json({ job: { id: body.request_id, status: 'queued', stage: 'В очереди' } });
    }
    if (submitted && url === `/api/jobs/${submitted.request_id}`) {
      polls++;
      return json({ job: { id: submitted.request_id, status: polls === 1 ? 'running' : 'complete',
        stage: 'Запускаем серверную модель', result: { judgment: { relation: 'same_topic', explanation: 'Учебный ответ' } } } });
    }
  } });
  await settle();
  app.element('check-text').value = 'Учебный текст для теста';
  await app.element('check-text').emit('input');
  app.element('compare-source').value = 'fixture-source';
  await app.element('compare-button').emit('click');
  assert.equal(submitted.body.text, 'Учебный текст для теста');
  assert.equal(polls, 2);
  assert.ok(app.element('direct-judgment').innerHTML.includes('Учебный ответ'));
  assert.ok(app.element('model-jobs-panel').innerHTML.includes('Ответ готов и сохранён'));
});

test('reload observes a saved rewrite without POST, editing the draft or automatically applying it', async () => {
  const id = 'a'.repeat(32);
  const calls = [];
  const entry = { request_id: id, path: '/api/rewrite', status: 'running',
    body: { report_id: 'fixture-report', match_id: 'fixture-match', mode: 'paraphrase', model: 'fixture-qwen' } };
  const app = fixture({
    stored: { 'istok.draft.v1': 'Новый учебный черновик', 'istok.model-jobs.v1': JSON.stringify({ [id]: entry }) },
    fetch(url, body) {
      calls.push({ url, body });
      if (url === '/api/status') return json(modalStatus());
      if (url === `/api/jobs/${id}`) return json({ job: { id, status: 'complete',
        result: { rewrite: { text: 'Сохранённая учебная правка', original_text: 'Исходный учебный фрагмент' } } } });
    },
  });
  await settle();
  assert.ok(calls.every(call => call.body === undefined));
  assert.equal(app.element('check-text').value, 'Новый учебный черновик');
  assert.equal(app.storage.get('istok.draft.v1'), 'Новый учебный черновик');
  assert.equal(app.evaluate('state.rewrite'), null);
  assert.ok(app.element('model-jobs-panel').innerHTML.includes('Сохранённая учебная правка'));
  assert.equal(app.element('apply-rewrite'), null);
});

test('lost submission is never automatically repeated and an explicit retry preserves the request ID', async () => {
  let firstRequest;
  const posts = [];
  const app = fixture({ fetch(url, body) {
    if (url === '/api/status') return json(modalStatus());
    if (url === '/api/model-jobs') {
      posts.push(body);
      if (!firstRequest) { firstRequest = body; throw new TypeError('Учебный обрыв связи'); }
      return json({ job: { id: body.request_id, status: 'complete', result: { judgment: { relation: 'unrelated', explanation: 'Учебный ответ после восстановления' } } } });
    }
    if (firstRequest && url === `/api/jobs/${firstRequest.request_id}`) return json({ job: {
      id: firstRequest.request_id, status: 'complete', result: { judgment: { relation: 'unrelated', explanation: 'Учебный ответ после восстановления' } },
    } });
  } });
  await settle();
  app.element('check-text').value = 'Учебный текст';
  await app.element('check-text').emit('input');
  app.element('compare-source').value = 'fixture-source';
  await app.element('compare-button').emit('click');
  await settle();
  assert.equal(posts.length, 1);
  assert.ok(JSON.parse(app.storage.get('istok.model-jobs.v1'))[firstRequest.request_id]);
  await app.element(`model-job-retry-${firstRequest.request_id}`).emit('click');
  assert.equal(posts.length, 2);
  assert.deepEqual(posts[0], posts[1]);
  assert.ok(app.element('model-jobs-panel').innerHTML.includes('Учебный ответ после восстановления'));
});

test('restored candidate judgment uses the latest saved error instead of the old positive job result', async () => {
  const id = 'b'.repeat(32);
  const entry = { request_id: id, path: '/api/candidate-judge', status: 'running',
    body: { retrieval_id: 'fixture-retrieval', candidate_id: 'candidate-1', model: 'fixture-qwen' } };
  const app = fixture({ stored: { 'istok.model-jobs.v1': JSON.stringify({ [id]: entry }) }, fetch(url) {
    if (url === '/api/status') return json(modalStatus());
    if (url === `/api/jobs/${id}`) return json({ job: { id, status: 'complete',
      result: { judgment: { id: 'old-positive', relation: 'paraphrase', explanation: 'OLD POSITIVE MUST NOT RETURN' } } } });
    if (url === '/api/candidate-judgments/fixture-retrieval') return json({ judgments: [
      { candidate_id: 'candidate-1', error: 'Новая учебная ошибка', code: 'model_error' },
    ] });
  } });
  await settle();
  assert.equal(app.evaluate("state.candidateJudgments['fixture-retrieval:candidate-1']"), undefined);
  assert.equal(app.evaluate("state.candidateErrors['fixture-retrieval:candidate-1'].message"), 'Новая учебная ошибка');
  assert.ok(!app.element('model-jobs-panel').innerHTML.includes('OLD POSITIVE MUST NOT RETURN'));
  assert.ok(app.element('model-jobs-panel').innerHTML.includes('Новая учебная ошибка'));
});

test('an unknown restored request requires a user click before it can be submitted again', async () => {
  const id = 'c'.repeat(32);
  const entry = { request_id: id, path: '/api/evaluate', status: 'submitting', body: { model: 'fixture-qwen', split: 'test' } };
  const calls = [];
  const app = fixture({ stored: { 'istok.model-jobs.v1': JSON.stringify({ [id]: entry }) }, fetch(url, body) {
    calls.push({ url, body });
    if (url === '/api/status') return json(modalStatus());
    if (url === `/api/jobs/${id}`) return json({ error: 'Нет учебного задания' }, 404);
  } });
  await settle();
  assert.ok(calls.every(call => call.body === undefined));
  assert.ok(app.element(`model-job-retry-${id}`));
  assert.equal(app.evaluate(`state.modelJobs['${id}'].status`), 'missing');
});

test('a restored uncertain failure keeps its evidence and remains distinct from a connection failure', async () => {
  const id = 'd'.repeat(32);
  const entry = { request_id: id, path: '/api/judge', status: 'running',
    body: { report_id: 'fixture-report', match_id: 'fixture-match', model: 'fixture-qwen' } };
  const app = fixture({ stored: { 'istok.model-jobs.v1': JSON.stringify({ [id]: entry }) }, fetch(url) {
    if (url === '/api/status') return json(modalStatus());
    if (url === `/api/jobs/${id}`) return json({ job: { id, status: 'failed',
      code: 'unconfirmed_exact', error: 'Учебное совпадение не подтверждено',
      details: { final_judgment: { explanation: 'Учебное пояснение', evidence_text: 'Фрагмент A', evidence_source: 'Фрагмент B' } },
    } });
  } });
  await settle();
  const html = app.element('model-jobs-panel').innerHTML;
  assert.ok(html.includes('Нужна проверка редактора'));
  assert.ok(html.includes('Фрагмент A') && html.includes('Фрагмент B'));
  assert.ok(JSON.parse(app.storage.get('istok.model-jobs.v1'))[id]);
});

test('a restored running model job can be cancelled without submitting new compute', async () => {
  const id = 'e'.repeat(32);
  const waiting = deferred();
  let polls = 0;
  const calls = [];
  const entry = { request_id: id, path: '/api/evaluate', status: 'running', body: { model: 'fixture-qwen', split: 'test' } };
  const app = fixture({ stored: { 'istok.model-jobs.v1': JSON.stringify({ [id]: entry }) }, fetch(url, body) {
    calls.push({ url, body });
    if (url === '/api/status') return json(modalStatus());
    if (url === `/api/jobs/${id}`) {
      polls++;
      return polls === 1 ? json({ job: { id, status: 'running', stage: 'Запускаем серверную модель' } }) : waiting.promise;
    }
    if (url === `/api/jobs/${id}/cancel`) return json({ job: { id, status: 'cancelling', stage: 'Останавливаем после текущего ответа' } });
  } });
  await settle();
  await app.element(`model-job-cancel-${id}`).emit('click');
  assert.ok(app.element('model-jobs-panel').innerHTML.includes('Останавливаем после текущего ответа'));
  waiting.resolve(json({ job: { id, status: 'cancelled' } }));
  await settle();
  assert.ok(app.element('model-jobs-panel').innerHTML.includes('Задание остановлено'));
  assert.ok(calls.every(call => call.body === undefined || call.url.endsWith('/cancel')));
});

test('registered comparison retains the legacy local API path when Modal is not selected', async () => {
  const calls = [];
  const app = fixture({ fetch(url, body) {
    calls.push({ url, body });
    if (url === '/api/compare') return json({ judgment: { relation: 'same_topic', explanation: 'Учебный локальный ответ' } });
  } });
  await settle();
  app.element('check-text').value = 'Учебный текст';
  await app.element('check-text').emit('input');
  app.element('compare-source').value = 'fixture-source';
  await app.element('compare-button').emit('click');
  assert.ok(calls.some(call => call.url === '/api/compare'));
  assert.ok(!calls.some(call => call.url === '/api/model-jobs'));
  assert.ok(app.element('direct-judgment').innerHTML.includes('Учебный локальный ответ'));
});

test('late availability updates the evaluation warning without recreating the current form or drafts', async () => {
  const status = deferred();
  let configured = true;
  let reads = 0;
  const app = fixture({ view: 'evaluation', fetch(url) {
    if (url === '/api/status') {
      reads++;
      if (reads === 1) return status.promise;
      const value = modalStatus();
      value.model.can_submit = configured;
      return json(value);
    }
  } });
  await settle();
  const examples = app.element('example-list');
  const warning = app.element('evaluation-model-unavailable');
  const button = app.element('run-evaluation');
  assert.equal(warning.hidden, false);
  assert.equal(button.disabled, true);
  app.evaluate("state.exampleDrafts['fixture-example']={relation:'same_topic',comment:'Несохранённая учебная правка'}");
  status.resolve(json(modalStatus()));
  await settle();
  assert.equal(warning.hidden, true);
  assert.equal(button.disabled, false);
  assert.equal(app.element('example-list'), examples);
  assert.equal(app.element('run-evaluation'), button);
  assert.equal(app.evaluate("state.exampleDrafts['fixture-example'].comment"), 'Несохранённая учебная правка');
  configured = false;
  app.intervals[0]();
  await settle();
  assert.equal(warning.hidden, false);
  assert.equal(button.disabled, true);
  assert.equal(app.element('example-list'), examples);
});

test('a restored evaluation blocks the same model and payload until terminal status without another POST', async () => {
  const id = 'f'.repeat(32);
  const terminal = deferred();
  const calls = [];
  let polls = 0;
  // Key order deliberately differs from the registered evaluation handler.
  const entry = { request_id: id, path: '/api/evaluate', status: 'running', body: { split: 'test', model: 'fixture-qwen' } };
  const app = fixture({ view: 'evaluation', stored: { 'istok.model-jobs.v1': JSON.stringify({ [id]: entry }) }, fetch(url, body) {
    calls.push({ url, body });
    if (url === '/api/status') return json(modalStatus());
    if (url === `/api/jobs/${id}`) {
      polls++;
      return polls === 1 ? json({ job: { id, status: 'running', stage: 'Сравниваем' } }) : terminal.promise;
    }
  } });
  await settle();
  const button = app.element('run-evaluation');
  assert.equal(app.element('evaluation-model-unavailable').hidden, true);
  assert.equal(button.disabled, true);
  assert.equal(app.element('evaluation-job-note').hidden, false);
  app.element('model-select').value = 'fixture-qwen-other';
  await app.element('model-select').emit('change');
  assert.equal(button.disabled, false, 'A different model is a different requested evaluation');
  app.element('model-select').value = 'fixture-qwen';
  await app.element('model-select').emit('change');
  assert.equal(button.disabled, true);
  // The shared helper also prevents duplicate compute if invoked directly.
  const joining = app.evaluate("modelRequest('/api/evaluate',{model:'fixture-qwen',split:'test'})");
  await settle();
  assert.ok(calls.every(call => call.body === undefined));
  terminal.resolve(json({ job: { id, status: 'complete', result: { run: { model: 'fixture-qwen', metrics: {}, results: [] } } } }));
  await joining;
  await settle();
  assert.equal(button.disabled, false);
  assert.equal(app.element('evaluation-job-note').hidden, true);
  assert.ok(calls.every(call => call.body === undefined));
});

test('manual pair completion refreshes the replacement card after its original card was unmounted', async () => {
  const completion = deferred();
  const latest = { id: 'fixture-latest', relation: 'paraphrase', explanation: 'Новая учебная оценка' };
  let request;
  const app = fixture({ fetch(url, body) {
    if (url === '/api/status') return json(modalStatus());
    if (url === '/api/model-jobs') {
      request = body;
      return json({ job: { id: body.request_id, status: 'queued' } });
    }
    if (request && url === `/api/jobs/${request.request_id}`) return completion.promise;
    if (url === '/api/candidate-judgments/fixture-retrieval') return json({ judgments: [{ candidate_id: 'candidate-1', judgment: latest }] });
  } });
  await settle();
  function cardFixture() {
    const card = new FixtureElement(app.document);
    card.dataset = { retrieval: 'fixture-retrieval', candidate: 'candidate-1' };
    const button = new FixtureElement(app.document);
    button.dataset.candidateAction = 'judge';
    button.parent = card;
    card.querySelectorAll = selector => selector === '[data-candidate-action]' ? [button] : [];
    for (const selector of ['.candidate-judgment', '.candidate-error', '.candidate-rewrite', '.candidate-actions']) {
      card.children.set(selector, new FixtureElement(app.document));
    }
    return { card, button };
  }
  const original = cardFixture();
  let current = original;
  app.document.querySelectorAll = selector => selector === '[data-candidate]' ? [current.card]
    : selector === '[data-candidate-action]' ? [current.button] : [];
  app.expose('originalCard', original.card);
  app.evaluate(`
    globalThis.retrieval={id:'fixture-retrieval',report_id:'fixture-report',candidates:[{id:'candidate-1',rank:1,source_title:'Учебный источник'}]};
    state.report={id:'fixture-report',text:'Учебный текст',matches:[],stats:{}};
    state.retrievals['fixture-report']=retrieval;
    bindCandidateCardActions(originalCard,retrieval.candidates[0],retrieval);
  `);
  const running = original.button.emit('click');
  await settle();
  // Leaving and returning to the view replaces the card while the job survives.
  original.card.isConnected = false;
  original.button.isConnected = false;
  current = cardFixture();
  current.button.disabled = true;
  current.card.children.get('.candidate-judgment').innerHTML = 'Предыдущая карточка';
  completion.resolve(json({ job: { id: request.request_id, status: 'complete', result: { judgment: latest } } }));
  await running;
  assert.ok(current.card.children.get('.candidate-judgment').innerHTML.includes('Новая учебная оценка'));
  assert.equal(current.button.disabled, false);
  assert.equal(app.evaluate('candidateRequests.size'), 0);
});

test('refreshing another model job preserves an expanded result and keyboard focus', async () => {
  const app = fixture();
  await settle();
  const first = '1'.repeat(32), second = '2'.repeat(32);
  app.evaluate(`
    state.modelJobs['${first}']={request_id:'${first}',path:'/api/judge',body:{},status:'complete',
      job:{status:'complete',result:{judgment:{relation:'same_topic',explanation:'Учебный результат'}}}};
    state.modelJobs['${second}']={request_id:'${second}',path:'/api/evaluate',body:{},status:'running',job:{id:'${second}',status:'running',stage:'В очереди'}};
    renderModelJobs();
  `);
  const details = app.element(`model-job-result-${first}`);
  details.open = true;
  app.element(`model-job-summary-${first}`).focus();
  app.evaluate(`state.modelJobs['${second}'].job.stage='Сравниваем';renderModelJobs()`);
  assert.equal(app.element(`model-job-result-${first}`).open, true);
  assert.equal(app.document.activeElement, app.element(`model-job-summary-${first}`));
  const editor = app.element('check-text');
  editor.value = 'Учебная правка в редакторе';
  editor.focus();
  app.evaluate('renderModelJobs()');
  assert.equal(app.document.activeElement, editor, 'Polling must not steal focus from the editor');
  assert.equal(editor.value, 'Учебная правка в редакторе');
});

test('an explicit submission rejection survives reload with its original reason and no automatic retry', async () => {
  const reason = 'Уже запущены три проверки. Дождитесь завершения одной из них.';
  const app = fixture({ fetch(url) {
    if (url === '/api/status') return json(modalStatus());
    if (url === '/api/model-jobs') return json({ error: reason }, 400);
  } });
  await settle();
  app.element('check-text').value = 'Учебный текст';
  await app.element('check-text').emit('input');
  app.element('compare-source').value = 'fixture-source';
  await app.element('compare-button').emit('click');
  const saved = app.storage.get('istok.model-jobs.v1');
  const [entry] = Object.values(JSON.parse(saved));
  assert.equal(entry.status, 'rejected');
  assert.equal(entry.submission_error.message, reason);
  assert.equal(entry.submission_error.status, 400);
  const calls = [];
  const reloaded = fixture({ stored: { 'istok.model-jobs.v1': saved }, fetch(url, body) {
    calls.push({ url, body });
    if (url === '/api/status') return json(modalStatus());
  } });
  await settle();
  assert.ok(reloaded.element('model-jobs-panel').innerHTML.includes(reason));
  assert.ok(reloaded.element('model-jobs-panel').innerHTML.includes('Сервер не принял задание'));
  assert.ok(reloaded.element(`model-job-retry-${entry.request_id}`));
  assert.ok(calls.every(call => call.body === undefined && !call.url.startsWith('/api/jobs/')));
});

test('registered HTML export downloads the captured report while a different report is opened', async () => {
  const exported = deferred();
  const calls = [];
  const app = fixture({ fetch(url, body) {
    calls.push({ url, body });
    if (url === '/api/report-exports/report-a') return exported.promise;
  } });
  await settle();
  app.evaluate(`
    saveDraft('Учебная статья A');
    state.report={id:'report-a',text:state.text,matches:[],stats:{},issues:[]};
    state.retrievals['report-a']=null;
    renderReport();
  `);
  assert.ok(app.element('report-area').innerHTML.includes('↓ Данные JSON'));
  const downloading = app.element('download-reading-report').emit('click');
  await settle();
  assert.equal(app.downloads.length, 0);
  app.evaluate(`state.report={id:'report-b',text:'Учебная статья B',matches:[],stats:{},issues:[]};state.retrievals['report-b']=null;renderReport()`);
  const html = '<!doctype html><html lang="ru"><title>Учебный отчёт A</title></html>';
  exported.resolve(json({ filename: 'report-a.html', html }));
  await downloading;
  assert.equal(app.downloads.length, 1);
  assert.equal(app.downloads[0].filename, 'report-a.html');
  assert.equal(app.downloads[0].blob.type, 'text/html;charset=utf-8');
  assert.equal(await app.downloads[0].blob.text(), html);
  assert.equal(app.evaluate('state.report.id'), 'report-b');
  assert.equal(app.storage.get('istok.draft.v1'), 'Учебная статья A');
  assert.ok(calls.some(call => call.url === '/api/report-exports/report-a'));
  assert.ok(!calls.some(call => call.url === '/api/report-exports/report-b'));
  assert.ok(calls.every(call => call.body === undefined));
});

test('registered HTML export displays a server error and downloads nothing on failure', async () => {
  const app = fixture({ fetch(url) {
    if (url === '/api/report-exports/report-a') return json({ error: 'Учебная ошибка экспорта' }, 500);
  } });
  await settle();
  app.evaluate(`state.report={id:'report-a',text:'Учебная статья',matches:[],stats:{},issues:[]};state.retrievals['report-a']=null;renderReport()`);
  await app.element('download-reading-report').emit('click');
  assert.equal(app.downloads.length, 0);
  assert.equal(app.element('toast').textContent, 'Учебная ошибка экспорта');
  assert.equal(app.element('download-reading-report').disabled, false);
});
