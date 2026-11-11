'use strict';

const DRAFT_KEY = 'istok.draft.v1';
const MODEL_KEY = 'istok.model.v1';
const RETRIEVAL_JOBS_KEY = 'istok.retrieval-jobs.v1';
const CANDIDATE_JOBS_KEY = 'istok.candidate-jobs.v1';
const ACTIVE_JOB_STATUSES = ['queued', 'running', 'cancelling'];
const retrievalPolls = new Set();
const candidateRequests = new Set();
const candidateJobPolls = new Set();

function restoreDraft() {
  try {
    const draft = localStorage.getItem(DRAFT_KEY);
    return typeof draft === 'string' ? draft : '';
  } catch {
    return '';
  }
}

function saveDraft(text) {
  if (state.text !== text) document.querySelector('#direct-judgment')?.replaceChildren();
  state.text = text;
  try {
    localStorage.setItem(DRAFT_KEY, text);
    state.draftSaved = true;
  } catch {
    state.draftSaved = false;
  }
  updateDraftStatus();
  updateReportFreshness();
}

function restoreModel() {
  try {
    return localStorage.getItem(MODEL_KEY) || '';
  } catch {
    return '';
  }
}

function restoreActiveJobs(storageKey) {
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey) || '{}');
    if (!saved || typeof saved !== 'object' || Array.isArray(saved)) return {};
    return Object.fromEntries(Object.entries(saved).filter(([reportId, job]) => (
      reportId.length <= 100 && job && typeof job.id === 'string' && job.id.length <= 100
      && ACTIVE_JOB_STATUSES.includes(job.status)
    )).map(([reportId, job]) => [reportId, {
      id: job.id, status: job.status, stage: 'Возобновляем наблюдение за проверкой',
    }]));
  } catch {
    return {};
  }
}

function saveActiveJobs(storageKey, jobs) {
  const active = Object.fromEntries(Object.entries(jobs)
    .filter(([, job]) => job.id && ACTIVE_JOB_STATUSES.includes(job.status))
    .map(([reportId, job]) => [reportId, { id: job.id, status: job.status }]));
  try {
    localStorage.setItem(storageKey, JSON.stringify(active));
  } catch {
    // Polling in this open tab continues when browser storage is unavailable.
  }
}

function updateDraftStatus() {
  const status = document.querySelector('#draft-status');
  if (!status) return;
  status.textContent = state.draftSaved === false
    ? 'Черновик хранится только в открытой вкладке'
    : 'Черновик сохраняется на этом Mac';
}

function updateReportFreshness() {
  const stale = Boolean(state.report && state.text !== state.report.text);
  const notice = document.querySelector('#stale-report');
  if (notice) notice.hidden = !stale;
  const apply = document.querySelector('#apply-rewrite');
  if (apply) apply.disabled = stale;
  const retrieval = state.retrievals[state.report?.id];
  document.querySelectorAll('[data-apply-candidate]').forEach(button => {
    button.disabled = stale || Boolean(retrieval && candidateCheckRunning(retrieval.id));
  });
}


const state = {
  view: 'check', viewRevision: 0, text: restoreDraft(), sources: [], status: null, model: restoreModel(), report: null,
  selectedMatch: null, rewrite: null, judgment: null, examples: [], run: null,
  retrievals: {}, retrievalJobs: restoreActiveJobs(RETRIEVAL_JOBS_KEY), candidateJudgments: {}, candidateRewrites: {},
  candidateErrors: {}, candidateLoaded: new Set(), candidateCheckJobs: restoreActiveJobs(CANDIDATE_JOBS_KEY),
};
const main = document.querySelector('#main-content');
const relationNames = { exact: 'Дословное совпадение', paraphrase: 'Пересказ', same_topic: 'Общая тема', unrelated: 'Разные тексты' };
const viewNames = { check: 'Проверка текста', sources: 'Источники', evaluation: 'Оценка Qwen', history: 'История' };
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
const number = value => Number(value || 0).toLocaleString('ru-RU');
const chars = value => Array.from(value);
function installedModels() {
  const embedding = state.status?.embedding?.model?.split(':')[0];
  return (state.status?.model?.models || [])
    .map(model => typeof model === 'string' ? model : model.name)
    .filter(name => !embedding || name.split(':')[0] !== embedding);

}

function selectedModel() {
  if (state.model) return state.model;
  const models = installedModels();
  const preferred = state.status?.model?.model;
  return models.includes(preferred) ? preferred : models[0] || preferred;
}

function modelAvailable() {
  return installedModels().includes(selectedModel());
}

function currentComparison(reportId, matchId) {
  return state.view === 'check' && state.report?.id === reportId && state.selectedMatch === matchId;
}
const date = value => value ? new Date(value).toLocaleString('ru-RU', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : '';

async function api(path, data, method) {
  const response = await fetch(path, {
    method: method || (data === undefined ? 'GET' : 'POST'),
    headers: data === undefined ? {} : { 'Content-Type': 'application/json' },
    body: data === undefined ? undefined : JSON.stringify(data),
  });
  let result;
  try { result = await response.json(); }
  catch { throw new Error('Сервер вернул неожиданный ответ. Проверьте, что локальная версия запущена.'); }
  if (!response.ok) throw new Error(result.error || result.message || 'Не удалось выполнить запрос.');
  return result;
}
let toastTimer;
function toast(message, error = false) {
  const element = document.querySelector('#toast');
  element.textContent = message;
  element.classList.toggle('error', error);
  element.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { element.hidden = true; }, error ? 9000 : 4500);
}
async function busy(button, label, action) {
  const original = button.innerHTML;
  button.disabled = true;
  button.innerHTML = `<span class="spinner" aria-hidden="true"></span>${escapeHTML(label)}`;
  try { await action(); }
  catch (error) { toast(error.message, true); }
  finally { if (button.isConnected) { button.disabled = false; button.innerHTML = original; } }
}
function safeLink(url, label) {
  try {
    const parsed = new URL(url);
    if (!['https:', 'http:'].includes(parsed.protocol)) return '';
    return `<a class="source-url" href="${escapeHTML(parsed.href)}" target="_blank" rel="noopener noreferrer">${escapeHTML(label || parsed.hostname)} ↗</a>`;
  } catch { return ''; }
}
function heading(title, description, action = '', eyebrow = '') {
  return `<div class="page-heading"><div>${eyebrow ? `<div class="eyebrow">${escapeHTML(eyebrow)}</div>` : ''}<h1>${escapeHTML(title)}</h1><p class="page-description">${escapeHTML(description)}</p></div>${action}</div>`;
}
function empty(title, description, symbol = '⌕') {
  return `<div class="empty-state"><span class="empty-symbol" aria-hidden="true">${symbol}</span><h3>${escapeHTML(title)}</h3><p>${escapeHTML(description)}</p></div>`;
}
function scopeAside() {
  return `
    <div class="side-stack">
      <section class="card side-card">
        <h2>Область проверки <span class="scope-badge">Локально</span></h2>
        <p>Сравниваем текст с материалами, которые вы добавили в библиотеку источников.</p>
        <div class="source-summary">
          <span>Источников в библиотеке</span><strong id="library-count">${state.sources.length}</strong>
        </div>
        <a class="side-link" href="#sources">Управлять источниками <span>↗</span></a>
      </section>
      <aside class="hint-card">
        <div class="hint-icon" aria-hidden="true">“</div>
        <h3>Как читать отчёт</h3>
        <p>Откройте совпадение, сравните фрагменты и проверьте, как автор указал источник.</p>
      </aside>
      <p class="coverage-note"><span aria-hidden="true">◌</span>
        <span>Поиск по открытому интернету ещё не подключён. В отчёте учитывается только ваша библиотека.</span>
      </p>
    </div>`;
}

