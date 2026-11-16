'use strict';
// Full shipped app + real Chromium; all API responses below are SYNTHETIC.
// This is frontend contract/interaction evidence, never native/model-quality proof.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const http = require('node:http');
const { createHash } = require('node:crypto');
let chromium;
try { ({ chromium } = require(process.env.ISTOK_PLAYWRIGHT_MODULE || 'playwright')); } catch { /* Optional browser runner. */ }
const run = (name, fn) => test(name, { skip: !chromium }, fn);
const root = path.resolve(__dirname, '..');
const hash = value => createHash('sha256').update(value).digest('hex');
const fragment = 'Автор описал собственный опыт и сохранил ссылку [1] в 2026 году.';
const rewritten = 'Свой опыт автор изложил по-новому, сохранив ссылку [1] и 2026 год.';
let browser, server, origin;
test.before(async () => {
  if (!chromium) return;
  server = http.createServer(async (req, res) => {
    try {
      const name = new URL(req.url, 'http://localhost').pathname;
      const file = path.resolve(root, 'web', '.' + (name === '/' ? '/index.html' : name));
      if (!file.startsWith(path.join(root, 'web') + path.sep)) throw new Error('outside web');
      const mime = { '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html', '.svg': 'image/svg+xml',
        '.woff2': 'font/woff2', '.ttf': 'font/ttf', '.png': 'image/png', '.ico': 'image/x-icon' }[path.extname(file)];
      res.writeHead(200, { 'Content-Type': mime || 'application/octet-stream' }); res.end(await fs.readFile(file));
    } catch { res.writeHead(404); res.end('fixture static file unavailable'); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  origin = `http://127.0.0.1:${server.address().port}`;
  browser = await chromium.launch({ headless: true, executablePath: chromium.executablePath() });
});
test.after(async () => {
  await browser?.close();
  server?.closeAllConnections();
  if (server) await new Promise(resolve => server.close(resolve));
});

async function fixture(t, options = {}) {
  const context = await browser.newContext({ viewport: options.viewport || { width: 1440, height: 1080 } });
  const page = await context.newPage();
  page.setDefaultTimeout(4000);
  const calls = [], foreign = [], errors = [], jobs = new Map();
  const cap = { enabled: true, version: 'qwen-rewrite-own-v1', max_characters: 1000, max_output_characters: 1500 };
  const session = { mode: 'local', user: null, beta: { id: 'fixture-tenant', owner: false, max_characters: 200000,
    ai_correction: { enabled: true, rewrite: options.cap === undefined ? cap : options.cap } } };
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => {
    if (message.type() === 'error' && !(options.unknownPost && /503|ERR_FAILED/.test(message.text()))) errors.push(message.text());
  });
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.origin !== origin) { foreign.push(url.origin); return route.abort(); }
    if (!url.pathname.startsWith('/api/')) return route.continue();
    const request = route.request();
    const body = request.postData() ? JSON.parse(request.postData()) : null;
    calls.push({ path: url.pathname, method: request.method(), body });
    let response;
    if (url.pathname === '/api/session') response = session;
    else if (url.pathname === '/api/status') response = { model: { provider: 'disabled', available: false, can_submit: false,
      local_inference_disabled: true, models: [] }, counts: { sources: 0, reports: 0 },
      web: { configured: true, enabled: true, provider: 'serpapi-yandex', max_queries: 6, max_urls: 12 } };
    else if (url.pathname === '/api/sources') response = { sources: [] };
    else if (url.pathname === '/api/source-packs') response = { packs: [] };
    else if (url.pathname === '/api/correction-jobs') {
      jobs.set(body.request_id, body);
      if (options.unknownPost) return route.abort('failed');
      response = { job: { id: body.request_id, status: 'queued' } };
    } else if (url.pathname.startsWith('/api/correction-jobs/')) {
      if (options.gate) await options.gate;
      if (options.failGet) return route.fulfill({ status: 503, json: { error: 'Synthetic lookup unavailable' } });
      const id = url.pathname.split('/').at(-1), saved = jobs.get(id) || options.savedBody;
      assert.ok(saved, 'Only a known fixture job may be read');
      const result = saved.mode ? { mode: 'rewrite_own', version: 'qwen-rewrite-own-v1',
        rewritten_text: options.output || rewritten, text_sha256: hash(saved.text),
        protected_literals_sha256: hash(JSON.stringify(saved.protected_literals)), offset_encoding: 'utf-16', requires_review: true }
        : { issues: [], limitations: 'Synthetic grammar result; no model called.' };
      if (options.mutate) options.mutate(result, saved);
      response = { job: { id, status: 'complete', result: { correction: { text: saved.text, result } } } };
    } else throw new Error(`Unexpected fixture API ${request.method()} ${url.pathname}`);
    await route.fulfill({ json: response });
  });
  if (options.stored) await context.addInitScript(stored => {
    for (const [key, value] of Object.entries(stored)) localStorage.setItem(key, value);
  }, options.stored);
  await page.goto(origin + '/#' + (options.surface || 'home'));
  await page.locator('#qwen-open-' + (options.surface || 'home')).waitFor();
  t.after(async () => { assert.deepEqual(foreign, []); assert.deepEqual(errors, []); await context.close(); });
  return { page, calls, options, session, posts: () => calls.filter(call => call.path === '/api/correction-jobs'),
    async open(text = fragment, surface = options.surface || 'home', range) {
      await page.locator('#' + surface + '-text').fill(text);
      if (range) await page.locator('#' + surface + '-text').evaluate((field, [a, b]) => field.setSelectionRange(a, b), range);
      await page.locator('#qwen-open-' + surface).click();
    },
    async submit(surface = options.surface || 'home') {
      await page.locator('#qwen-consent-' + surface).check();
      await page.locator('#qwen-submit-' + surface).click();
    },
    async preview(surface = options.surface || 'home') { await page.locator('#qwen-preview-' + surface + ' h3').first().waitFor(); },
    async waitGet() { await page.waitForFunction(() => Object.values(state.rewriteJobs).some(entry => entry.status !== 'submitting')); }
  };
}

