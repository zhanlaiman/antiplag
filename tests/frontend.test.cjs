'use strict';

// These tests execute the complete application with controlled DOM/network seams.
// They verify state, asynchronous ordering and registered edit handlers. They do
// not verify browser layout, native validation or browser-specific file decoding.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

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

function fixture({ stored = {}, fetch: customFetch } = {}) {
  const storage = new Map(Object.entries(stored));
  const delays = [];
  const windowListeners = new Map();
  const document = {
    elements: new Map(), body: { classList: { toggle() {} } },
    querySelector(selector) { return selector.startsWith('#') ? this.elements.get(selector.slice(1)) || null : null; },
    querySelectorAll() { return []; },
    getElementById(id) { return this.elements.get(id) || null; },
  };
  const initial = new FixtureElement(document);
  initial.innerHTML = documentHTML;
  const location = { hash: '#check' };
  const context = vm.createContext({
    document, location, console, TextDecoder, Uint8Array, Event, URL,
    Option: class { constructor(text, value) { this.text = text; this.value = value; } },
    localStorage: {
      getItem: key => storage.get(key) ?? null,
      setItem: (key, value) => { storage.set(key, value); },
    },
    window: { addEventListener: (name, callback) => windowListeners.set(name, callback), matchMedia: () => ({ matches: false }) },
    setInterval() {},
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
      };
      assert.ok(Object.hasOwn(defaults, url), `Unexpected API request ${url}`);
      return json(defaults[url]);
    },
  });
  // Execute every statement, including startup and normal listener registration.
  vm.runInContext(application, context, { filename: 'web/app.js' });
  return {
    document, storage, delays,
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