function updateCharacterCount() {
  const count = document.querySelector('#character-count');
  if (count) count.textContent = `${number(chars(state.text).length)} знаков`;
  const wordCount = document.querySelector('#word-count');
  if (wordCount) wordCount.textContent = `${number(state.text.trim() ? state.text.trim().split(/\s+/u).length : 0)} слов`;
}
async function readTextFile(input, target) {
  const file = input.files[0];
  if (!file) return;
  if (!/\.(txt|md)$/i.test(file.name)) { toast('В этой версии доступны текстовые файлы .txt и .md.', true); input.value = ''; return; }
  if (file.size > 800000) { toast('Файл слишком большой. Используйте текст до 200 000 знаков.', true); input.value = ''; return; }
  const previousValue = target.value;
  const text = await file.text();
  input.value = '';
  if (!target.isConnected || target.value !== previousValue) return;
  if (chars(text).length > 200000) {
    throw new Error('В файле больше 200 000 знаков. Выберите более короткий текст.');
  }
  target.value = text;
  target.dispatchEvent(new Event('input', { bubbles: true }));
  input.value = '';
}
function renderCheck() {
  main.innerHTML = heading(
    'Проверьте свой текст.',
    'Найдите совпадения, сравните источники и доработайте текст.',
    '<button class="text-button" id="load-demo" type="button">Попробовать на примере <span aria-hidden="true">↗</span></button>',
    'Сравнение с вашей библиотекой'
  ) + `
    <div class="workspace-grid">
      <div>
        <section class="card editor-card">
          <div class="editor-header">
            <div class="editor-title"><span class="document-icon" aria-hidden="true">▤</span>Ваш текст</div>
            <label class="file-button">↑ Загрузить .txt
              <input id="text-file" type="file" accept=".txt,.md,text/plain,text/markdown" aria-label="Загрузить текстовый файл">
            </label>
          </div>
          <textarea class="editor-text" id="check-text" maxlength="200000" aria-label="Текст для проверки"
            placeholder="Вставьте текст, который хотите проверить…&#10;&#10;Добавьте источники в библиотеку или начните с готового примера."></textarea>
          <div class="editor-meta">
            <span><span id="character-count">0 знаков</span> · <span id="word-count">0 слов</span></span>
            <span>До 200 000 знаков</span>
          </div>
          <div class="editor-footer">
            <p id="draft-status">Черновик сохраняется на этом Mac</p>
            <button class="button" id="check-button" type="button">Проверить текст <span aria-hidden="true">↗</span></button>
          </div>
        </section>
        <div id="check-error" role="alert"></div>
        <section class="card direct-compare">
          <h2>Есть предполагаемый источник?</h2>
          <p>Попросите Qwen сравнить смысл вашего текста с конкретным материалом из библиотеки.
            До 12 000 знаков суммарно в двух текстах.</p>
          <div class="direct-compare-controls">
            <select id="compare-source" aria-label="Источник для сравнения">
              <option value="">Выберите источник</option>
              ${state.sources.map(source => `<option value="${escapeHTML(source.id)}">${escapeHTML(source.title)}</option>`).join('')}
            </select>
            <button class="button secondary small" id="compare-button" type="button" ${!modelAvailable() ? 'disabled' : ''}>Сравнить смысл</button>
          </div>
          <div id="direct-judgment"></div>
        </section>
      </div>
      ${scopeAside()}
    </div>
    <section class="report-section" id="report-area" aria-live="polite"></section>`;
  const textarea = document.querySelector('#check-text');
  textarea.value = state.text;
  textarea.addEventListener('input', () => {
    saveDraft(textarea.value);
    updateCharacterCount();
  });
  updateCharacterCount();
  updateDraftStatus();
  document.querySelector('#text-file').addEventListener('change', async event => {
    try { await readTextFile(event.target, textarea); } catch (error) { toast(error.message, true); }
  });
  document.querySelector('#load-demo').addEventListener('click', event => busy(event.currentTarget, 'Открываем пример', async () => {
    const revision = state.viewRevision;
    const originalText = state.text;
    const result = await api('/api/demo', {});
    await refreshSources();
    if (state.view !== 'check' || state.viewRevision !== revision || state.text !== originalText) return;
    saveDraft(result.text);
    state.report = null;
    state.selectedMatch = null;
    renderCheck();
    toast('Учебный текст и его источники готовы. Нажмите «Проверить текст».');
  }));
  document.querySelector('#check-button').addEventListener('click', event => busy(event.currentTarget, 'Проверяем текст', checkText));
  document.querySelector('#compare-source').addEventListener('change', () => {
    document.querySelector('#direct-judgment').replaceChildren();
  });
  document.querySelector('#compare-button').addEventListener('click', event => busy(event.currentTarget, 'Qwen сравнивает', async () => {
    if (!state.text.trim()) throw new Error('Вставьте текст для сравнения.');
    const sourceId = document.querySelector('#compare-source').value;
    if (!sourceId) throw new Error('Выберите источник из библиотеки.');
    const text = state.text;
    const revision = state.viewRevision;
    const resultArea = document.querySelector('#direct-judgment');
    const { judgment } = await api('/api/compare', { text, source_id: sourceId, model: selectedModel() });
    if (!resultArea.isConnected || state.viewRevision !== revision || state.text !== text
      || document.querySelector('#compare-source')?.value !== sourceId) return;
    resultArea.innerHTML = judgmentHTML(judgment);
  }));
  renderReport();
}
async function checkText() {
  if (!state.text.trim()) throw new Error('Вставьте текст для проверки.');
  if (!state.sources.length) throw new Error('Сначала добавьте хотя бы один источник или откройте готовый пример.');
  const text = state.text;
  const { report } = await api('/api/check', { text });
  if (state.text !== text) {
    toast('Отчёт для предыдущего варианта сохранён в истории. Проверьте обновлённый текст.');
    return;
  }
  state.report = report;
  state.selectedMatch = report.matches[0]?.id ?? null;
  state.rewrite = null;
  state.judgment = null;
  if (state.view !== 'check') return;
  renderReport();
  document.querySelector('#report-area')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}
function annotatedText(report) {
  const text = chars(report.text);
  const boundaries = new Set([0, text.length]);
  for (const match of report.matches) { boundaries.add(match.start); boundaries.add(match.end); }
  const ordered = [...boundaries].sort((a, b) => a - b);
  const output = [];
  for (let index = 0; index < ordered.length - 1; index++) {
    const start = ordered[index], end = ordered[index + 1];
    const covering = report.matches.filter(match => match.start < end && match.end > start);
    const value = escapeHTML(text.slice(start, end).join(''));
    const selected = covering.find(match => match.id === state.selectedMatch);
    const match = selected || covering[0];
    output.push(match ? `<mark tabindex="0" role="button" aria-label="Показать совпадение с ${escapeHTML(match.source_title)}" data-highlight="${escapeHTML(match.id)}" class="${selected ? 'selected' : ''} ${match.kind === 'similar' ? 'fuzzy' : ''}">${value}</mark>` : value);
  }
  return output.join('');
}
function matchCardHTML(match, index) {
  const selected = match.id === state.selectedMatch;
  const label = reviewLabel(match.id);
  return `
    <button class="match-item ${selected ? 'selected' : ''}" data-match="${escapeHTML(match.id)}" type="button">
      <div class="match-kicker">
        <span>Фрагмент ${index + 1}</span>
        <span class="match-kind ${match.kind}">${match.kind === 'exact' ? 'Дословно' : 'Похожая формулировка'}</span>
      </div>
      <strong>${escapeHTML(match.source_title)}</strong>
      <p>${escapeHTML(match.excerpt.slice(0, 120))}${match.excerpt.length > 120 ? '…' : ''}</p>
      ${match.quoted ? '<div class="subtle">Есть признаки оформления цитаты</div>' : ''}
      ${label ? `<div class="finding-badge ${state.report.reviews[match.id].decision}">${escapeHTML(label)}</div>` : ''}
    </button>`;
}