run('capability is explicit; open sends no POST and grammar remains separate', async t => {
  for (const cap of [null, { enabled: false }, { enabled: true, version: 'unknown', max_characters: 1000, max_output_characters: 1500 },
    { enabled: true, version: 'qwen-rewrite-own-v1', max_characters: 3000, max_output_characters: 1500 }]) {
    const app = await fixture(t, { cap }); await app.open();
    assert.equal(await app.page.locator('#qwen-submit-home').isDisabled(), true);
    assert.equal(app.posts().length, 0);
    assert.match(await app.page.locator('.ai-proofreader').textContent(), /3000/);
  }
});

run('explicit consent, hashed preview, selected-only single apply, and 1440/390 layout', async t => {
  for (const width of [1440, 390]) {
    const app = await fixture(t, { viewport: { width, height: 1080 } });
    const before = 'Собственный вводный текст. '.repeat(55), after = ' Заключение автора.'.repeat(30);
    const draft = before + fragment + after;
    await app.open(draft, 'home', [before.length, before.length + fragment.length]);
    const savedReport = await app.page.evaluate(({ draft, fragment, start }) => {
      state.homeReport = { id: '8'.repeat(32), text: draft, scope: 'web_search', created_at: '2026-11-11T10:00:00Z',
        source_snapshots_count: 1, stats: { uniqueness_percent: 58.3, similarity_percent: 30, source_count: 1 },
        matches: [{ id: 'fixture-match', source_id: 'fixture-source', source_url: '', source_title: 'Учебный источник',
          start, end: start + fragment.length, excerpt: fragment, source_excerpt: fragment, kind: 'exact', score: 1 }],
        issues: [], web: { status: 'complete', queries_planned: 2, queries_completed: 2, sources_checked: 1,
          urls_attempted: 1, errors: [], limitations: ['Synthetic fixture report; no discovery executed.'] } };
      renderHomeResult(); return JSON.stringify(state.homeReport);
    }, { draft, fragment, start: before.length });
    await app.page.locator('#qwen-result-home').click();
    assert.equal(await app.page.locator('#qwen-fragment-home').inputValue(), fragment);
    assert.equal(await app.page.locator('#qwen-fragment-home').evaluate(element =>
      element.getBoundingClientRect().top >= document.querySelector('.home-nav').getBoundingClientRect().bottom), true);
    await app.page.locator('#qwen-submit-home').click();
    assert.equal(app.posts().length, 0, 'consent is required');
    await app.page.locator('#qwen-protected-home').fill('[1]\n2026\n');
    await app.submit(); await app.preview();
    const post = app.posts()[0].body;
    assert.deepEqual(Object.keys(post).sort(), ['ai_consent', 'mode', 'protected_literals', 'request_id', 'text', 'version']);
    assert.equal(post.mode, 'rewrite_own'); assert.equal(post.version, 'qwen-rewrite-own-v1');
    assert.deepEqual(post.protected_literals, ['[1]', '2026']); assert.equal(post.text, fragment);
    assert.equal(await app.page.locator('#qwen-apply-home').isDisabled(), true);
    await app.page.locator('#qwen-review-home').check();
    assert.equal(await app.page.locator('#qwen-apply-home').isEnabled(), true);
    if (process.env.ISTOK_QWEN_SCREENSHOTS) {
      await fs.mkdir(process.env.ISTOK_QWEN_SCREENSHOTS, { recursive: true });
      await app.page.locator('#qwen-title-home').evaluate(element => {
        const top = scrollY + element.getBoundingClientRect().top - document.querySelector('.home-nav').getBoundingClientRect().height - 24;
        scrollTo(0, top);
      });
      await app.page.screenshot({ path: path.join(process.env.ISTOK_QWEN_SCREENSHOTS, `qwen-${width}-intro.png`) });
      await app.page.locator('#qwen-preview-home').evaluate(element => element.scrollIntoView({ block: 'center' }));
      await app.page.screenshot({ path: path.join(process.env.ISTOK_QWEN_SCREENSHOTS, `qwen-${width}-preview.png`) });
    }
    assert.equal(await app.page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await app.page.locator('#qwen-apply-home').click();
    assert.equal(await app.page.locator('#home-text').inputValue(), before + rewritten + after);
    assert.equal(app.posts().length, 1, 'apply must never launch another job/search');
    assert.equal(await app.page.evaluate(() => JSON.stringify(state.homeReport)), savedReport);
    assert.equal(await app.page.locator('#home-report-stale').isVisible(), true);
    assert.equal(await app.page.locator('#qwen-apply-home').isDisabled(), true);
    assert.equal(app.calls.some(call => /web-check|model-jobs|rewrite-jobs/.test(call.path)), false);
    await app.page.locator('#qwen-close-home').click();
    assert.equal(await app.page.locator('#qwen-open-home').evaluate(element => element === document.activeElement), true);
  }
});

run('Unicode admission/output boundaries and untrusted HTML stay literal', async t => {
  const tooLong = await fixture(t); await tooLong.open();
  await tooLong.page.locator('#qwen-fragment-home').fill('😀'.repeat(1001)); await tooLong.submit();
  assert.equal(tooLong.posts().length, 0);
  for (const count of [1500, 1501]) {
    const app = await fixture(t, { output: '😀'.repeat(count) }); await app.open('😀'.repeat(1000)); await app.submit();
    await app.page.waitForFunction(() => !activeRewritePanel.busy);
    assert.equal(app.posts()[0].body.text.length, 2000, '1000 Unicode codepoints, 2000 UTF16 units');
    await app.page.locator('#qwen-review-home').check();
    assert.equal(await app.page.locator('#qwen-apply-home').isEnabled(), count === 1500);
  }
  const html = '<img src="https://example.invalid/x" onerror="window.bad=true">';
  const app = await fixture(t, { output: html }); await app.open(); await app.submit(); await app.preview();
  assert.equal(await app.page.locator('#qwen-preview-home img').count(), 0);
  assert.match(await app.page.locator('#qwen-preview-home').textContent(), /<img/);
  assert.equal(await app.page.evaluate(() => window.bad), undefined);
});

run('mode/version/hash/encoding/review/protected occurrence mismatches refuse Apply', async t => {
  for (const mutate of [r => { r.mode = 'grammar'; }, r => { r.version = 'other'; }, r => { r.text_sha256 = '0'.repeat(64); },
    r => { r.protected_literals_sha256 = '0'.repeat(64); }, r => { r.offset_encoding = 'python'; },
    r => { r.requires_review = false; }, r => { r.rewritten_text = 'Потерянная ссылка и число.'; }]) {
    const app = await fixture(t, { mutate }); await app.open();
    await app.page.locator('#qwen-protected-home').fill('[1]\n2026'); await app.submit();
    await app.page.waitForFunction(() => !activeRewritePanel.busy);
    await app.page.locator('#qwen-review-home').check();
    assert.equal(await app.page.locator('#qwen-apply-home').isDisabled(), true);
  }
});

run('ABA through user input, programmatic assignment and TXT import invalidates binding', async t => {
  for (const kind of ['input', 'assignment', 'import', 'setRangeText', 'saveDraft']) {
    const surface = kind === 'saveDraft' ? 'check' : 'home';
    const app = await fixture(t, { surface }); await app.open(); await app.submit(); await app.preview();
    await app.page.locator('#qwen-review-' + surface).check();
    if (kind === 'input') { await app.page.locator('#home-text').fill('Изменённый черновик'); await app.page.locator('#home-text').fill(fragment); }
    else if (kind === 'assignment') await app.page.evaluate(() => { const f = document.querySelector('#home-text'), old = f.value; f.value = 'B'; f.value = old; });
    else if (kind === 'setRangeText') await app.page.evaluate(() => { const f = document.querySelector('#home-text'), old = f.value; f.setRangeText('B', 0, f.value.length); f.setRangeText(old, 0, f.value.length); });
    else if (kind === 'saveDraft') await app.page.evaluate(() => { const old = state.text; saveDraft('B'); saveDraft(old); });
    else {
      for (const value of ['Импортированный текст', fragment]) {
        await app.page.locator('#home-file').setInputFiles({ name: 'owned.txt', mimeType: 'text/plain', buffer: Buffer.from(value) });
        await app.page.waitForFunction(value => document.querySelector('#home-text').value === value, value);
      }
    }
    assert.equal(await app.page.locator('#qwen-apply-' + surface).isDisabled(), true, kind);
    assert.equal(app.posts().length, 1);
  }
});

run('fragment/protected ABA while GET is pending cannot resurrect a preview', async t => {
  let release; const gate = new Promise(resolve => { release = resolve; });
  const app = await fixture(t, { gate }); await app.open(); await app.submit(); await app.waitGet();
  await app.page.evaluate(() => { const f = document.querySelector('#qwen-fragment-home'), old = f.value; f.value = 'B'; f.value = old; });
  release(); await app.page.waitForFunction(() => !activeRewritePanel.busy);
  assert.equal(await app.page.locator('#qwen-preview-home').textContent(), '');
  assert.equal(await app.page.locator('#qwen-apply-home').isDisabled(), true);
});

run('fragment/protected form ABA during pending Apply hashes cannot write a hidden preview', async t => {
  for (const name of ['fragment', 'protected']) {
    const app = await fixture(t); await app.open(); await app.submit(); await app.preview();
    await app.page.locator('#qwen-review-home').check();
    await app.page.evaluate(() => {
      const digest = crypto.subtle.digest.bind(crypto.subtle);
      window.applyDigestWaiters = [];
      crypto.subtle.digest = (...args) => new Promise((resolve, reject) => {
        window.applyDigestWaiters.push(() => digest(...args).then(resolve, reject));
      });
    });
    await app.page.locator('#qwen-apply-home').click();
    await app.page.waitForFunction(() => activeRewritePanel.applying && window.applyDigestWaiters.length === 3);
    await app.page.evaluate(name => {
      const field = document.getElementById(`qwen-${name}-home`), original = field.value;
      field.value = 'Другой фрагмент'; field.value = original;
    }, name);
    assert.equal(await app.page.locator('#qwen-preview-home').textContent(), '');
    assert.equal(await app.page.locator('#qwen-review-home').isChecked(), false);
    await app.page.locator('#qwen-review-home').check();
    await app.page.evaluate(() => window.applyDigestWaiters.splice(0).forEach(release => release()));
    await app.page.waitForFunction(() => !activeRewritePanel.applying);
    assert.equal(await app.page.locator('#home-text').inputValue(), fragment, name);
    assert.equal(await app.page.locator('#qwen-apply-home').isDisabled(), true);
    assert.equal(await app.page.evaluate(() => activeRewritePanel.verified === null && !activeRewritePanel.entry.applied), true);
    assert.equal(app.posts().length, 1);
  }
});

run('route, replacement field and tenant transitions reject delayed jobs', async t => {
  for (const kind of ['route', 'field', 'tenant']) {
    let release; const gate = new Promise(resolve => { release = resolve; });
    const app = await fixture(t, { gate }); await app.open(); await app.submit(); await app.waitGet();
    if (kind === 'route') await app.page.evaluate(() => { location.hash = '#check'; });
    else if (kind === 'field') await app.page.evaluate(() => { const f = document.querySelector('#home-text'), next = f.cloneNode(true); next.value = f.value; f.replaceWith(next); });
    else await app.page.evaluate(() => { state.session.beta.id = 'other-tenant'; hydrateSessionStorage(); renderView(); });
    release(); await app.page.waitForFunction(() => rewriteObservers.size === 0);
    assert.equal(app.posts().length, 1);
    assert.equal(await app.page.evaluate(() => localStorage.getItem('istok.qwen-rewrite-jobs.v1.saas.beta.other-tenant')), null);
    assert.equal(await app.page.evaluate(() => document.querySelector('#home-text')?.value || state.homeText), kind === 'tenant' ? '' : fragment);
  }
});

run('ambiguous pasted fragment has preview only and never replaces the long document', async t => {
  const app = await fixture(t); const draft = fragment + ' Связующий текст. '.repeat(80) + fragment;
  await app.open(draft); await app.page.locator('#qwen-fragment-home').fill(fragment); await app.submit(); await app.preview();
  await app.page.locator('#qwen-review-home').check();
  assert.equal(await app.page.locator('#qwen-apply-home').isDisabled(), true);
  assert.equal(await app.page.locator('#home-text').inputValue(), draft);
});

run('unknown delivery uses same-ID GET only; reload cache is separate and read-only', async t => {
  const options = { unknownPost: true, failGet: true };
  const app = await fixture(t, options); await app.open(); await app.submit();
  await app.page.waitForFunction(() => activeRewritePanel.entry?.status === 'missing');
  const id = app.posts()[0].body.request_id;
  assert.equal(await app.page.locator('#qwen-submit-home').isDisabled(), true);
  await app.page.locator('#qwen-recover-home').click();
  assert.equal(app.posts().length, 1);
  options.failGet = false; await app.page.locator('#qwen-recover-home').click(); await app.preview();
  assert.equal(app.posts().length, 1);
  assert.ok(app.calls.filter(c => c.path.includes('/api/correction-jobs/')).every(c => c.path.endsWith(id)));
  assert.equal(await app.page.evaluate(() => localStorage.getItem('istok.model-jobs.v1.saas.beta.fixture-tenant')), null);
  await app.page.reload(); await app.page.locator('#qwen-open-home').waitFor(); await app.page.locator('#qwen-open-home').click(); await app.preview();
  await app.page.locator('#qwen-review-home').check();
  assert.equal(await app.page.locator('#qwen-apply-home').isDisabled(), true);
  assert.equal(app.posts().length, 1);
});

run('legacy grammar still submits the original3000-character schema', async t => {
  const app = await fixture(t); const text = 'Свой текст. '.repeat(250);
  assert.equal(text.length, 3000); await app.page.locator('#home-text').fill(text);
  await app.page.locator('.proofreader > summary').click();
  await app.page.locator('.ai-proofreader > summary').click();
  await app.page.locator('#ai-consent-home').check(); await app.page.locator('#ai-start-home').click();
  await app.page.waitForFunction(() => Object.values(state.modelJobs).some(entry => entry.status === 'complete'));
  assert.deepEqual(Object.keys(app.posts()[0].body).sort(), ['ai_consent', 'request_id', 'text']);
  assert.equal(app.posts()[0].body.text, text);
  assert.equal(await app.page.evaluate(() => localStorage.getItem('istok.qwen-rewrite-jobs.v1.saas.beta.fixture-tenant')), null);
});