function renderReport() {
  const area = document.querySelector('#report-area');
  if (!area) return;
  if (!state.report) {
    area.innerHTML = empty('Здесь появится отчёт', 'После проверки можно открыть каждое совпадение, увидеть источник и предложить правку.');
    return;
  }
  const report = state.report;
  const stats = report.stats;
  const matches = report.matches;
  const selected = matches.find(match => match.id === state.selectedMatch);
  area.innerHTML = `
    <div id="stale-report" class="warning" hidden>
      Текст в редакторе изменён. Этот отчёт относится к предыдущему варианту.
      Повторите проверку перед применением правок.
      <button class="text-button" id="restore-report-text" type="button">Открыть текст отчёта в редакторе</button>
    </div>
    <div class="section-heading">
      <div><h2>Фрагменты для сверки</h2><p>По локальной библиотеке · ${escapeHTML(date(report.created_at))}</p></div>
      <button class="text-button" id="download-report" type="button">↓ Сохранить отчёт</button>
    </div>
    <div class="stats-row">
      <div class="stat-card">
        <strong>${Number(stats.similarity_percent || 0).toLocaleString('ru-RU', { maximumFractionDigits: 1 })}%</strong>
        <span>текста с лексическими совпадениями</span>
      </div>
      <div class="stat-card"><strong>${number(matches.length)}</strong><span>найдено фрагментов</span></div>
      <div class="stat-card"><strong>${number(stats.source_count)}</strong><span>источников с совпадениями</span></div>
    </div>
    ${(report.issues || []).map(issue => `<div class="warning">${escapeHTML(typeof issue === 'string' ? issue : issue.message || JSON.stringify(issue))}</div>`).join('')}
    ${matches.length ? `
      <div class="report-grid">
        <div class="card">
          <div class="editor-header">
            <span class="editor-title">Совпадения в тексте</span><span class="mini-label">Нажмите на подсветку</span>
          </div>
          <div class="annotated-text">${annotatedText(report)}</div>
          <div class="report-caption">Перекрывающиеся участки учитываются в проценте один раз. Оформленные цитаты также могут совпадать.</div>
        </div>
        <div class="match-list">${matches.map(matchCardHTML).join('')}</div>
      </div>` : empty(
        'Лексических совпадений в библиотеке не найдено',
        'Глубокие пересказы могут остаться незамеченными. Для сравнения с конкретным источником используйте Qwen.',
        '⌕'
      )}
    <div id="comparison-area">${selected ? comparisonHTML(selected) : ''}</div>
    <section class="retrieval-section" id="retrieval-area"></section>`;
  area.querySelectorAll('[data-match], [data-highlight]').forEach(element => {
    const select = () => {
      state.selectedMatch = element.dataset.match || element.dataset.highlight;
      state.rewrite = null;
      state.judgment = null;
      renderReport();
    };
    element.addEventListener('click', select);
    if (element.tagName === 'MARK') {
      element.addEventListener('keydown', event => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          select();
        }
      });
    }
  });
  document.querySelector('#download-report').addEventListener('click', () => {
    download(`istok-report-${report.id}.json`, JSON.stringify(report, null, 2), 'application/json');
  });
  document.querySelector('#restore-report-text').addEventListener('click', () => {
    saveDraft(report.text);
    document.querySelector('#check-text').value = state.text;
    updateCharacterCount();
    toast('Текст сохранённого отчёта открыт в редакторе.');
  });
  bindComparison(selected);
  updateReportFreshness();
  renderRetrieval();
  loadRetrieval(report.id);
}

async function loadRetrieval(reportId) {
  if (Object.hasOwn(state.retrievals, reportId)) return;
  state.retrievals[reportId] = null;
  try {
    const { retrieval } = await api(`/api/retrievals/${encodeURIComponent(reportId)}`);
    if (state.retrievals[reportId] === null) state.retrievals[reportId] = retrieval;
    if (state.view === 'check' && state.report?.id === reportId) renderRetrieval();
  } catch (error) {
    delete state.retrievals[reportId];
    if (state.view === 'check' && state.report?.id === reportId) toast(error.message, true);
  }
}

function retrievalRunning(reportId) {
  return ACTIVE_JOB_STATUSES.includes(state.retrievalJobs[reportId]?.status);
}

function candidateKey(retrieval, candidate) {
  return `${retrieval.id}:${candidate.id}`;
}

function candidateActionsHTML(candidate, retrieval) {
  const judgment = state.candidateJudgments[candidateKey(retrieval, candidate)];
  const positive = judgment?.id && ['exact', 'paraphrase'].includes(judgment.relation);
  const running = candidateCheckRunning(retrieval.id);
  return `
    <button class="button small secondary" data-candidate-action="judge" type="button" ${modelAvailable() && !running ? '' : 'disabled'}>Сравнить с Qwen</button>
    ${positive ? `
      <button class="text-button" data-candidate-action="quote" type="button" ${running ? 'disabled' : ''}>Оформить цитату</button>
      <button class="text-button" data-candidate-action="paraphrase" type="button" ${modelAvailable() && !running ? '' : 'disabled'}>Предложить пересказ</button>
    ` : ''}`;
}

function candidateHTML(candidate, retrieval) {
  const judgment = state.candidateJudgments[candidateKey(retrieval, candidate)];
  const rewrite = state.candidateRewrites[candidateKey(retrieval, candidate)];
  return `
    <article class="candidate-card" data-candidate="${escapeHTML(candidate.id)}" data-retrieval="${escapeHTML(retrieval.id)}">
      <div class="candidate-heading">
        <h3>${escapeHTML(candidate.source_title)}</h3><span class="subtle">Место ${candidate.rank}</span>
      </div>
      <blockquote>${escapeHTML(candidate.source_excerpt)}</blockquote>
      ${safeLink(candidate.source_url, 'Открыть источник')}
      <div class="action-row candidate-actions">${candidateActionsHTML(candidate, retrieval)}</div>
      ${judgment ? '' : '<p class="field-help">Сначала сравните пару с Qwen. После подтверждённого совпадения можно подготовить правку конкретного фрагмента.</p>'}
      <div class="candidate-error">${state.candidateErrors[candidateKey(retrieval, candidate)] ? `<div class="inline-error">${escapeHTML(state.candidateErrors[candidateKey(retrieval, candidate)])}</div>` : ''}</div>
      <div class="candidate-judgment">${judgment ? judgmentHTML(judgment) : ''}</div>
      <div class="candidate-rewrite">${rewrite ? candidateRewriteHTML(candidate, retrieval) : ''}</div>
    </article>`;
}

function candidateRewriteHTML(candidate, retrieval) {
  const rewrite = state.candidateRewrites[candidateKey(retrieval, candidate)];
  return `
    <div class="rewrite-box">
      <strong>Предложение правки</strong>
      <p class="field-help">Будет заменена только выдержка, показанная ниже. Проверьте смысл и источник.</p>
      <blockquote class="replacement-original">${escapeHTML(rewrite.original_text)}</blockquote>
      <label class="field-label">Предлагаемая замена</label>
      <textarea class="candidate-rewrite-text" aria-label="Правка для фрагмента из ${escapeHTML(candidate.source_title)}">${escapeHTML(rewrite.text)}</textarea>
      <div class="action-row">
        <button class="button small" data-apply-candidate="${escapeHTML(candidate.id)}" type="button"
          ${state.text === state.report?.text && !candidateCheckRunning(retrieval.id) ? '' : 'disabled'}>Применить в редакторе</button>
        <button class="text-button" data-discard-candidate="${escapeHTML(candidate.id)}" type="button">Отменить</button>
      </div>
    </div>`;
}

function renderRetrieval() {
  const area = document.querySelector('#retrieval-area');
  if (!area || !state.report) return;
  const reportId = state.report.id;
  const retrieval = state.retrievals[reportId];
  const job = state.retrievalJobs[reportId];
  const running = retrievalRunning(reportId);
  const embeddingAvailable = Boolean(state.status?.embedding?.available);
  const groups = new Map();
  for (const candidate of retrieval?.candidates || []) {
    if (!groups.has(candidate.query_index)) groups.set(candidate.query_index, []);
    groups.get(candidate.query_index).push(candidate);
  }
  area.innerHTML = `
    <div class="section-heading">
      <div>
        <h2>Источники для проверки пересказов</h2>
        <p>Ищем похожие фрагменты внутри библиотеки. Каждый кандидат требует сверки.</p>
      </div>
    </div>
    <div class="card retrieval-controls">
      <p>Поиск по смыслу дополняет поиск по словам. Список кандидатов сохраняется отдельно от процента лексических совпадений.</p>
      <div class="action-row">
        <button class="button small" id="retrieve-hybrid" type="button" ${running || !embeddingAvailable ? 'disabled' : ''}>Поиск пересказов</button>
        <button class="button secondary small" id="retrieve-lexical" type="button" ${running ? 'disabled' : ''}>Поиск по словам</button>
      </div>
      <p class="field-help" id="embedding-unavailable" ${embeddingAvailable ? 'hidden' : ''}>Модель поиска по смыслу BGE-M3 пока недоступна. Поиск по словам работает самостоятельно.</p>
      ${running ? `
        <div class="job-status" role="status"><span class="spinner" aria-hidden="true"></span>
          <span id="retrieval-job-label">${escapeHTML(job.stage || 'Ищем фрагменты')}${job.progress?.total ? ` · ${number(job.progress.current)} из ${number(job.progress.total)}` : ''}</span>
        </div>
        <p class="field-help">Можно перейти на другой экран. Поиск продолжится.</p>
        <button class="text-button" id="cancel-retrieval" type="button" ${!job.id || job.status === 'cancelling' ? 'disabled' : ''}>${job.status === 'cancelling' ? 'Останавливаем после текущего шага' : 'Остановить поиск'}</button>` : ''}
      ${job?.status === 'cancelled' ? '<p class="field-help">Поиск остановлен. Можно запустить его снова.</p>' : ''}
      ${job?.status === 'failed' ? `<div class="inline-error">${escapeHTML(job.error || 'Не удалось завершить поиск.')}</div>` : ''}
    </div>
    ${retrieval ? `
      <p class="dataset-note">
        ${retrieval.mode === 'hybrid' ? 'Поиск по словам и смыслу' : 'Поиск по словам'} ·
        ${number(retrieval.stats?.candidate_count ?? retrieval.candidates.length)} кандидатов ·
        ${number(retrieval.stats?.query_chunks)} фрагментов статьи ·
        ${number(retrieval.stats?.source_chunks)} фрагментов библиотеки
      </p>
      <div id="candidate-check-area"></div>
      ${(retrieval.warnings || []).map(warning => `<div class="warning">${escapeHTML(typeof warning === 'string' ? warning : warning.message || JSON.stringify(warning))}</div>`).join('')}
      ${[...groups.values()].map(candidates => `
        <details class="card candidate-group">
          <summary><span>${escapeHTML(candidates[0].query_excerpt.slice(0, 135))}${candidates[0].query_excerpt.length > 135 ? '…' : ''}</span><small>${candidates.length} кандидатов</small></summary>
          <div class="candidate-group-body">
            <h3>Фрагмент вашей статьи</h3><blockquote class="candidate-query">${escapeHTML(candidates[0].query_excerpt)}</blockquote>
            ${candidates.map(candidate => candidateHTML(candidate, retrieval)).join('')}
          </div>
        </details>`).join('')}
      ${retrieval.candidates.length ? '' : empty('Кандидаты не найдены', 'Попробуйте добавить больше подходящих источников в библиотеку.', '⌕')}
    ` : ''}`;
  document.querySelector('#retrieve-hybrid').addEventListener('click', () => startRetrieval(reportId, 'hybrid'));
  document.querySelector('#retrieve-lexical').addEventListener('click', () => startRetrieval(reportId, 'lexical'));
  const cancel = document.querySelector('#cancel-retrieval');
  if (cancel) cancel.addEventListener('click', event => busy(event.currentTarget, 'Останавливаем', async () => {
    const { job: cancelled } = await api(`/api/jobs/${encodeURIComponent(job.id)}/cancel`, {});
    state.retrievalJobs[reportId] = cancelled;
    saveActiveJobs(RETRIEVAL_JOBS_KEY, state.retrievalJobs);
    renderRetrieval();
  }));
  if (retrieval) {
    bindCandidateActions(retrieval);
    renderCandidateCheck(retrieval);
    loadCandidateJudgments(retrieval.id);
  }
}

function updateRetrievalJobStatus(reportId) {
  if (state.view !== 'check' || state.report?.id !== reportId) return;
  const label = document.querySelector('#retrieval-job-label');
  const job = state.retrievalJobs[reportId];
  if (!label || !job) return;
  const progress = job.progress?.total ? ` · ${number(job.progress.current)} из ${number(job.progress.total)}` : '';
  label.textContent = (job.stage || 'Ищем фрагменты') + progress;
}

async function pollRetrieval(reportId, jobId) {
  if (retrievalPolls.has(reportId)) return;
  retrievalPolls.add(reportId);
  try {
    while (retrievalRunning(reportId)) {
      updateRetrievalJobStatus(reportId);
      await new Promise(resolve => setTimeout(resolve, 1000));
      const { job } = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
      state.retrievalJobs[reportId] = job;
      saveActiveJobs(RETRIEVAL_JOBS_KEY, state.retrievalJobs);
    }
    const finished = state.retrievalJobs[reportId];
    if (finished.status === 'complete') {
      state.retrievals[reportId] = finished.result;
      toast('Поиск источников завершён. Кандидаты готовы для сверки.');
    }
  } catch (error) {
    state.retrievalJobs[reportId] = { status: 'failed', error: error.message };
  } finally {
    retrievalPolls.delete(reportId);
    saveActiveJobs(RETRIEVAL_JOBS_KEY, state.retrievalJobs);
  }
  if (state.view === 'check' && state.report?.id === reportId) renderRetrieval();
}

async function startRetrieval(reportId, mode) {
  if (retrievalRunning(reportId)) return;
  state.retrievalJobs[reportId] = { status: 'queued', stage: 'Подготавливаем поиск' };
  renderRetrieval();
  try {
    const { job } = await api('/api/retrieval-jobs', { report_id: reportId, mode, top_k: 5 });
    state.retrievalJobs[reportId] = job;
    saveActiveJobs(RETRIEVAL_JOBS_KEY, state.retrievalJobs);
    if (state.view === 'check' && state.report?.id === reportId) renderRetrieval();
    await pollRetrieval(reportId, job.id);
  } catch (error) {
    state.retrievalJobs[reportId] = { status: 'failed', error: error.message };
    saveActiveJobs(RETRIEVAL_JOBS_KEY, state.retrievalJobs);
    if (state.view === 'check' && state.report?.id === reportId) renderRetrieval();
  }
}

function mergeCandidateOutcomes(retrievalId, entries) {
  for (const item of entries) {
    const key = `${retrievalId}:${item.candidate_id}`;
    if (item.judgment) {
      const previous = state.candidateJudgments[key];
      if (previous?.id !== item.judgment.id) delete state.candidateRewrites[key];
      state.candidateJudgments[key] = item.judgment;
      delete state.candidateErrors[key];
    } else if (item.error) {
      state.candidateErrors[key] = item.error;
      delete state.candidateJudgments[key];
      delete state.candidateRewrites[key];
    }
  }
}

async function loadCandidateJudgments(retrievalId, force = false) {
  if (!force && state.candidateLoaded.has(retrievalId)) return;
  state.candidateLoaded.add(retrievalId);
  try {
    const { judgments } = await api(`/api/candidate-judgments/${encodeURIComponent(retrievalId)}`);
    mergeCandidateOutcomes(retrievalId, judgments);
    const retrieval = state.retrievals[state.report?.id];
    if (state.view === 'check' && retrieval?.id === retrievalId) {
      updateCandidateCards(retrieval);
      renderCandidateCheck(retrieval);
    }
  } catch (error) {
    state.candidateLoaded.delete(retrievalId);
    toast(error.message, true);
  }
}

function updateCandidateCards(retrieval) {
  document.querySelectorAll('[data-candidate]').forEach(card => {
    if (card.dataset.retrieval !== retrieval.id) return;
    const candidate = retrieval.candidates.find(item => item.id === card.dataset.candidate);
    const key = candidateKey(retrieval, candidate);
    if (candidateRequests.has(key)) return;
    const judgment = state.candidateJudgments[key];
    const error = state.candidateErrors[key];
    card.querySelector('.candidate-judgment').innerHTML = judgment ? judgmentHTML(judgment) : '';
    card.querySelector('.candidate-error').innerHTML = error ? `<div class="inline-error">${escapeHTML(error)}</div>` : '';
    if (!state.candidateRewrites[key]) card.querySelector('.candidate-rewrite').replaceChildren();
    card.querySelector('.candidate-actions').innerHTML = candidateActionsHTML(candidate, retrieval);
    bindCandidateCardActions(card, candidate, retrieval);
  });
}

function candidateCheckRunning(retrievalId) {
  return ACTIVE_JOB_STATUSES.includes(state.candidateCheckJobs[retrievalId]?.status);
}

function renderCandidateCheck(retrieval) {
  const area = document.querySelector('#candidate-check-area');
  if (!area) return;
  const job = state.candidateCheckJobs[retrieval.id];
  const running = candidateCheckRunning(retrieval.id);
  const counts = Object.fromEntries(Object.keys(relationNames).map(relation => [relation, 0]));
  let errors = 0;
  for (const candidate of retrieval.candidates) {
    const key = candidateKey(retrieval, candidate);
    const relation = state.candidateJudgments[key]?.relation;
    if (Object.hasOwn(counts, relation)) counts[relation]++;
    if (state.candidateErrors[key]) errors++;
  }
  area.innerHTML = `
    <section class="card candidate-check-controls">
      <h3>Сравнить кандидатов с Qwen</h3>
      <p>${number(retrieval.candidates.length)} пар фрагментов. Модель проверит их последовательно и сохранит каждый ответ.</p>
      <div class="action-row">
        <button class="button small" id="check-all-candidates" type="button" ${running || !modelAvailable() || !retrieval.candidates.length ? 'disabled' : ''}>Проверить кандидатов с Qwen</button>
        ${running ? `<button class="text-button" id="cancel-candidate-check" type="button" ${!job.id || job.status === 'cancelling' ? 'disabled' : ''}>${job.status === 'cancelling' ? 'Останавливаем после текущего ответа' : 'Остановить'}</button>` : ''}
      </div>
      <p class="field-help">${running ? 'Можно перейти на другой экран. При остановке дождёмся текущего ответа модели.' : `Выбрана модель ${escapeHTML(selectedModel() || 'не подключена')}.`}</p>
      ${running ? `<div class="job-status" role="status"><span class="spinner" aria-hidden="true"></span><span id="candidate-check-progress">${escapeHTML(job.stage || 'Подготавливаем сравнение')}${job.progress?.total ? ` · ${number(job.progress.current)} из ${number(job.progress.total)}` : ''}</span></div>` : ''}
      ${job?.status === 'cancelled' ? '<p class="field-help">Проверка остановлена. Завершённые ответы сохранены.</p>' : ''}
      ${job?.status === 'failed' ? `<div class="inline-error">${escapeHTML(job.error || 'Не удалось закончить сравнение.')}</div>` : ''}
      <div class="candidate-verdict-counts">
        ${Object.entries(relationNames).map(([relation, label]) => `<span>${escapeHTML(label)} <strong>${number(counts[relation])}</strong></span>`).join('')}
        <span class="${errors ? 'result-fail' : ''}">Ошибки запросов <strong>${number(errors)}</strong></span>
      </div>
    </section>`;
  document.querySelector('#check-all-candidates').addEventListener('click', () => startCandidateCheck(retrieval));
  const cancel = document.querySelector('#cancel-candidate-check');
  if (cancel) cancel.addEventListener('click', event => busy(event.currentTarget, 'Останавливаем', async () => {
    const { job: cancelled } = await api(`/api/jobs/${encodeURIComponent(job.id)}/cancel`, {});
    state.candidateCheckJobs[retrieval.id] = cancelled;
    saveActiveJobs(CANDIDATE_JOBS_KEY, state.candidateCheckJobs);
    renderCandidateCheck(retrieval);
  }));
}

function updateCandidateCheckProgress(retrievalId) {
  const retrieval = state.retrievals[state.report?.id];
  if (state.view !== 'check' || retrieval?.id !== retrievalId) return;
  const label = document.querySelector('#candidate-check-progress');
  const job = state.candidateCheckJobs[retrievalId];
  if (label && job) {
    const progress = job.progress?.total ? ` · ${number(job.progress.current)} из ${number(job.progress.total)}` : '';
    label.textContent = (job.stage || 'Qwen сравнивает фрагменты') + progress;
  }
}

async function pollCandidateCheck(retrievalId, jobId) {
  let polls = 0;
  if (candidateJobPolls.has(retrievalId)) return;
  candidateJobPolls.add(retrievalId);
  try {
    while (candidateCheckRunning(retrievalId)) {
      updateCandidateCheckProgress(retrievalId);
      await new Promise(resolve => setTimeout(resolve, 1000));
      const { job } = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
      state.candidateCheckJobs[retrievalId] = job;
      saveActiveJobs(CANDIDATE_JOBS_KEY, state.candidateCheckJobs);
      polls++;
      if (polls % 3 === 0) await loadCandidateJudgments(retrievalId, true);
    }
    const finished = state.candidateCheckJobs[retrievalId];
    if (finished.result?.judgments) mergeCandidateOutcomes(retrievalId, finished.result.judgments);
    await loadCandidateJudgments(retrievalId, true);
    if (finished.status === 'complete') toast('Сравнение кандидатов завершено. Ответы и ошибки сохранены.');
  } catch (error) {
    state.candidateCheckJobs[retrievalId] = { status: 'failed', error: error.message };
  } finally {
    candidateJobPolls.delete(retrievalId);
    saveActiveJobs(CANDIDATE_JOBS_KEY, state.candidateCheckJobs);
  }
  const retrieval = state.retrievals[state.report?.id];
  if (state.view === 'check' && retrieval?.id === retrievalId) {
    renderCandidateCheck(retrieval);
    updateCandidateCards(retrieval);
    updateReportFreshness();
  }
}

async function startCandidateCheck(retrieval) {
  if (candidateCheckRunning(retrieval.id)) return;
  const model = selectedModel();
  if (!modelAvailable()) { toast('Выберите доступную локальную модель.', true); return; }
  state.candidateCheckJobs[retrieval.id] = { status: 'queued', stage: 'Подготавливаем сравнение' };
  renderCandidateCheck(retrieval);
  updateCandidateCards(retrieval);
  updateReportFreshness();
  try {
    const { job } = await api('/api/candidate-check-jobs', { retrieval_id: retrieval.id, model });
    state.candidateCheckJobs[retrieval.id] = job;
    saveActiveJobs(CANDIDATE_JOBS_KEY, state.candidateCheckJobs);
    if (state.view === 'check' && state.retrievals[state.report?.id]?.id === retrieval.id) renderCandidateCheck(retrieval);
    await pollCandidateCheck(retrieval.id, job.id);
  } catch (error) {
    state.candidateCheckJobs[retrieval.id] = { status: 'failed', error: error.message };
    saveActiveJobs(CANDIDATE_JOBS_KEY, state.candidateCheckJobs);
    const current = state.retrievals[state.report?.id];
    if (state.view === 'check' && current?.id === retrieval.id) renderCandidateCheck(retrieval);
  }
}

function bindCandidateActions(retrieval) {
  document.querySelectorAll('[data-candidate]').forEach(card => {
    const candidate = retrieval.candidates.find(item => item.id === card.dataset.candidate);
    bindCandidateCardActions(card, candidate, retrieval);
    if (state.candidateRewrites[candidateKey(retrieval, candidate)]) bindCandidateRewrite(card, candidate, retrieval);
  });
}

function bindCandidateCardActions(card, candidate, retrieval) {
  const key = candidateKey(retrieval, candidate);
  card.querySelectorAll('[data-candidate-action]').forEach(button => {
    button.addEventListener('click', () => busy(button, 'Готовим ответ', async () => {
      if (candidateRequests.has(key)) return;
      candidateRequests.add(key);
      card.querySelectorAll('[data-candidate-action]').forEach(control => { control.disabled = true; });
      const action = button.dataset.candidateAction;
      try {
        const payload = { retrieval_id: retrieval.id, candidate_id: candidate.id, model: selectedModel() };
        if (action === 'judge') {
          const { judgment } = await api('/api/candidate-judge', payload);
          state.candidateJudgments[key] = judgment;
          delete state.candidateErrors[key];
          delete state.candidateRewrites[key];
          if (card.isConnected) {
            card.querySelector('.candidate-judgment').innerHTML = judgmentHTML(judgment);
            card.querySelector('.candidate-rewrite').replaceChildren();
            card.querySelector('.candidate-error').replaceChildren();
          }
          if (state.view === 'check' && state.retrievals[state.report?.id]?.id === retrieval.id) renderCandidateCheck(retrieval);
        } else {
          const judgment = state.candidateJudgments[key];
          if (!judgment?.id || !['exact', 'paraphrase'].includes(judgment.relation)) {
            throw new Error('Сначала подтвердите совпадение сравнением с Qwen.');
          }
          const { rewrite } = await api('/api/candidate-rewrite', {
            ...payload, judgment_id: judgment.id, mode: action,
          });
          state.candidateRewrites[key] = rewrite;
          if (card.isConnected && state.report?.id === retrieval.report_id) {
            card.querySelector('.candidate-rewrite').innerHTML = candidateRewriteHTML(candidate, retrieval);
            bindCandidateRewrite(card, candidate, retrieval);
          }
        }
      } catch (error) {
        if (action === 'judge') {
          mergeCandidateOutcomes(retrieval.id, [{ candidate_id: candidate.id, error: error.message }]);
          if (card.isConnected) {
            card.querySelector('.candidate-judgment').replaceChildren();
            card.querySelector('.candidate-rewrite').replaceChildren();
            card.querySelector('.candidate-error').innerHTML = `<div class="inline-error">${escapeHTML(error.message)}</div>`;
          }
          if (state.view === 'check' && state.retrievals[state.report?.id]?.id === retrieval.id) renderCandidateCheck(retrieval);
        }
        throw error;
      } finally {
        candidateRequests.delete(key);
        if (card.isConnected) {
          card.querySelector('.candidate-actions').innerHTML = candidateActionsHTML(candidate, retrieval);
          bindCandidateCardActions(card, candidate, retrieval);
        }
      }
    }));
  });
}

function bindCandidateRewrite(card, candidate, retrieval) {
  const field = card.querySelector('.candidate-rewrite-text');
  field.addEventListener('input', () => { state.candidateRewrites[candidateKey(retrieval, candidate)].text = field.value; });
  card.querySelector('[data-discard-candidate]').addEventListener('click', () => {
    delete state.candidateRewrites[candidateKey(retrieval, candidate)];
    card.querySelector('.candidate-rewrite').replaceChildren();
  });
  card.querySelector('[data-apply-candidate]').addEventListener('click', () => {
    if (state.report?.id !== retrieval.report_id || state.text !== state.report.text
      || state.retrievals[retrieval.report_id]?.id !== retrieval.id || candidateCheckRunning(retrieval.id)) {
      toast('Текст изменён. Повторите проверку перед применением правки.', true);
      return;
    }
    const rewrite = state.candidateRewrites[candidateKey(retrieval, candidate)];
    const text = chars(state.text);
    const start = rewrite.replacement_start;
    const end = rewrite.replacement_end;
    if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end <= start
      || text.slice(start, end).join('') !== rewrite.original_text) {
      toast('Не удалось сопоставить фрагмент с текстом отчёта. Повторите поиск.', true);
      return;
    }
    saveDraft(text.slice(0, start).join('') + field.value + text.slice(end).join(''));
    state.report = null;
    state.rewrite = null;
    renderCheck();
    document.querySelector('#check-text').focus();
    toast('Правка применена к фрагменту. Проверьте обновлённый текст.');
  });
}

function reviewLabel(matchId) {
  const decision = state.report?.reviews?.[matchId]?.decision;
  if (decision === 'confirmed') return 'Подтверждено редактором';
  if (decision === 'dismissed') return 'Отклонено редактором';
  return '';
}

function matchReviewHTML(match) {
  const review = state.report?.reviews?.[match.id];
  return `
    <div class="finding-review">
      <h3>Решение редактора</h3>
      <p class="field-help">${escapeHTML(reviewLabel(match.id) || 'Сравните фрагменты и сохраните своё решение.')} Доля совпадений отражает исходный результат поиска.</p>
      <input class="field" id="finding-comment" maxlength="2000" value="${escapeHTML(review?.comment || '')}" aria-label="Комментарий к находке" placeholder="Комментарий · необязательно">
      <div class="action-row">
        <button class="button small secondary" data-finding-decision="confirmed" type="button">Подтвердить</button>
        <button class="button small secondary" data-finding-decision="dismissed" type="button">Отклонить находку</button>
        ${review && review.decision !== 'pending' ? '<button class="text-button" data-finding-decision="pending" type="button">Вернуть на проверку</button>' : ''}
      </div>
    </div>`;
}

function comparisonHTML(match) {
  const available = modelAvailable();
  return `
    <section class="card comparison-card">
      <div class="section-heading">
        <h2>Сравнение фрагментов</h2>
        <span class="subtle">${match.kind === 'exact' ? 'Дословное совпадение' : 'Лексическое сходство'}</span>
      </div>
      <div class="comparison-columns">
        <div><h3>ВАШ ТЕКСТ</h3><blockquote>${escapeHTML(match.excerpt)}</blockquote></div>
        <div>
          <h3>${escapeHTML(match.source_title)}</h3>
          <blockquote>${escapeHTML(match.source_excerpt)}</blockquote>
          ${safeLink(match.source_url, 'Открыть источник')}
        </div>
      </div>
      <div class="action-row">
        <button class="button small secondary" id="quote-button" type="button">Оформить цитату</button>
        <button class="button small secondary" id="paraphrase-button" type="button" ${available ? '' : 'disabled'}>Предложить пересказ</button>
        <button class="text-button" id="judge-button" type="button" ${available ? '' : 'disabled'}>Оценить с Qwen ↗</button>
      </div>
      ${available ? '' : '<p class="field-help">Для пересказа и оценки подключите Qwen в меню модели сверху.</p>'}
      ${matchReviewHTML(match)}
      <div id="judgment-area">${state.judgment ? judgmentHTML(state.judgment) : ''}</div>
      <div id="rewrite-area">${state.rewrite ? rewriteHTML() : ''}</div>
    </section>`;
}

function judgmentHTML(judgment) {
  return `<div class="judgment"><strong>${escapeHTML(relationNames[judgment.relation] || judgment.relation)}</strong><p>${escapeHTML(judgment.explanation)}</p>${judgment.evidence_text && judgment.evidence_source ? `<div class="comparison-columns"><div><h3>Фрагмент вашего текста</h3><blockquote>${escapeHTML(judgment.evidence_text)}</blockquote></div><div><h3>Фрагмент источника</h3><blockquote>${escapeHTML(judgment.evidence_source)}</blockquote></div></div>` : ''}<p class="field-help">Оценка модели ${escapeHTML(judgment.model || selectedModel())}. Сопоставьте вывод с фрагментами выше.</p></div>`;
}
function rewriteHTML() {
  return `<div class="rewrite-box"><strong>Предложение правки</strong><p class="field-help">Проверьте смысл, числа и ссылку перед применением.</p><textarea id="rewrite-text" aria-label="Предложенная правка"></textarea><div class="action-row"><button class="button small" id="apply-rewrite" type="button">Применить в редакторе</button><button class="text-button" id="discard-rewrite" type="button">Отменить</button></div></div>`;
}
function bindComparison(match) {
  if (!match) return;
  const reportId = state.report.id;
  document.querySelectorAll('[data-finding-decision]').forEach(button => {
    button.addEventListener('click', () => busy(button, 'Сохраняем решение', async () => {
      const reviewSection = button.closest('.finding-review');
      reviewSection.querySelectorAll('button').forEach(control => { control.disabled = true; });
      const comment = document.querySelector('#finding-comment').value.trim();
      let review;
      try {
        ({ review } = await api('/api/report-reviews', {
          report_id: reportId,
          match_id: match.id,
          decision: button.dataset.findingDecision,
          comment,
        }));
      } finally {
        if (reviewSection.isConnected) {
          reviewSection.querySelectorAll('button').forEach(control => { control.disabled = false; });
        }
      }
      if (state.report?.id !== reportId) return;
      state.report.reviews = { ...state.report.reviews, [match.id]: review };
      if (currentComparison(reportId, match.id)) renderReport();
      toast('Решение по находке сохранено.');
    }));
  });
  for (const [id, mode] of [['quote-button', 'quote'], ['paraphrase-button', 'paraphrase']]) {
    document.querySelector(`#${id}`).addEventListener('click', event => busy(event.currentTarget, 'Готовим правку', async () => {
      const resultArea = document.querySelector('#rewrite-area');
      const { rewrite } = await api('/api/rewrite', { report_id: reportId, match_id: match.id, mode, model: selectedModel() });
      if (!resultArea.isConnected || !currentComparison(reportId, match.id)) return;
      state.rewrite = rewrite;
      document.querySelector('#rewrite-area').innerHTML = rewriteHTML();
      bindRewrite(match);
    }));
  }
  document.querySelector('#judge-button').addEventListener('click', event => busy(event.currentTarget, 'Qwen сравнивает', async () => {
    const resultArea = document.querySelector('#judgment-area');
    const { judgment } = await api('/api/judge', { report_id: reportId, match_id: match.id, model: selectedModel() });
    if (!resultArea.isConnected || !currentComparison(reportId, match.id)) return;
    state.judgment = judgment;
    document.querySelector('#judgment-area').innerHTML = judgmentHTML(judgment);
  }));
  if (state.rewrite) bindRewrite(match);
}
function bindRewrite(match) {
  const reportId = state.report.id;
  const field = document.querySelector('#rewrite-text');
  field.value = state.rewrite.text;
  field.addEventListener('input', () => { state.rewrite.text = field.value; });
  document.querySelector('#discard-rewrite').addEventListener('click', () => { state.rewrite = null; document.querySelector('#rewrite-area').replaceChildren(); });
  document.querySelector('#apply-rewrite').addEventListener('click', () => {
    if (!currentComparison(reportId, match.id)) return;
    if (state.text !== state.report.text) { toast('Текст в редакторе уже изменился. Повторите проверку перед применением правки.', true); return; }
    const text = chars(state.text);
    saveDraft(text.slice(0, match.start).join('') + field.value + text.slice(match.end).join(''));
    state.report = null;
    state.rewrite = null;
    renderCheck();
    document.querySelector('#check-text').focus();
    toast('Правка применена. Нажмите «Проверить текст» для нового отчёта.');
  });
}
function renderSourceList() {
  const list = document.querySelector('#source-list');
  if (!list) return;
  const count = document.querySelector('#source-list-count');
  if (count) count.textContent = `${number(state.sources.length)} материалов`;
  list.innerHTML = state.sources.length ? state.sources.map(source => `
    <article class="card source-card">
      <div>
        <h3>${escapeHTML(source.title)}</h3>
        <p>${escapeHTML(source.text.slice(0, 170))}${source.text.length > 170 ? '…' : ''}</p>
        ${safeLink(source.url)}
        <small>${number(chars(source.text).length)} знаков · ${escapeHTML(date(source.created_at))}</small>
      </div>
      <button class="button small danger" data-delete-source="${escapeHTML(source.id)}"
        type="button" aria-label="Удалить источник ${escapeHTML(source.title)}">Удалить</button>
    </article>`).join('') : empty(
      'Библиотека пока пуста',
      'Добавьте первый источник или откройте учебный пример на странице проверки.',
      '▤'
    );
  list.querySelectorAll('[data-delete-source]').forEach(button => {
    button.addEventListener('click', () => {
      if (!confirm('Удалить этот источник из библиотеки?')) return;
      busy(button, 'Удаляем', async () => {
        await api(`/api/sources/${encodeURIComponent(button.dataset.deleteSource)}`, undefined, 'DELETE');
        await refreshSources();
        renderSourceList();
        toast('Источник удалён.');
      });
    });
  });
}

function renderSources() {
  main.innerHTML = heading(
    'Библиотека источников',
    'Добавьте материалы, с которыми нужно сравнивать тексты. Ссылка сохраняется для отчёта, содержимое вставляется вручную.',
    '', 'Материалы для проверки'
  ) + `
    <section class="card form-card">
      <h2>Добавить источник</h2>
      <form id="source-form" class="source-form">
        <div class="form-grid">
          <div class="form-group">
            <label class="field-label" for="source-title">Название</label>
            <input class="field" id="source-title" name="title" required maxlength="300" placeholder="Например, статья об исследованиях">
          </div>
          <div class="form-group">
            <label class="field-label" for="source-url">Ссылка на публикацию · необязательно</label>
            <input class="field" id="source-url" name="url" type="url" maxlength="2000" placeholder="https://…">
          </div>
        </div>
        <div class="form-group">
          <div class="section-heading">
            <label class="field-label" for="source-text">Полный текст источника</label>
            <label class="file-button">↑ Загрузить .txt
              <input id="source-file" type="file" accept=".txt,.md,text/plain,text/markdown" aria-label="Загрузить текст источника">
            </label>
          </div>
          <textarea class="field" id="source-text" name="text" required maxlength="200000" placeholder="Вставьте текст публикации…"></textarea>
        </div>
        <button class="button" type="submit">Добавить в библиотеку <span aria-hidden="true">+</span></button>
        <p class="field-help">Источники сохраняются на вашем Mac и используются в следующих проверках.</p>
      </form>
    </section>
    <div class="section-heading source-list-heading">
      <h2>Ваши источники</h2><span class="subtle" id="source-list-count"></span>
    </div>
    <div id="source-list" class="source-list"></div>`;
  renderSourceList();
  document.querySelector('#source-form').addEventListener('submit', event => {
    event.preventDefault();
    const form = event.currentTarget;
    const submitted = {
      title: form.elements.title.value,
      text: form.elements.text.value,
      url: form.elements.url.value,
    };
    busy(form.querySelector('button[type="submit"]'), 'Сохраняем', async () => {
      await api('/api/sources', { ...submitted, title: submitted.title.trim(), url: submitted.url.trim() });
      await refreshSources();
      if (form.isConnected && Object.entries(submitted).every(([key, value]) => form.elements[key].value === value)) {
        form.reset();
      }
      renderSourceList();
      toast('Источник добавлен в библиотеку.');
    });
  });
  document.querySelector('#source-file').addEventListener('change', async event => {
    try {
      await readTextFile(event.target, document.querySelector('#source-text'));
    } catch (error) {
      toast(error.message, true);
    }
  });
}

async function renderEvaluation() {
  main.innerHTML = heading('Оценка Qwen', 'Проверьте, как Qwen сравнивает пару фрагментов с уже известным источником. Сохраните свои исправления.', '', 'Оценка локальной модели') + `<section class="card evaluation-top"><div><h2>Контрольная проверка</h2><p>Дословные совпадения, пересказы и независимые тексты на одну тему. Источник заранее дан модели.<br>Каждый запуск использует отложенную тестовую выборку. На Mac проверка может занять несколько минут.</p></div><button class="button" id="run-evaluation" type="button" ${!modelAvailable() ? 'disabled' : ''}>Запустить оценку <span aria-hidden="true">↗</span></button></section>${!modelAvailable() ? '<div class="warning">Локальная модель пока недоступна. Подключите Ollama и Qwen через меню модели сверху. Примеры можно просматривать уже сейчас.</div>' : ''}<div id="evaluation-result"></div><div class="section-heading" style="margin-top:28px"><div><h2>Размеченные примеры</h2><p>Учебные тексты написаны специально для прототипа.</p></div><button class="text-button" id="export-training" type="button">↓ Выгрузить обучение</button></div><div id="example-list"><p class="subtle">Загружаем примеры…</p></div>`;
  document.querySelector('#run-evaluation').addEventListener('click', event => busy(event.currentTarget, 'Qwen проходит проверку', async () => {
    const { run } = await api('/api/evaluate', { model: selectedModel(), split: 'test' });
    state.run = run;
    if (state.view === 'evaluation') {
      renderEvaluationResults();
      renderExamples();
    }
    toast('Оценка завершена. Результаты сохранены.');
  }));
  document.querySelector('#export-training').addEventListener('click', event => busy(event.currentTarget, 'Готовим выборку', async () => {
    const { records } = await api('/api/export', { split: 'train' });
    if (!records.length) throw new Error('Сначала сохраните свою оценку хотя бы одного примера с пометкой «Обучение».');
    download('istok-train.jsonl', records.map(record => JSON.stringify(record)).join('\n') + '\n', 'application/x-ndjson');
    toast('Обучающая выборка выгружена. Тестовые примеры сохраняются для оценки.');
  }));
  try {
    const [exampleData, runData] = await Promise.all([api('/api/examples'), api('/api/evaluations')]);
    state.examples = exampleData.examples;
    if (!state.run && runData.runs.length) state.run = runData.runs[0];
    if (state.view !== 'evaluation') return;
    renderExamples();
    renderEvaluationResults();
  } catch (error) { if (state.view === 'evaluation') document.querySelector('#example-list').innerHTML = `<div class="inline-error">${escapeHTML(error.message)}</div>`; }
}
function metricValue(value) {
  if (typeof value !== 'number') return '—';
  return `${(value * 100).toLocaleString('ru-RU', { maximumFractionDigits: 1 })}%`;
}
function renderEvaluationResults() {
  const container = document.querySelector('#evaluation-result');
  if (!container || !state.run) return;
  const run = state.run;
  const metrics = run.metrics || {};
  container.innerHTML = `<div class="section-heading" style="margin-top:25px"><div><h2>Последний запуск</h2><p>${escapeHTML(run.model)} · ${escapeHTML(date(run.created_at))}</p></div></div><div class="stats-row"><div class="stat-card"><strong>${metricValue(metrics.accuracy)}</strong><span>верных меток на примерах</span></div><div class="stat-card"><strong>${metricValue(metrics.precision)}</strong><span>точность положительных ответов</span></div><div class="stat-card"><strong>${metricValue(metrics.recall)}</strong><span>полнота при известном источнике</span></div></div><p class="dataset-note">Завершено ${number(metrics.completed)} из ${number(metrics.total)} примеров · ошибок запросов ${number(metrics.errors)}. Точность находок и полнота учитывают завершённые ответы.<br>Учебная выборка помогает находить ошибки. Для оценки качества на реальных документах понадобится расширенный независимый набор.</p>`;
}
function exampleHTML(example) {
  const result = state.run?.results?.find(item => item.example_id === example.id || item.id === example.id);
  const predicted = result?.predicted ?? result?.relation ?? result?.judgment?.relation;
  const correct = predicted === example.expected;
  const selected = example.review?.relation || example.expected;
  const resultText = result?.error || `Qwen · ${relationNames[predicted] || predicted || 'Ответ не получен'} ${correct ? '✓' : '· требуется проверка'}`;
  return `
    <article class="card example-card">
      <div class="example-title">
        <h3>${escapeHTML(example.title || example.id)}</h3>
        <span class="tag">${example.split === 'train' ? 'Обучение' : 'Тест'}</span>
      </div>
      <div class="example-pair">
        <div><small>ПРОВЕРЯЕМЫЙ ТЕКСТ</small><p>${escapeHTML(example.text)}</p></div>
        <div><small>ИСТОЧНИК</small><p>${escapeHTML(example.source_text)}</p></div>
      </div>
      <div class="example-result">
        <span class="subtle">Ожидаемый ответ</span> · <strong>${escapeHTML(relationNames[example.expected] || example.expected)}</strong>
        ${result ? `
          <p class="${correct ? 'result-ok' : 'result-fail'}">${escapeHTML(resultText)}</p>
          <p class="subtle">${escapeHTML(result.explanation || result.judgment?.explanation || '')}</p>` : ''}
      </div>
      <form class="review-form" data-review="${escapeHTML(example.id)}">
        <select name="relation" aria-label="Правильная оценка для ${escapeHTML(example.id)}">
          ${Object.entries(relationNames).map(([value, label]) => `<option value="${value}" ${value === selected ? 'selected' : ''}>${label}</option>`).join('')}
        </select>
        <input class="field" name="comment" maxlength="2000" aria-label="Комментарий к примеру"
          value="${escapeHTML(example.review?.comment || '')}" placeholder="Почему вы так считаете?">
        <button class="button small secondary" type="submit">Сохранить оценку</button>
      </form>
    </article>`;
}

function renderExamples() {
  const container = document.querySelector('#example-list');
  if (!container) return;
  container.innerHTML = state.examples.map(exampleHTML).join('') || empty(
    'Примеры ещё не добавлены',
    'Контрольная выборка появится здесь после подготовки данных.',
    '◈'
  );
  container.querySelectorAll('[data-review]').forEach(form => {
    form.addEventListener('submit', event => {
      event.preventDefault();
      busy(form.querySelector('button'), 'Сохраняем', async () => {
        const { review } = await api('/api/reviews', {
          example_id: form.dataset.review,
          relation: form.elements.relation.value,
          comment: form.elements.comment.value.trim(),
        });
        const example = state.examples.find(item => item.id === form.dataset.review);
        if (example) example.review = review;
        toast('Ваша оценка сохранена.');
      });
    });
  });
}

async function renderHistory() {
  main.innerHTML = heading('История проверок', 'Сохранённые отчёты доступны вместе с фрагментами источников.', '', 'Ваши документы') + '<div id="history-list"><p class="subtle">Загружаем отчёты…</p></div>';
  try {
    const { reports } = await api('/api/reports');
    if (state.view !== 'history') return;
    const list = document.querySelector('#history-list');
    list.innerHTML = reports.length ? reports.map(report => `<article class="card history-row"><div><small>${escapeHTML(date(report.created_at))}</small><h3>${escapeHTML(report.text.slice(0, 85))}${report.text.length > 85 ? '…' : ''}</h3><p>${number(report.stats.total_characters ?? chars(report.text).length)} знаков · ${Number(report.stats.similarity_percent || 0).toLocaleString('ru-RU', { maximumFractionDigits: 1 })}% совпадений</p></div><button class="button secondary small" data-open-report="${escapeHTML(report.id)}" type="button">Открыть отчёт ↗</button></article>`).join('') : empty('Проверок пока нет', 'Проверьте первый текст, и его отчёт появится здесь.', '↶');
    list.querySelectorAll('[data-open-report]').forEach(button => button.addEventListener('click', () => busy(button, 'Открываем', async () => {
      const revision = state.viewRevision;
      const { report } = await api(`/api/reports/${encodeURIComponent(button.dataset.openReport)}`);
      if (state.view !== 'history' || state.viewRevision !== revision) return;
      state.report = report;
      if (!state.text.trim() || state.text === report.text) saveDraft(report.text);
      state.selectedMatch = report.matches[0]?.id ?? null;
      state.rewrite = null;
      state.judgment = null;
      location.hash = '#check';
    })));
  } catch (error) { if (state.view === 'history') document.querySelector('#history-list').innerHTML = `<div class="inline-error">${escapeHTML(error.message)}</div>`; }
}
function download(filename, content, type) {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
async function refreshSources() {
  const { sources } = await api('/api/sources');
  state.sources = sources;
  document.querySelector('#source-count').textContent = sources.length;
  const libraryCount = document.querySelector('#library-count');
  if (libraryCount) libraryCount.textContent = sources.length;
  const select = document.querySelector('#compare-source');
  if (select) {
    const selected = select.value;
    select.replaceChildren(new Option('Выберите источник', ''));
    for (const source of sources) select.add(new Option(source.title, source.id));
    select.value = selected;
  }
}
async function refreshStatus() {
  state.status = await api('/api/status');
  updateModelControls();
}

function updateModelControls() {
  const model = state.status.model;
  const available = modelAvailable();
  document.querySelector('#model-dot').classList.toggle('muted', !available);
  document.querySelector('#model-label').textContent = available ? selectedModel() : 'Qwen не подключена';
  document.querySelector('#model-description').textContent = available ? 'Ollama доступна. Выберите модель для оценки пересказов и предложений правок.' : (model.error || 'Запустите Ollama и загрузите Qwen. Проверка точных совпадений доступна уже сейчас.');
  const select = document.querySelector('#model-select');
  select.replaceChildren();
  const models = installedModels();
  if (state.model && !installedModels().includes(state.model)) {
    select.add(new Option(`${state.model} · не установлена`, state.model));
  }
  for (const item of models) {
    const name = typeof item === 'string' ? item : item.name;
    select.add(new Option(name, name));
  }
  if (!models.length) select.add(new Option('Установленных моделей пока нет', ''));
  select.value = selectedModel() || '';
  select.disabled = !models.length;
  const batchButton = document.querySelector('#check-all-candidates');
  const currentRetrieval = state.retrievals[state.report?.id];
  if (batchButton) batchButton.disabled = !available || !currentRetrieval?.candidates.length || candidateCheckRunning(currentRetrieval.id);
  const hybrid = document.querySelector('#retrieve-hybrid');
  if (hybrid) hybrid.disabled = !state.status.embedding?.available || retrievalRunning(state.report?.id);
  const embeddingNotice = document.querySelector('#embedding-unavailable');
  if (embeddingNotice) embeddingNotice.hidden = Boolean(state.status.embedding?.available);
  document.querySelectorAll('[data-candidate-action="judge"], [data-candidate-action="paraphrase"]').forEach(button => {
    const card = button.closest('[data-candidate]');
    const key = `${card.dataset.retrieval}:${card.dataset.candidate}`;
    if (!candidateRequests.has(key)) button.disabled = !available || candidateCheckRunning(card.dataset.retrieval);
  });
  for (const id of ['compare-button', 'paraphrase-button', 'judge-button', 'run-evaluation']) {
    const button = document.getElementById(id);
    if (button && !button.querySelector('.spinner')) button.disabled = !available;
  }
}
function renderView() {
  state.viewRevision += 1;
  const requested = location.hash.replace('#', '') || 'check';
  state.view = viewNames[requested] ? requested : 'check';
  document.querySelectorAll('[data-view]').forEach(link => {
    const active = link.dataset.view === state.view;
    link.classList.toggle('active', active);
    if (active) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current');
  });
  document.querySelector('#breadcrumb-title').textContent = viewNames[state.view];
  document.title = `${viewNames[state.view]} · Исток`;
  if (state.view === 'check') renderCheck();
  else if (state.view === 'sources') renderSources();
  else if (state.view === 'evaluation') renderEvaluation();
  else renderHistory();
}
document.querySelector('#model-settings').addEventListener('click', () => document.querySelector('#model-dialog').showModal());
document.querySelector('#model-select').addEventListener('change', event => {
  state.model = event.target.value;
  try {
    localStorage.setItem(MODEL_KEY, state.model);
  } catch {
    // The selected model still applies to this open tab.
  }
  updateModelControls();
});
document.querySelector('#refresh-model').addEventListener('click', event => busy(event.currentTarget, 'Проверяем', async () => {
  await refreshStatus();
  toast(modelAvailable() ? 'Локальная модель доступна.' : 'Ollama или модель пока недоступны.', !modelAvailable());
}));
window.addEventListener('hashchange', renderView);
(async function init() {
  renderView();
  for (const [reportId, job] of Object.entries(state.retrievalJobs)) {
    pollRetrieval(reportId, job.id);
  }
  for (const [retrievalId, job] of Object.entries(state.candidateCheckJobs)) {
    pollCandidateCheck(retrievalId, job.id);
  }
  const results = await Promise.allSettled([refreshSources(), refreshStatus()]);
  for (const result of results) if (result.status === 'rejected') toast(result.reason.message, true);
  if (state.view === 'sources') renderSourceList();
})();

setInterval(() => {
  refreshStatus().catch(error => {
    state.status = { model: { models: [], error: error.message } };
    updateModelControls();
  });
}, 15000);
