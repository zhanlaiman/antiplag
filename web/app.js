'use strict';

const DRAFT_KEY = 'istok.draft.v1';
const MODEL_KEY = 'istok.model.v1';
const MODAL_MODEL_KEY = 'istok.model.modal.v1';
const MODEL_JOBS_KEY = 'istok.model-jobs.v1';
const MODEL_JOB_PATHS = ['/api/compare', '/api/candidate-judge', '/api/candidate-rewrite', '/api/judge', '/api/rewrite', '/api/evaluate', '/api/web-check'];
const RETRIEVAL_JOBS_KEY = 'istok.retrieval-jobs.v1';
const CANDIDATE_JOBS_KEY = 'istok.candidate-jobs.v1';
const REVIEW_DRAFTS_KEY = 'istok.review-drafts.v1';
const CHECK_REQUESTS_KEY = 'istok.check-requests.v1';
const OPERATION_IDS_KEY = 'istok.operation-ids.v1';
const SOURCE_DRAFT_KEY = 'istok.source-draft.v1';
const HOME_DRAFT_KEY = 'istok.home-draft.v1';
let storageScope = null;
let turnstileLoader = null;
let turnstileWidget = null;
const ACTIVE_JOB_STATUSES = ['queued', 'running', 'cancelling'];
const retrievalPolls = new Set();
const candidateRequests = new Set();
const candidateJobPolls = new Set();
const fileReadTokens = new WeakMap();
const modelJobObservers = new Map();
const modelRequestGenerations = new Map();

function storageKey(key) {
  if (storageScope === null) return null;
  return storageScope === 'local' ? key : `${key}.saas.${storageScope}`;
}

function storageGet(key) {
  const scoped = storageKey(key);
  return scoped ? localStorage.getItem(scoped) : null;
}

function storageSet(key, value) {
  const scoped = storageKey(key);
  if (!scoped) throw new Error('Рабочее пространство ещё не открыто.');
  localStorage.setItem(scoped, value);
}

function isSaas() { return state.session?.mode === 'saas'; }
function privateAccess() {
  return state.sessionReady && !state.sessionExpired && !state.sessionTransition
    && (!isSaas() || Boolean(state.session.user?.email_verified));
}

function authError(error) {
  return error.status === 401 || ['auth_required', 'session_changed'].includes(error.code);
}

function hydrateSessionStorage() {
  storageScope = isSaas() ? state.session.user ? `user.${encodeURIComponent(state.session.user.id)}` : 'guest' : 'local';
  state.text = restoreDraft();
  try { state.homeText = storageGet(HOME_DRAFT_KEY) || ''; } catch { state.homeText = ''; }
  state.model = restoreModel();
  state.modelJobs = restoreModelJobs();
  state.retrievalJobs = restoreActiveJobs(RETRIEVAL_JOBS_KEY);
  state.candidateCheckJobs = restoreActiveJobs(CANDIDATE_JOBS_KEY);
  const reviews = restoreReviewDrafts();
  state.findingDrafts = reviews.findings;
  state.exampleDrafts = reviews.examples;
  try { state.checkRequests = JSON.parse(storageGet(CHECK_REQUESTS_KEY) || '{}'); }
  catch { state.checkRequests = {}; }
  try { state.operationIds = JSON.parse(storageGet(OPERATION_IDS_KEY) || '{}'); }
  catch { state.operationIds = {}; }
  try { state.sourceDraft = JSON.parse(storageGet(SOURCE_DRAFT_KEY) || '{}'); }
  catch { state.sourceDraft = {}; }
}

function restoreDraft() {
  try {
    const draft = storageGet(DRAFT_KEY);
    return typeof draft === 'string' ? draft : '';
  } catch {
    return '';
  }
}

function saveDraft(text) {
  if (state.text !== text) document.querySelector('#direct-judgment')?.replaceChildren();
  state.text = text;
  try {
    storageSet(DRAFT_KEY, text);
    state.draftSaved = true;
  } catch {
    state.draftSaved = false;
  }
  updateDraftStatus();
  updateReportFreshness();
}

function restoreModel(provider = 'ollama') {
  try {
    return storageGet(provider === 'modal' ? MODAL_MODEL_KEY : MODEL_KEY) || '';
  } catch {
    return '';
  }
}

function restoreModelJobs() {
  try {
    const saved = JSON.parse(storageGet(MODEL_JOBS_KEY) || '{}');
    return Object.fromEntries(Object.entries(saved).filter(([id, entry]) => (
      /^[a-f0-9]{32}$/.test(id) && entry?.request_id === id
      && MODEL_JOB_PATHS.includes(entry.path) && entry.body && typeof entry.body === 'object'
      && !Array.isArray(entry.body)
    )).map(([id, entry]) => [id, { ...entry, stage: 'Восстанавливаем состояние задания' }]));
  } catch {
    return {};
  }
}

function saveModelJobs() {
  try {
    const saved = Object.fromEntries(Object.entries(state.modelJobs).map(([id, entry]) => [id, {
      request_id: id, path: entry.path, body: entry.body, context: entry.context,
      created_at: entry.created_at, status: entry.status, submission_error: entry.submission_error,
    }]));
    storageSet(MODEL_JOBS_KEY, JSON.stringify(saved));
    return true;
  } catch {
    return false;
  }
}

function restoreActiveJobs(storageKey) {
  try {
    const saved = JSON.parse(storageGet(storageKey) || '{}');
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
    storageSet(storageKey, JSON.stringify(active));
    return true;
  } catch {
    // Polling in this open tab continues when browser storage is unavailable.
    return false;
  }
}

function restoreReviewDrafts() {
  try {
    const saved = JSON.parse(storageGet(REVIEW_DRAFTS_KEY) || '{}');
    const findings = Object.fromEntries(Object.entries(saved.findings || {})
      .filter(([, value]) => typeof value === 'string' && value.length <= 8000));
    const examples = Object.fromEntries(Object.entries(saved.examples || {})
      .filter(([, value]) => value && typeof value.comment === 'string' && typeof value.relation === 'string'));
    return { findings, examples };
  } catch {
    return { findings: {}, examples: {} };
  }
}

function saveReviewDrafts() {
  try {
    storageSet(REVIEW_DRAFTS_KEY, JSON.stringify({
      findings: state.findingDrafts, examples: state.exampleDrafts,
    }));
  } catch {
    // Dirty review fields remain available in the current tab.
  }
}

const reviewDrafts = restoreReviewDrafts();

function updateDraftStatus() {
  const status = document.querySelector('#draft-status');
  if (!status) return;
  status.textContent = state.draftSaved === false
    ? 'Черновик хранится только в открытой вкладке'
    : isSaas() ? 'Черновик сохраняется в этом браузере для вашего аккаунта' : 'Черновик сохраняется на этом Mac';
}

function updateReportFreshness() {
  const stale = Boolean(state.report && state.text !== state.report.text);
  const notice = document.querySelector('#stale-report');
  if (notice) notice.hidden = !stale;
  const apply = document.querySelector('#apply-rewrite');
  if (apply) apply.disabled = stale;
  const retrieval = state.retrievals[state.report?.id];
  document.querySelectorAll('[data-apply-candidate]').forEach(button => {
    const card = button.closest('[data-candidate]');
    const key = card ? `${card.dataset.retrieval}:${card.dataset.candidate}` : '';
    button.disabled = stale || Boolean(retrieval && candidateCheckRunning(retrieval.id)) || candidateRequests.has(key)
      || Boolean(card && candidateHasModelJob(card.dataset.retrieval, card.dataset.candidate));

  });
}


const state = {
  session: null, sessionReady: false, sessionExpired: false, sessionTransition: false,
  authChallenge: null, authEmail: '', captchaToken: '', checkRequests: {}, operationIds: {},
  sourceDraft: {},
  checkMode: null, checkModeChosen: false, internetConsent: false,
  homeText: '', homeConsent: false,
  view: 'check', viewRevision: 0, text: restoreDraft(), sources: [], status: null, model: restoreModel(), report: null,
  modelProvider: 'ollama', modelJobs: restoreModelJobs(),
  selectedMatch: null, rewrite: null, judgment: null, examples: [], run: null,
  retrievals: {}, retrievalJobs: restoreActiveJobs(RETRIEVAL_JOBS_KEY), candidateJudgments: {}, candidateRewrites: {},
  candidateErrors: {}, candidateLoaded: new Set(), candidateGenerations: {}, candidateLoadRequests: {},
  homeReport: null, homeMatch: null, sourcePacks: null,
  findingDrafts: reviewDrafts.findings, exampleDrafts: reviewDrafts.examples, candidateCheckJobs: restoreActiveJobs(CANDIDATE_JOBS_KEY),
};
const main = document.querySelector('#main-content');
const relationNames = { exact: 'Дословное совпадение', paraphrase: 'Возможный пересказ', same_topic: 'Общая тема', unrelated: 'Разные тексты' };
const viewNames = { home: 'Главная', check: 'Проверка текста', sources: 'Источники', evaluation: 'Оценка Qwen', history: 'История' };
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
const number = value => Number(value || 0).toLocaleString('ru-RU');
const chars = value => Array.from(value);
function installedModels() {
  const embedding = state.status?.embedding?.model?.split(':')[0];
  return (state.status?.model?.models || [])
    .map(model => typeof model === 'string' ? model : model.name)
    .filter(name => !embedding || name.split(':')[0] !== embedding);

}

function modelProvider() {
  return state.status?.model?.provider || 'ollama';
}

function selectedModel() {
  if (state.model) return state.model;
  const models = installedModels();
  const preferred = state.status?.model?.model;
  return models.includes(preferred) ? preferred : models[0] || preferred;
}

function modelAvailable() {
  if (modelProvider() === 'modal') {
    return Boolean(state.status?.model?.configured && state.status.model.can_submit
      && installedModels().includes(selectedModel()));
  }
  if (state.status?.model?.local_inference_disabled || modelProvider() === 'disabled') return false;
  return installedModels().includes(selectedModel());
}

function processingDisclosure() {
  if (modelProvider() === 'modal') {
    const trial = state.status?.model?.synthetic_only
      ? ' Тестовый облачный режим. Используйте учебный пример или открытые источники.' : '';
    return 'Для смыслового поиска и сравнения тексты и фрагменты библиотеки передаются серверным моделям Modal. На Mac модели не запускаются.' + trial;
  }
  if (state.status?.model?.local_inference_disabled || modelProvider() === 'disabled') {
    return 'Локальные модели отключены. Точный поиск по библиотеке работает без модели.';
  }
  return 'Точный поиск работает по вашей библиотеке. Способ обработки смысловых фрагментов указан в настройках модели.';
}

function updateProcessingLabels() {
  document.querySelectorAll('[data-processing-disclosure]').forEach(element => {
    element.textContent = processingDisclosure();
  });
  const note = document.querySelector('#model-processing-note');
  if (note) note.textContent = processingDisclosure();
  const trial = document.querySelector('#model-trial-note');
  if (trial) {
    trial.hidden = !state.status?.model?.synthetic_only;
    trial.textContent = 'Тестовый облачный режим. Используйте учебный пример или открытые источники.';
  }
}

function currentComparison(reportId, matchId) {
  return state.view === 'check' && state.report?.id === reportId && state.selectedMatch === matchId;
}
const date = value => value ? new Date(value).toLocaleString('ru-RU', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : '';

async function api(path, data, method) {
  const publicPath = path === '/api/session' || path.startsWith('/api/auth/');
  if (isSaas() && !publicPath && !privateAccess()) {
    const error = new Error('Войдите в аккаунт, чтобы продолжить. Черновик сохранён в браузере.');
    error.status = 401;
    error.code = 'auth_required';
    throw error;
  }
  const owner = state.session?.user?.id;
  const verb = method || (data === undefined ? 'GET' : 'POST');
  const headers = data === undefined ? {} : { 'Content-Type': 'application/json' };
  if (isSaas() && verb !== 'GET') headers['X-Istok-CSRF'] = state.session.csrf_token || '';
  const response = await fetch(path, {
    method: verb, headers, credentials: 'same-origin',
    body: data === undefined ? undefined : JSON.stringify(data),
  });
  let result;
  try { result = await response.json(); }
  catch { throw new Error('Сервер вернул неожиданный ответ. Проверьте, что локальная версия запущена.'); }
  if (!response.ok) {
    const error = new Error(result.error || result.message || 'Не удалось выполнить запрос.');
    error.status = response.status;
    error.code = result.code;
    error.details = result.details;
    if (isSaas() && result.quota) { state.session.quota = result.quota; updateQuotaUI(); }
    if (isSaas() && response.status === 401 && !publicPath) {
      saveDraft(state.text);
      state.sessionExpired = true;
      await refreshSession().catch(() => {});
      renderView();
      renderModelJobs();
    }
    throw error;
  }
  if (!publicPath && isSaas() && (state.sessionTransition || state.sessionExpired || state.session.user?.id !== owner)) {
    const error = new Error('Сессия изменилась. Ответ сохранён в исходном аккаунте.');
    error.code = 'session_changed';
    throw error;
  }
  return result;
}

function resetCaptcha(remove = false) {
  state.captchaToken = '';
  if (turnstileWidget !== null && window.turnstile) {
    if (remove) { window.turnstile.remove(turnstileWidget); turnstileWidget = null; }
    else window.turnstile.reset(turnstileWidget);
  }
  updateAuthButton();
}

function updateAuthButton() {
  const button = document.querySelector('#auth-request-button');
  if (button) button.disabled = !state.session?.auth?.configured || Boolean(button.dataset.busy)
    || Boolean(state.session.auth.captcha_site_key && !state.captchaToken);
}

async function mountCaptcha() {
  const sitekey = state.session?.auth?.captcha_site_key;
  const container = document.querySelector('#auth-captcha');
  if (!isSaas() || privateAccess() || !state.session.auth.configured || !sitekey || !container || state.authChallenge) return;
  if (!window.turnstile) {
    if (!turnstileLoader) turnstileLoader = new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
      script.async = true;
      script.onload = resolve;
      script.onerror = () => { script.remove(); turnstileLoader = null; reject(new Error('Не удалось загрузить проверку CAPTCHA. Попробуйте ещё раз.')); };
      document.head.append(script);
    });
    await turnstileLoader;
  }
  if (!container.isConnected || state.authChallenge || !window.turnstile) return;
  turnstileWidget = window.turnstile.render(container, {
    sitekey, theme: 'light', size: 'compact',
    callback(token) { if (container.isConnected) { state.captchaToken = token; updateAuthButton(); } },
    'expired-callback'() { if (container.isConnected) resetCaptcha(); },
    'error-callback'() {
      if (container.isConnected) {
        state.captchaToken = '';
        document.querySelector('#auth-feedback').textContent = 'Проверка CAPTCHA не завершена. Нажмите «Обновить CAPTCHA».';
        updateAuthButton();
      }
    },
  });
}

function renderAuthScreen() {
  resetCaptcha(true);
  const auth = state.session?.auth || {};
  main.innerHTML = `
    <section class="auth-layout">
      <div class="auth-intro"><div class="eyebrow">Исток · личное пространство</div>
        <h1>Проверьте текст.<br>Сохраните источники.</h1>
        <p>До 6 000 знаков в сутки после подтверждения почты. ${webConfigured() ? 'Ищем доступные страницы в интернете и показываем совпавшие фрагменты со ссылками.' : 'Доступна проверка по вашей библиотеке. Поиск страниц в интернете ещё не подключён.'}</p>
        <p class="field-help">В бесплатном режиме до 3 проверок и 3 правок в сутки. Смысловая сверка ограничена общим лимитом модели: большая подборка может в него не войти. Подтверждение почты и CAPTCHA помогают ограничивать массовые регистрации.</p>
      </div>
      <section class="card auth-card"><h2>${state.authChallenge ? 'Введите код из письма' : 'Войти по почте'}</h2>
        <p class="field-help">${state.sessionExpired ? 'Сессия завершилась. Черновик вашего аккаунта сохранён в браузере.' : 'Пароль не нужен. Отправим одноразовый код.'}</p>
        ${!auth.configured ? '<p class="warning">Вход по почте пока не настроен. Можно сохранить гостевой черновик ниже.</p>' : ''}
        <form id="auth-request-form" ${state.authChallenge ? 'hidden' : ''}>
          <label class="field-label" for="auth-email">Электронная почта</label>
          <input class="field" id="auth-email" type="email" autocomplete="email" required maxlength="254" value="${escapeHTML(state.authEmail)}">
          <div id="auth-captcha"></div>
          ${auth.captcha_site_key ? '<button class="text-button" id="auth-refresh-captcha" type="button">Обновить CAPTCHA</button>' : ''}
          <button class="button" id="auth-request-button" type="submit" disabled>Получить код</button>
        </form>
        <form id="auth-verify-form" ${state.authChallenge ? '' : 'hidden'}>
          <p>Код отправлен на ${escapeHTML(state.authEmail)}.</p>
          <label class="field-label" for="auth-code">Код подтверждения</label>
          <input class="field" id="auth-code" autocomplete="one-time-code" inputmode="numeric" pattern="[0-9]{6,8}" minlength="6" maxlength="8" required>
          <button class="button" id="auth-verify-button" type="submit">Подтвердить почту</button>
          <button class="text-button" id="auth-new-code" type="button">Получить новый код</button>
        </form>
        <p id="auth-feedback" class="inline-error" role="alert"></p>
        <p class="field-help">Общая сеть офиса или учебного заведения сама по себе не означает нарушение. При ограничении сохраните сообщение сервиса для обращения в поддержку.</p>
      </section>
    </section>
    <section class="card guest-draft"><h2>Гостевой черновик</h2>
      <p class="field-help">Остаётся в этом браузере. В новый аккаунт автоматически не переносится.</p>
      <textarea id="guest-text" aria-label="Гостевой черновик" placeholder="Можно сохранить текст до входа…"></textarea>
      <p class="field-help" id="guest-draft-status" role="status"></p>
    </section>`;
  const guestKey = `${DRAFT_KEY}.saas.guest`;
  if (state.guestText === undefined) {
    try { state.guestText = localStorage.getItem(guestKey) || ''; } catch { state.guestText = ''; }
  }
  const guest = document.querySelector('#guest-text');
  guest.value = state.guestText;
  guest.addEventListener('input', () => {
    state.guestText = guest.value;
    try {
      localStorage.setItem(guestKey, guest.value);
      document.querySelector('#guest-draft-status').textContent = 'Гостевой черновик сохранён в этом браузере.';
    } catch { document.querySelector('#guest-draft-status').textContent = 'Черновик хранится только в открытой вкладке.'; }
  });
  document.querySelector('#auth-email').addEventListener('input', event => { state.authEmail = event.target.value; });
  document.querySelector('#auth-request-form').addEventListener('submit', event => {
    event.preventDefault();
    busy(document.querySelector('#auth-request-button'), 'Отправляем код', async () => {
      state.authEmail = document.querySelector('#auth-email').value.trim();
      try {
        if (!auth.configured) throw new Error('Вход по почте пока не настроен.');
        if (auth.captcha_site_key && !state.captchaToken) throw new Error('Завершите CAPTCHA перед отправкой кода.');
        const result = await api('/api/auth/request', { email: state.authEmail, captcha_token: state.captchaToken });
        state.authChallenge = result.challenge_id;
        renderAuthScreen();
      } catch (error) { document.querySelector('#auth-feedback').textContent = error.message; resetCaptcha(); throw error; }
    });
  });
  document.querySelector('#auth-verify-form').addEventListener('submit', event => {
    event.preventDefault();
    busy(document.querySelector('#auth-verify-button'), 'Подтверждаем', async () => {
      try {
        await api('/api/auth/verify', { challenge_id: state.authChallenge, email: state.authEmail, code: document.querySelector('#auth-code').value.trim() });
        state.sessionTransition = true;
        location.reload();
      } catch (error) { document.querySelector('#auth-feedback').textContent = error.message; throw error; }
    });
  });
  document.querySelector('#auth-new-code').addEventListener('click', () => { state.authChallenge = null; renderAuthScreen(); });
  document.querySelector('#auth-refresh-captcha')?.addEventListener('click', () => {
    if (turnstileWidget !== null) resetCaptcha();
    else mountCaptcha().catch(error => { document.querySelector('#auth-feedback').textContent = error.message; });
  });
  updateAuthButton();
  mountCaptcha().catch(error => { if (document.querySelector('#auth-feedback')) document.querySelector('#auth-feedback').textContent = error.message; });
}

async function refreshSession() {
  const previous = state.session;
  const session = await api('/api/session');
  if (!['local', 'saas'].includes(session.mode)) throw new Error('Сервер вернул неизвестный режим рабочего пространства.');
  if (previous && (previous.mode !== session.mode || (session.user && previous.user?.id !== session.user.id))) {
    if (privateAccess()) saveDraft(state.text);
    state.sessionTransition = true;
    location.reload();
    return;
  }
  const lostUser = Boolean(previous?.user && !session.user);
  if (lostUser) {
    saveDraft(state.text);
    state.sessionExpired = true;
  }
  state.session = session;
  updateAccountUI();
  if (lostUser) { renderView(); renderModelJobs(); }
}

function updateAccountUI() {
  const user = state.session?.user;
  const name = document.querySelector('#account-name');
  const description = document.querySelector('#account-description');
  const logout = document.querySelector('#logout-button');
  if (name) name.textContent = isSaas() ? user ? user.email || 'Аккаунт подтверждён' : 'Вход по почте' : 'Личное пространство';
  if (description) description.textContent = isSaas() ? '6 000 знаков в сутки' : 'Локальный прототип';
  if (logout) logout.hidden = !isSaas() || !user;
  const evaluation = document.querySelector('#evaluation-nav');
  if (evaluation) evaluation.hidden = isSaas();
  const storageNote = document.querySelector('#workspace-storage-note');
  if (storageNote) storageNote.textContent = isSaas() ? 'Источники и отчёты доступны в вашем аккаунте.' : 'Тексты и отчёты сохраняются в локальной библиотеке.';
  updateQuotaUI();
}

function quotaRemaining() {
  return Number.isFinite(state.session?.quota?.remaining) ? state.session.quota.remaining : 0;
}

function webStatus() { return state.status?.web || state.session?.web || {}; }
function webConfigured() { return webStatus().configured === true; }
function internetMode() { return state.checkMode === 'internet'; }

function updateCheckModeUI() {
  if (!state.checkModeChosen) state.checkMode = webConfigured() ? 'internet' : 'library';
  const internet = internetMode();
  for (const [id, selected] of [['mode-internet', internet], ['mode-library', !internet]]) {
    const tab = document.getElementById(id);
    if (tab) { tab.setAttribute('aria-selected', String(selected)); tab.classList.toggle('active', selected); }
  }
  const description = document.querySelector('#check-mode-description');
  if (description) {
    description.setAttribute('aria-labelledby', internet ? 'mode-internet' : 'mode-library');
    description.textContent = internet
      ? 'Находим страницы через поисковый сервис, загружаем доступный текст и показываем совпадения со ссылками. Qwen для этой проверки не используется.'
      : 'Сравниваем текст только с материалами вашей библиотеки. Этот результат не является уникальностью по интернету.';
  }
  const unavailable = document.querySelector('#web-availability');
  if (unavailable) unavailable.hidden = webConfigured();
  const setup = document.querySelector('#setup-search-link');
  if (setup) setup.hidden = isSaas();
  const options = document.querySelector('#internet-options');
  if (options) options.hidden = !internet;
  const consent = document.querySelector('#internet-consent');
  if (consent) consent.checked = state.internetConsent;
  const tools = document.querySelector('#library-tools');
  if (tools) tools.hidden = internet;
  const scope = document.querySelector('#check-scope');
  if (scope) scope.innerHTML = scopeAside();
  const button = document.querySelector('#check-button');
  if (button && !button.dataset.busy) button.textContent = internet ? 'Проверить уникальность ↗' : 'Проверить по библиотеке ↗';
  updateQuotaUI();
}

function matchingCheckRequest(text = state.text) {
  const sourceIds = state.sources.map(source => source.id).sort();
  return Object.values(state.checkRequests).find(entry => entry.text === text
    && JSON.stringify(entry.source_ids) === JSON.stringify(sourceIds));
}

function updateQuotaUI() {
  const summary = document.querySelector('#quota-summary');
  const quota = state.session?.quota;
  if (summary) {
    summary.hidden = !isSaas() || !privateAccess();
    summary.textContent = `Осталось ${number(quotaRemaining())} из ${number(quota?.limit || 6000)} знаков`;
  }
  const note = document.querySelector('#check-quota-note');
  const entry = isSaas() && !internetMode() ? matchingCheckRequest() : null;
  const length = chars(state.text).length;
  if (note) {
    note.hidden = !isSaas();
    note.textContent = length > quotaRemaining()
      ? `В тексте ${number(length)} знаков, осталось ${number(quotaRemaining())}. Текст сохранён целиком. Сократите его перед новой проверкой.`
      : `На новую проверку ${number(length)} знаков. Осталось ${number(quotaRemaining())} из ${number(quota?.limit || 6000)} сегодня.${quota?.reset_at ? ` Лимит обновится ${date(quota.reset_at)}.` : ''}`;
  }
  const button = document.querySelector('#check-button');
  const pendingWeb = internetMode() && pendingModelRequest('/api/web-check', { text: state.text, internet_consent: true });
  if (button) button.disabled = Boolean(button.dataset.busy) || !privateAccess()
    || Boolean(isSaas() && entry?.status !== 'complete' && length > quotaRemaining())
    || Boolean(entry && entry.status !== 'complete')
    || Boolean(internetMode() && (!webConfigured() || !state.internetConsent || pendingWeb));
  updateHomeControls();
}

function saveCheckRequests() {
  storageSet(CHECK_REQUESTS_KEY, JSON.stringify(state.checkRequests));
}

function renderCheckRequests() {
  const area = document.querySelector('#check-requests');
  if (!area) return;
  const entries = isSaas() ? Object.values(state.checkRequests) : [];
  area.hidden = !entries.length;
  area.innerHTML = entries.map(entry => `<div class="card check-request-notice">
    <strong>${entry.status === 'complete' ? 'Отчёт сохранён' : entry.status === 'pending' ? 'Проверка отправлена' : 'Проверка не завершена'}</strong>
    <p>${number(chars(entry.text).length)} знаков${entry.error ? ` · ${escapeHTML(entry.error.message)}` : ''}</p>
    ${entry.status === 'complete'
      ? `<button class="text-button" id="open-check-${entry.request_id}" type="button">Открыть отчёт</button>`
      : `<p class="field-help">Повторная отправка использует прежний идентификатор и исходный текст. Автоматического повтора нет.</p><button class="text-button" id="retry-check-${entry.request_id}" type="button" ${entry.status === 'pending' && state.checkSending === entry.request_id ? 'disabled' : ''}>Повторить отправку проверки</button>`}
  </div>`).join('');
  for (const entry of entries) {
    document.getElementById(`retry-check-${entry.request_id}`)?.addEventListener('click', event => busy(event.currentTarget, 'Отправляем проверку', () => sendCheckRequest(entry)));
    document.getElementById(`open-check-${entry.request_id}`)?.addEventListener('click', event => busy(event.currentTarget, 'Открываем отчёт', async () => {
      const { report } = await api(`/api/reports/${encodeURIComponent(entry.report_id || entry.request_id)}`);
      state.report = report;
      state.selectedMatch = report.matches[0]?.id || null;
      state.rewrite = null;
      state.judgment = null;
      if (state.view === 'check') renderReport();
    }));
  }
  updateQuotaUI();
}

async function sendCheckRequest(entry) {
  if (state.checkSending === entry.request_id) return;
  entry.status = 'pending';
  try { saveCheckRequests(); }
  catch {
    entry.status = 'rejected';
    entry.error = { message: 'Не удалось сохранить отправку в браузере. Проверка не отправлена.' };
    renderCheckRequests();
    throw new Error(entry.error.message);
  }
  state.checkSending = entry.request_id;
  renderCheckRequests();
  try {
    const { report } = await api('/api/check', { request_id: entry.request_id, text: entry.text, source_ids: entry.source_ids });
    entry.status = 'complete';
    entry.report_id = report.id;
    delete entry.error;
    saveCheckRequests();
    acceptCheckReport(report, entry.text);
  } catch (error) {
    entry.status = error.status >= 400 && error.status < 500 ? 'rejected' : 'uncertain';
    entry.error = { message: error.message, code: error.code, details: error.details };
    saveCheckRequests();
    throw error;
  } finally {
    state.checkSending = null;
    renderCheckRequests();
    if (privateAccess()) await refreshSession().catch(() => {});
  }
}

function publicOperationId(key) {
  if (!isSaas()) return undefined;
  if (!state.operationIds[key]) {
    state.operationIds[key] = crypto.randomUUID().replaceAll('-', '');
    storageSet(OPERATION_IDS_KEY, JSON.stringify(state.operationIds));
  }
  return state.operationIds[key];
}

function acknowledgeOperation(key) {
  if (!isSaas()) return;
  delete state.operationIds[key];
  storageSet(OPERATION_IDS_KEY, JSON.stringify(state.operationIds));
}

function publicReportReady(reportId = state.report?.id) {
  return !isSaas() || Boolean(privateAccess() && state.report && state.report.id === reportId && state.text === state.report.text);
}

function modelJobTitle(entry) {
  if (entry.path === '/api/web-check') return 'Проверка уникальности в интернете';
  if (entry.path === '/api/evaluate') return 'Оценка на учебных примерах';
  if (entry.path.includes('rewrite')) return entry.body.mode === 'quote' ? 'Оформление цитаты' : 'Предложение правки';
  return 'Сравнение фрагментов';
}

function modelJobActive(entry) {
  return ['submitting', 'delivery_unknown', 'missing', 'rejected', ...ACTIVE_JOB_STATUSES].includes(entry.job?.status || entry.status);
}

function canonicalModelBody(value) {
  if (Array.isArray(value)) return value.map(canonicalModelBody);
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.keys(value).sort().map(key => [key, canonicalModelBody(value[key])]));
  }
  return value;
}

function pendingModelRequest(path, body) {
  const signatureOf = value => {
    const { operation_id, ...input } = value;
    return JSON.stringify(canonicalModelBody(input));
  };
  const signature = signatureOf(body);
  return Object.values(state.modelJobs).find(entry => entry.path === path && modelJobActive(entry)
    && signatureOf(entry.body) === signature);
}

function candidateHasModelJob(retrievalId, candidateId) {
  return Object.values(state.modelJobs).some(entry => entry.body.retrieval_id === retrievalId
    && entry.body.candidate_id === candidateId && modelJobActive(entry));
}

function modelJobResultHTML(entry) {
  const result = entry.job?.result;
  if (!result) return '';
  if (result.report) {
    const report = result.report;
    return `<p>${report.scope === 'web_search' ? 'Уникальность' : 'Отчёт'} · ${webUniqueness(report)}</p>
      <button class="text-button" id="open-web-report-${entry.request_id}" type="button">Открыть отчёт со ссылками</button>`;
  }
  const key = `${entry.body.retrieval_id}:${entry.body.candidate_id}`;
  if (entry.path === '/api/candidate-judge') {
    if (!entry.latestLoaded) return '<p>Ответ задания сохранён. Актуальную оценку пары пока не удалось загрузить. Откройте отчёт и обновите её перед правкой.</p>';
    if (state.candidateErrors[key]) return candidateErrorHTML(state.candidateErrors[key]);
    const latest = state.candidateJudgments[key];
    return latest ? judgmentHTML(latest) : '<p>Ответ сохранён. Актуальная оценка будет загружена из отчёта.</p>';
  }
  if (result.rewrite) {
    const rewrite = result.rewrite;
    const original = rewrite.original_text || entry.context?.original_text;
    if (entry.path === '/api/candidate-rewrite') {
      const judgment = state.candidateJudgments[key];
      if (!entry.latestLoaded || !judgment || judgment.id !== rewrite.judgment_id || !['exact', 'paraphrase'].includes(judgment.relation)) {
        return '<p>Оценка пары изменилась или ещё не загружена. Откройте отчёт и проверьте актуальный ответ перед новой правкой.</p>';
      }
    }
    return `<p class="field-help">Сохранённое предложение. Текст в редакторе не изменён.</p>
      ${original ? `<h3>Исходный фрагмент</h3><blockquote>${escapeHTML(original)}</blockquote>` : ''}
      <h3>Предложенная правка</h3><blockquote>${escapeHTML(rewrite.text)}</blockquote>`;
  }
  if (result.judgment) return judgmentHTML(result.judgment);
  if (result.run) return `<p>Оценка модели ${escapeHTML(result.run.model)} сохранена. Откройте экран «Оценка Qwen», чтобы посмотреть результаты.</p>`;
  return '';
}

function renderModelJobs() {
  const panel = document.querySelector('#model-jobs-panel');
  if (!panel) return;
  if (!privateAccess() || state.view === 'home') { panel.hidden = true; updateHomeControls(); return; }
  const entries = Object.values(state.modelJobs);
  const openResults = entries.filter(entry => document.getElementById(`model-job-result-${entry.request_id}`)?.open);
  const focusedId = panel.contains(document.activeElement) ? document.activeElement.id : '';
  panel.hidden = !entries.length;
  panel.innerHTML = entries.map(entry => {
    const job = entry.job;
    const status = job?.status || entry.status;
    const active = ['submitting', ...ACTIVE_JOB_STATUSES].includes(status);
    const missing = ['missing', 'delivery_unknown', 'rejected'].includes(status);
    const progress = job?.progress?.total ? ` · ${number(job.progress.current)} из ${number(job.progress.total)}` : '';
    const stage = entry.watchMessage || job?.stage || entry.stage || 'В очереди';
    const uncertain = job?.code === 'unconfirmed_exact';
    const outcome = uncertain ? 'Нужна проверка редактора'
      : status === 'complete' ? 'Ответ готов и сохранён'
      : status === 'cancelled' ? 'Задание остановлено'
      : status === 'failed' ? 'Задание завершилось с ошибкой'
      : status === 'rejected' ? 'Сервер не принял задание'
      : missing ? 'Отправка не подтверждена' : stage;
    return `<article class="model-job-notice">
      <div class="model-job-heading"><strong>${escapeHTML(modelJobTitle(entry))}</strong><span>${escapeHTML(entry.body.model || '')}${entry.created_at ? ` · ${escapeHTML(date(entry.created_at))}` : ''}</span></div>
      ${entry.context?.source_title ? `<p class="field-help">${escapeHTML(entry.context.source_title)}</p>` : ''}
      <p role="status">${active ? '<span class="spinner" aria-hidden="true"></span>' : ''}${escapeHTML(outcome)}${progress}</p>
      ${active ? '<p class="field-help">Можно перейти на другой экран или закрыть вкладку. Состояние задания сохранено.</p>' : ''}
      ${job?.error ? uncertain
        ? candidateErrorHTML({ message: job.error, code: job.code, details: job.details })
        : `<div class="inline-error">${escapeHTML(job.error)}</div>` : ''}
      ${missing ? `<p class="inline-error">${escapeHTML(entry.submission_error?.message || entry.watchMessage || 'Не удалось подтвердить отправку задания. Автоматического повторного запуска нет.')}</p>` : ''}
      ${status === 'complete' ? `<details id="model-job-result-${entry.request_id}"><summary id="model-job-summary-${entry.request_id}">Посмотреть сохранённый результат</summary>${modelJobResultHTML(entry)}</details>` : ''}
      <div class="action-row">
        ${active && job?.id ? `<button class="text-button" id="model-job-cancel-${entry.request_id}" type="button" ${status === 'cancelling' ? 'disabled' : ''}>${status === 'cancelling' ? 'Останавливаем после текущего ответа' : 'Остановить'}</button>` : ''}
        ${missing ? `<button class="button small secondary" id="model-job-retry-${entry.request_id}" type="button">Повторить отправку этого задания</button>` : ''}
        ${!active ? `<button class="text-button" id="model-job-dismiss-${entry.request_id}" type="button">Убрать уведомление</button>` : ''}
      </div>
    </article>`;
  }).join('');
  for (const entry of openResults) {
    const details = document.getElementById(`model-job-result-${entry.request_id}`);
    if (details) details.open = true;
  }
  if (focusedId) document.getElementById(focusedId)?.focus({ preventScroll: true });
  for (const entry of entries) {
    document.getElementById(`open-web-report-${entry.request_id}`)?.addEventListener('click', () => {
      const report = entry.job?.result?.report;
      if (!report) return;
      state.report = report;
      state.selectedMatch = report.matches[0]?.id || null;
      state.rewrite = null;
      state.judgment = null;
      if (state.view === 'check') renderReport();
      else location.hash = '#check';
    });
    document.getElementById(`model-job-cancel-${entry.request_id}`)?.addEventListener('click', event => busy(event.currentTarget, 'Останавливаем', async () => {
      const { job } = await api(`/api/jobs/${entry.request_id}/cancel`, {});
      entry.job = job;
      entry.status = job.status;
      saveModelJobs();
      renderModelJobs();
    }));
    document.getElementById(`model-job-retry-${entry.request_id}`)?.addEventListener('click', event => busy(event.currentTarget, 'Отправляем', async () => {
      await submitModelJob(entry);
      await observeModelJob(entry);
    }));
    document.getElementById(`model-job-dismiss-${entry.request_id}`)?.addEventListener('click', () => {
      delete state.modelJobs[entry.request_id];
      saveModelJobs();
      renderModelJobs();
    });
  }
  updateModelActionControls();
}

async function submitModelJob(entry) {
  entry.status = 'submitting';
  entry.watchMessage = '';
  if (!saveModelJobs()) {
    entry.status = 'missing';
    entry.watchMessage = 'Не удалось сохранить задание в браузере. Запрос не отправлен.';
    renderModelJobs();
    throw new Error(entry.watchMessage);
  }
  renderModelJobs();
  try {
    const { job } = entry.path === '/api/web-check'
      ? await api('/api/web-check-jobs', { request_id: entry.request_id, ...entry.body })
      : await api('/api/model-jobs', { request_id: entry.request_id, path: entry.path, body: entry.body });
    if (job.id !== entry.request_id) throw new Error('Сервер вернул неожиданный идентификатор задания.');
    entry.job = job;
    entry.status = job.status;
    delete entry.submission_error;
    saveModelJobs();
    renderModelJobs();
  } catch (error) {
    entry.status = error.status >= 400 && error.status < 500 ? 'rejected' : 'delivery_unknown';
    entry.job = null;
    entry.submission_error = { message: error.message, status: error.status, code: error.code, details: error.details };
    entry.watchMessage = error.message;
    saveModelJobs();
    renderModelJobs();
    throw error;
  }
}

async function observeModelJob(entry, restored = false) {
  if (modelJobObservers.has(entry.request_id)) return modelJobObservers.get(entry.request_id);
  const observing = (async () => {
    try {
      while (true) {
        const { job } = await readJobWithRetry(entry.request_id, message => {
          entry.watchMessage = message;
          renderModelJobs();
        });
        entry.job = job;
        entry.status = job.status;
        entry.watchMessage = '';
        saveModelJobs();
        renderModelJobs();
        if (!ACTIVE_JOB_STATUSES.includes(job.status)) break;
        await new Promise(resolve => setTimeout(resolve, 1000));
      }
    } catch (error) {
      if (authError(error)) {
        entry.watchMessage = 'Войдите в аккаунт, чтобы восстановить наблюдение за заданием.';
        saveModelJobs();
        renderModelJobs();
        throw error;
      }
      entry.status = 'missing';
      entry.job = null;
      entry.watchMessage = error.message;
      saveModelJobs();
      renderModelJobs();
      throw error;
    }
    if (entry.body.retrieval_id) {
      entry.latestLoaded = Boolean(await loadCandidateJudgments(entry.body.retrieval_id, true));
      renderModelJobs();
    }
    if (entry.job.status === 'complete') {
      if (entry.path === '/api/web-check' && entry.job.result?.report) {
        const linked = /^#(?:home|check)\/([a-f0-9]{32})$/.exec(location.hash);
        if (restored && linked && linked[1] !== entry.job.result.report.id) return entry.job.result;
        if (entry.context?.surface === 'home' && state.homeText === entry.body.text) {
          state.homeReport = entry.job.result.report;
          if (state.view === 'home') renderHomeResult();
        }
        if (state.text === entry.job.result.report.text && state.view === 'check') acceptCheckReport(entry.job.result.report, entry.body.text);
        if (isSaas() && privateAccess()) await refreshSession().catch(() => {});
      }
      return entry.job.result;
    }
    const error = new Error(entry.job.error || 'Задание остановлено. Сохранённые результаты остаются доступны.');
    error.code = entry.job.code;
    error.details = entry.job.details;
    throw error;
  })();
  modelJobObservers.set(entry.request_id, observing);
  try { return await observing; }
  finally { modelJobObservers.delete(entry.request_id); }
}

async function modelRequest(path, body, context = {}) {
  if (isSaas()) {
    if (path === '/api/evaluate') throw new Error('Диагностическая оценка недоступна в общем сервисе.');
    if (!publicReportReady(context.report_id || body.report_id || state.report?.id)) {
      throw new Error('Сначала проверьте текущий текст. Смысловые операции доступны для сохранённого отчёта этого текста.');
    }
    if (path === '/api/compare') body = { ...body, report_id: state.report.id };
  }
  if (modelProvider() !== 'modal' && !isSaas()) return api(path, body);
  if (body.mode !== 'quote' && !modelAvailable()) {
    throw new Error('Отправка модельных заданий сейчас отключена. Проверьте настройки сервера.');
  }
  const pending = pendingModelRequest(path, body);
  if (pending) {
    if (['missing', 'delivery_unknown', 'rejected'].includes(pending.status)) {
      throw new Error('Отправка этого задания не подтверждена. Используйте кнопку повторной отправки в уведомлении.');
    }
    return observeModelJob(pending);
  }
  const actionKey = JSON.stringify([path, body.report_id, body.match_id, body.retrieval_id, body.candidate_id, body.source_id]);
  const generation = (modelRequestGenerations.get(actionKey) || 0) + 1;
  modelRequestGenerations.set(actionKey, generation);
  const requestId = crypto.randomUUID().replaceAll('-', '');
  if (isSaas()) body = { ...body, operation_id: requestId };
  const entry = { request_id: requestId, path, body, context, created_at: new Date().toISOString(), status: 'submitting' };
  state.modelJobs[requestId] = entry;
  await submitModelJob(entry);
  const result = await observeModelJob(entry);
  if (modelRequestGenerations.get(actionKey) !== generation) {
    const error = new Error('Сохранён ответ более раннего задания.');
    error.code = 'stale_result';
    throw error;
  }
  return result;
}

async function startWebCheck({ text = state.text, consent = state.internetConsent, surface = 'check' } = {}) {
  if (!webConfigured()) throw new Error('Проверка по интернету ещё не подключена. Подключите поисковый сервис перед запуском.');
  if (!text.trim()) throw new Error('Вставьте текст для проверки.');
  if (textLengthError(text)) throw new Error(textLengthError(text));
  if (!consent) throw new Error('Подтвердите отправку поисковых фрагментов для проверки по интернету.');
  if (isSaas() && chars(text).length > quotaRemaining()) throw new Error('Текст превышает остаток дневного лимита. Черновик сохранён целиком.');
  const body = { text, internet_consent: true };
  const pending = pendingModelRequest('/api/web-check', body);
  if (pending) throw new Error('Для этого текста уже есть задание. Его состояние и повторная отправка доступны в уведомлении.');
  const id = crypto.randomUUID().replaceAll('-', '');
  const entry = { request_id: id, path: '/api/web-check', body, context: { surface }, created_at: new Date().toISOString(), status: 'submitting' };
  state.modelJobs[id] = entry;
  await submitModelJob(entry);
  const { report } = await observeModelJob(entry);
  if (surface === 'home') {
    state.homeReport = report;
    if (state.view === 'home') renderHomeResult();
  } else acceptCheckReport(report, text);
}

function updateHomeControls() {
  const button = document.querySelector('#home-check-button');
  if (!button) return;
  const pending = pendingModelRequest('/api/web-check', { text: state.homeText, internet_consent: true });
  const length = chars(state.homeText).length;
  button.disabled = Boolean(button.dataset.busy) || !webConfigured() || !state.homeConsent || !state.homeText.trim()
    || Boolean(pending) || Boolean(isSaas() && length > quotaRemaining());
  const count = document.querySelector('#home-character-count');
  if (count) count.textContent = `${number(length)} знаков`;
  const status = document.querySelector('#home-search-status');
  if (status) status.textContent = webConfigured() ? 'Поиск страниц подключён' : 'Подключаем проверку по интернету';
  const setup = document.querySelector('#home-setup-search');
  if (setup) setup.hidden = webConfigured() || isSaas();
  const missing = document.querySelector('#home-search-unavailable');
  if (missing) missing.hidden = webConfigured();
  const quota = document.querySelector('#home-quota');
  if (quota) { quota.hidden = !isSaas(); quota.textContent = `Осталось ${number(quotaRemaining())} из ${number(state.session.quota?.limit || 6000)} знаков сегодня.`; }
  const progress = document.querySelector('#home-check-progress');
  if (progress) {
    progress.hidden = !pending;
    progress.textContent = pending ? `${pending.job?.stage || 'Состояние проверки сохранено'}. Подробности доступны в рабочем пространстве.` : '';
  }
  const stale = document.querySelector('#home-report-stale');
  if (stale) stale.hidden = state.homeReport?.text === state.homeText;
}

function renderHomeResult() {
  const area = document.querySelector('#home-report-result');
  if (!area) return;
  const report = state.homeReport;
  area.hidden = !report || report.scope !== 'web_search';
  if (area.hidden) return;
  setReportAddress('home', report);
  const selected = report.matches.find(match => match.id === state.homeMatch) || report.matches[0];
  area.innerHTML = `<section class="home-result-card">
    <div class="home-result-heading"><div><p class="home-section-label">Ваш результат</p><h2>${webUniqueness(report)}</h2><p>${webCheckComplete(report) ? 'уникальность по найденным страницам' : 'проверка неполная · окончательный процент не установлен'}</p></div><a href="#check/${escapeHTML(report.id)}" id="home-open-full-report">Открыть полный отчёт ↗</a></div>
    <p id="home-report-stale" class="warning" hidden>Это отчёт для предыдущего текста. Новая правка в поле сохранена отдельно.</p>
    ${webStatsHTML(report)}
    ${selected ? `<div class="card"><h3>Совпадения в вашем тексте</h3><div class="annotated-text">${annotatedText(report, selected.id)}</div>
      <p class="field-help">Нажмите на выделенный фрагмент, чтобы сверить его с источником.</p></div>
      <div class="comparison-columns home-source-comparison" id="home-fragments" tabindex="-1">
        <div><h3>Ваш фрагмент</h3><blockquote>${escapeHTML(selected.excerpt)}</blockquote></div>
        <div><h3>${escapeHTML(selected.source_title)}</h3><blockquote>${escapeHTML(selected.source_excerpt)}</blockquote>${safeLink(selected.source_url, 'Открыть источник')}</div>
      </div>` : `<p>${hasWebSources(report) ? 'На прочитанных страницах дословных совпадений не обнаружено.' : 'Поиск не дал страниц, по которым можно проверить текст.'}</p>`}
    ${webCoverageHTML(report)}
  </section>`;
  bindMatchSelection(area, id => {
    state.homeMatch = id;
    renderHomeResult();
    showComparison('#home-fragments');
  });
  document.querySelector('#home-open-full-report').addEventListener('click', () => {
    if (!state.text) saveDraft(report.text);
    state.report = report;
    state.selectedMatch = selected?.id || null;
    state.rewrite = null;
    state.judgment = null;
  });
  updateHomeControls();
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
  button.dataset.busy = 'true';
  button.disabled = true;
  button.innerHTML = `<span class="spinner" aria-hidden="true"></span>${escapeHTML(label)}`;
  try { await action(); }
  catch (error) { if (!['stale_result', 'session_changed'].includes(error.code)) toast(error.message, true); }
  finally {
    if (button.isConnected) {
      delete button.dataset.busy;
      button.disabled = false;
      button.innerHTML = original;
      updateModelActionControls();
      updateAuthButton();
    }
  }
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
  if (internetMode()) return `
    <div class="side-stack">
      <section class="card side-card"><h2>Область проверки <span class="scope-badge">Интернет</span></h2>
        <p>Поисковые фразы помогают найти страницы. Совпадения проверяются по загруженному тексту страниц и сопровождаются ссылками.</p>
        <p class="field-help">Поисковый сниппет сам по себе не считается доказательством совпадения.</p>
      </section>
      <aside class="hint-card"><h3>Что означает процент</h3><p>Доля слов без найденных дословных совпадений на доступных страницах. Закрытые сайты, пересказы и переводы могут остаться незамеченными.</p></aside>
      <p class="coverage-note">Если поиск или загрузка страниц завершатся с ошибками, итоговая уникальность будет помечена как неустановленная.</p>
    </div>`;
  return `
    <div class="side-stack">
      <section class="card side-card">
        <h2>Область проверки <span class="scope-badge">Библиотека</span></h2>
        <p>Сравниваем текст с материалами, которые вы добавили в библиотеку источников.</p>
        <p class="field-help" data-processing-disclosure>${escapeHTML(processingDisclosure())}</p>
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
        <span>Этот режим учитывает только вашу библиотеку. Для поиска страниц выберите вкладку «Интернет».</span>
      </p>
    </div>`;
}

function updateCharacterCount() {
  const count = document.querySelector('#character-count');
  if (count) count.textContent = `${number(chars(state.text).length)} знаков`;
  const wordCount = document.querySelector('#word-count');
  if (wordCount) wordCount.textContent = `${number(state.text.trim() ? state.text.trim().split(/\s+/u).length : 0)} слов`;
}
function textLengthError(text) {
  return chars(text).length > 200000 ? 'В тексте больше 200 000 знаков. Сократите его перед проверкой.' : '';
}

function validateTextLength(target) {
  const message = textLengthError(target.value);
  target.setCustomValidity(message);
  target.setAttribute('aria-invalid', message ? 'true' : 'false');
  const error = document.getElementById(target.id === 'check-text' ? 'check-error' : 'source-text-limit-error');
  if (error) {
    error.textContent = message;
    error.classList.toggle('inline-error', Boolean(message));
  }
  return !message;
}

function decodeTextFile(buffer) {
  const bytes = new Uint8Array(buffer);
  if (bytes.length >= 4 && ((bytes[0] === 0xff && bytes[1] === 0xfe && bytes[2] === 0 && bytes[3] === 0)
    || (bytes[0] === 0 && bytes[1] === 0 && bytes[2] === 0xfe && bytes[3] === 0xff))) {
    throw new Error('Кодировка UTF-32 пока не поддерживается. Сохраните текстовый файл в UTF-8.');
  }
  const utf8BOM = bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf;
  let encoding = 'utf-8';
  if (bytes[0] === 0xff && bytes[1] === 0xfe) encoding = 'utf-16le';
  if (bytes[0] === 0xfe && bytes[1] === 0xff) encoding = 'utf-16be';
  let text;
  let guessed = false;
  try {
    text = new TextDecoder(encoding, { fatal: true }).decode(bytes);
  } catch {
    if (utf8BOM) {
      throw new Error('Не удалось прочитать файл UTF-8. Проверьте его кодировку и загрузите снова.');
    }
    if (encoding !== 'utf-8') {
      throw new Error('Не удалось прочитать файл UTF-16. Сохраните его в UTF-8 и загрузите снова.');
    }
    try {
      text = new TextDecoder('windows-1251', { fatal: true }).decode(bytes);
      encoding = 'windows-1251';
      guessed = true;
    } catch {
      throw new Error('Не удалось прочитать кодировку файла. Сохраните текст в UTF-8 и загрузите снова.');
    }
  }
  if (/[\u0000-\u0008\u000b\u000e-\u001f\u007f-\u009f]/u.test(text)) {
    throw new Error('Файл содержит двоичные данные или неподдерживаемую кодировку. Сохраните обычный текст в UTF-8.');
  }
  if (chars(text).length > 200000) {
    throw new Error('В файле больше 200 000 знаков. Выберите более короткий текст.');
  }
  return { text, encoding, guessed };
}

function showImportHint(target, decoded) {
  const hint = document.getElementById(`${target.id}-import-hint`);
  if (!hint) return;
  hint.hidden = !decoded.guessed;
  hint.textContent = decoded.guessed
    ? 'Предположительно, файл сохранён в Windows-1251. Проверьте, правильно ли отображается текст.'
    : '';
}

async function readTextFile(input, target) {
  const file = input.files[0];
  if (!file) return;
  input.value = '';
  const token = {};
  fileReadTokens.set(target, token);
  if (!/\.(txt|md)$/i.test(file.name)) {
    throw new Error('В этой версии доступны текстовые файлы .txt и .md.');
  }
  if (file.size > 800004) {
    throw new Error('Файл слишком большой. Используйте текст до 200 000 знаков.');
  }
  const previousValue = target.value;
  let editedWhileReading = false;
  const onInput = () => { editedWhileReading = true; };
  target.addEventListener('input', onInput);
  let decoded;
  try {
    const buffer = await file.arrayBuffer();
    if (!target.isConnected || editedWhileReading || target.value !== previousValue
      || fileReadTokens.get(target) !== token) return;
    decoded = decodeTextFile(buffer);
  } finally {
    target.removeEventListener('input', onInput);
  }
  target.value = decoded.text;
  showImportHint(target, decoded);
  target.dispatchEvent(new Event('input', { bubbles: true }));
}

function scrollHomeSection(id) {
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  document.getElementById(id).scrollIntoView({ behavior: reduced ? 'auto' : 'smooth', block: 'start' });
}

function renderHome() {
  const maxQueries = Number.isInteger(webStatus().max_queries) ? webStatus().max_queries : 6;
  const maxUrls = Number.isInteger(webStatus().max_urls) ? webStatus().max_urls : 12;
  main.innerHTML = `
    <div class="home-page home-v2">
      <header class="home-nav">
        <a class="home-brand" href="#home" aria-label="Исток, главная">исток<span class="home-brand-dot">.</span></a>
        <nav aria-label="Главная навигация"><a href="#home" data-home-scroll="home-check">Проверить текст</a><a href="#home" data-home-scroll="home-how">Как это работает</a><a href="#home" data-home-scroll="home-scope">Что входит в оценку</a></nav>
        <a class="home-nav-cta" href="#check">Рабочее пространство ↗</a>
      </header>
      <section class="home-v2-hero" id="home-check">
        <div class="home-orbit home-orbit-one" aria-hidden="true"></div><div class="home-orbit home-orbit-two" aria-hidden="true"></div>
        <div class="home-v2-copy">
          <p class="home-kicker">Исток · проверка текста с источниками</p>
          <h1 class="home-v2-title" aria-label="Проверьте уникальность текста"><span data-reveal>Проверьте</span><span data-reveal>уникальность</span><span data-reveal>текста<span class="home-title-dot" aria-hidden="true">.</span></span></h1>
          <p class="home-v2-description">Найдите страницы с совпадениями.<br>Сверьте фрагменты. Сохраните ссылки.</p>
          <p class="home-v2-status" id="home-search-status">${webConfigured() ? 'Поиск страниц подключён' : 'Подключаем проверку по интернету'}</p>
          ${state.text.trim() ? '<p class="home-existing-draft">Есть черновик в рабочем пространстве. <a href="#check">Продолжить его ↗</a></p>' : ''}
        </div>
        <form class="home-checker" id="home-check-form">
          <div class="home-checker-heading"><label for="home-text">Ваш текст</label><label class="home-file-button">Загрузить TXT / MD<input id="home-file" type="file" accept=".txt,.md,text/plain,text/markdown" aria-label="Загрузить текст для интернет-проверки"></label></div>
          <textarea id="home-text" aria-label="Текст для проверки уникальности" placeholder="Вставьте статью, заметку или абзац, который хотите проверить…"></textarea>
          <p id="home-text-import-hint" class="field-help" role="status" hidden></p>
          <div class="home-input-meta"><span id="home-character-count">0 знаков</span><span>С пробелами</span></div>
          <label class="home-consent"><input id="home-consent" type="checkbox">Разрешаю отправить поисковые фрагменты этого текста внешнему поисковому сервису для поиска источников.</label>
          <button class="home-primary" id="home-check-button" type="submit" disabled>Проверить уникальность <span aria-hidden="true">↗</span></button>
          <p class="home-quota" id="home-quota" hidden></p>
          <p class="home-connect-note" id="home-search-unavailable" ${webConfigured() ? 'hidden' : ''}>Интернет-поиск пока не подключён к этому экземпляру. Кнопка станет доступна после подключения сервиса.</p>
          <a class="home-setup-link" id="home-setup-search" href="/setup-search.html" ${isSaas() || webConfigured() ? 'hidden' : ''}>Подключить поисковый сервис ↗</a>
          <p class="home-check-progress" id="home-check-progress" role="status" hidden></p>
          <p class="inline-error" id="home-check-error" role="alert"></p>
          <a class="home-secondary-check" id="home-library-link" href="#check">Или открыть проверку по своей библиотеке ↗</a>
        </form>
      </section>
      <div id="home-report-result" class="home-report-result" hidden></div>
      <div class="home-process-ribbon" aria-hidden="true"><div>Найти страницу <span>↗</span> Сверить текст <span>↗</span> Открыть источник <span>↗</span> Найти страницу <span>↗</span></div></div>
      <section class="home-v2-how home-section" id="home-how">
        <div class="home-section-heading"><p class="home-section-label" data-reveal>От текста к источнику</p><h2 data-reveal>Сначала совпадения.<br>Потом процент.</h2></div>
        <div class="home-process-rows">
          <article data-reveal><span>01</span><div><h3>Ищем страницы</h3><p>Составляем поисковые фразы из текста и получаем адреса возможных источников.</p></div></article>
          <article data-reveal><span>02</span><div><h3>Сверяем содержимое</h3><p>Загружаем доступные страницы. Проверяем совпадения по их тексту, а не по поисковому сниппету.</p></div></article>
          <article data-reveal><span>03</span><div><h3>Показываем, что совпало</h3><p>В отчёте остаются фрагменты, ссылки и результат проверки. Его можно скачать для чтения.</p></div></article>
        </div>
      </section>
      <section class="home-v2-scope home-section" id="home-scope">
        <div data-reveal><p class="home-section-label">Понятные границы</p><h2>Процент со ссылками.<br>И с объяснением.</h2><p class="home-scope-copy">Уникальность — доля слов без найденных дословных совпадений на доступных страницах. Закрытые сайты, переводы и глубокие пересказы могут остаться незамеченными. Если часть поиска не завершилась, мы покажем это в отчёте.</p></div>
        <div class="home-search-limits" data-reveal>
          <div role="group" aria-label="До ${maxQueries} поисковых запросов за проверку"><small>До</small><strong data-count="${maxQueries}" aria-hidden="true">${maxQueries}</strong><span>поисковых запросов за проверку</span></div>
          <div role="group" aria-label="До ${maxUrls} страниц для сверки"><small>До</small><strong data-count="${maxUrls}" aria-hidden="true">${maxUrls}</strong><span>страниц для сверки текста</span></div>
        </div>
        <p class="home-processing-note">Для интернет-проверки поисковые фрагменты передаются внешнему поисковому сервису. Смысловая сверка с Qwen — отдельный дополнительный инструмент.</p>
      </section>
      <section class="home-v2-library home-section" data-reveal>
        <p class="home-section-label">Для собственных материалов</p><h2>Есть своя библиотека?</h2><p>Сравнивайте текст с выбранными публикациями отдельно от интернет-поиска. В вашем рабочем пространстве <strong id="home-library-count">${number(state.sources.length)}</strong> материалов.</p><a href="#sources">Открыть библиотеку ↗</a>
      </section>
      <footer class="home-footer"><a class="home-brand" href="#home">исток<span class="home-brand-dot">.</span></a><span>У каждого совпадения есть источник</span><a href="#home" data-home-scroll="home-check">К проверке ↑</a></footer>
    </div>`;
  const field = document.querySelector('#home-text');
  field.value = state.homeText;
  document.querySelector('#home-consent').checked = state.homeConsent;
  field.addEventListener('input', () => {
    state.homeText = field.value;
    try { storageSet(HOME_DRAFT_KEY, field.value); } catch { /* The current field remains available. */ }
    document.querySelector('#home-check-error').textContent = '';
    updateHomeControls();
  });
  document.querySelector('#home-consent').addEventListener('change', event => { state.homeConsent = event.target.checked; updateHomeControls(); });
  document.querySelector('#home-file').addEventListener('change', async event => {
    try { await readTextFile(event.target, field); } catch (error) { document.querySelector('#home-check-error').textContent = error.message; }
  });
  document.querySelector('#home-check-form').addEventListener('submit', event => {
    event.preventDefault();
    return busy(document.querySelector('#home-check-button'), 'Ищем источники…', async () => {
      try { await startWebCheck({ text: state.homeText, consent: state.homeConsent, surface: 'home' }); }
      catch (error) { if (document.querySelector('#home-check-error')) document.querySelector('#home-check-error').textContent = error.message; throw error; }
      finally { updateHomeControls(); }
    });
  });
  document.querySelector('#home-library-link').addEventListener('click', () => { state.checkMode = 'library'; state.checkModeChosen = true; });
  main.querySelectorAll('[data-home-scroll]').forEach(link => link.addEventListener('click', event => { event.preventDefault(); scrollHomeSection(link.dataset.homeScroll); }));
  renderHomeResult();
  updateHomeControls();
  window.IstokMotion?.mount(main);
}

function renderHomeDemo() {
  const area = document.querySelector('#home-demo-result');
  if (!area) return;
  const report = state.homeReport;
  if (!report) {
    area.innerHTML = `
      <div class="home-demo-before">
        <div><p class="home-demo-column-label">ФРАГМЕНТ СТАТЬИ</p><p>Городские сады помогают жителям выращивать овощи рядом с домом и знакомиться с соседями.</p></div>
        <div><p class="home-demo-column-label">УЧЕБНЫЙ ИСТОЧНИК</p><h3>Городские сады</h3><p>Городские сады помогают жителям выращивать овощи рядом с домом и знакомиться с соседями.</p></div>
      </div>
      <div class="home-demo-result-footer"><span>Нажмите «Проверить этот пример», чтобы получить результат поиска.</span><span aria-hidden="true">↗</span></div>`;
    return;
  }
  const selected = report.matches.find(match => match.id === state.homeMatch) || report.matches[0];
  area.innerHTML = `
    <div class="home-demo-matches" role="group" aria-label="Найденные учебные источники">
      ${report.matches.map((match, index) => `<button type="button" data-home-match="${escapeHTML(match.id)}" aria-pressed="${match.id === selected?.id}">${index + 1}. ${escapeHTML(match.source_title.replace('Учебный пример · ', ''))}</button>`).join('')}
    </div>
    ${selected ? `
      <div class="home-demo-comparison">
        <div><p class="home-demo-column-label">НАЙДЕННЫЙ ФРАГМЕНТ СТАТЬИ</p><p class="home-demo-excerpt"><mark>${escapeHTML(selected.excerpt)}</mark></p></div>
        <div><p class="home-demo-column-label">МАТЕРИАЛ БИБЛИОТЕКИ</p><h3>${escapeHTML(selected.source_title.replace('Учебный пример · ', ''))}</h3><p class="home-demo-excerpt">${escapeHTML(selected.source_excerpt)}</p></div>
      </div>` : '<div class="home-demo-no-matches">В учебном тексте не найдены лексические совпадения.</div>'}
    <div class="home-demo-result-footer"><span>Рабочий поиск завершён. Учебный отчёт сохранён в истории.</span><a href="#history">Открыть историю <span aria-hidden="true">↗</span></a></div>`;
  area.querySelectorAll('[data-home-match]').forEach(button => {
    button.addEventListener('click', () => { state.homeMatch = button.dataset.homeMatch; renderHomeDemo(); });
  });
}

async function runHomeDemo() {
  if (isSaas()) {
    location.hash = '#check';
    toast('Учебный текст можно открыть кнопкой «Попробовать на примере». Проверка расходует дневной лимит.');
    return;
  }
  if (state.homeDemoRunning) return;
  state.homeDemoRunning = true;
  try {
    const demo = await api('/api/demo', {});
    await refreshSources();
    const { report } = await api('/api/check', { text: demo.text, source_ids: demo.sources.map(source => source.id) });
    state.homeReport = report;
    state.homeMatch = report.matches[0]?.id || null;
    if (state.view === 'home') renderHomeDemo();
    else toast('Учебный отчёт готов и сохранён в истории.');
  } finally {
    state.homeDemoRunning = false;
  }
}

function renderCheck() {
  main.innerHTML = heading(
    'Проверьте уникальность текста.',
    'Выберите область проверки. Получите найденные фрагменты и ссылки на источники.',
    '<button class="text-button" id="load-demo" type="button">Попробовать на примере <span aria-hidden="true">↗</span></button>',
    'Интернет и ваша библиотека'
  ) + `
    <div class="check-modes" role="tablist" aria-label="Область проверки">
      <button class="mode-tab" id="mode-internet" role="tab" type="button" aria-controls="check-mode-description" aria-selected="false">Интернет</button>
      <button class="mode-tab" id="mode-library" role="tab" type="button" aria-controls="check-mode-description" aria-selected="true">Библиотека</button>
    </div>
    <p class="mode-description" id="check-mode-description" role="tabpanel"></p>
    <div class="warning web-availability" id="web-availability">Проверка уникальности по интернету ещё не подключена. Для неё нужен поисковый сервис.
      <a href="/setup-search.html" id="setup-search-link">Подключить поиск в этом локальном экземпляре ↗</a>
    </div>
    <div class="workspace-grid">
      <div>
        <section class="card editor-card">
          <div class="editor-header">
            <div class="editor-title"><span class="document-icon" aria-hidden="true">▤</span>Ваш текст</div>
            <label class="file-button">↑ Загрузить .txt
              <input id="text-file" type="file" accept=".txt,.md,text/plain,text/markdown" aria-label="Загрузить текстовый файл">
            </label>
          </div>
          <p class="import-hint" id="check-text-import-hint" role="status" hidden></p>
          <textarea class="editor-text" id="check-text" aria-label="Текст для проверки"
            placeholder="Вставьте текст, который хотите проверить…&#10;&#10;Добавьте источники в библиотеку или начните с готового примера."></textarea>
          <div class="editor-meta">
            <span><span id="character-count">0 знаков</span> · <span id="word-count">0 слов</span></span>
            <span>${isSaas() ? 'До 6 000 знаков в сутки' : 'До 200 000 знаков'}</span>
          </div>
          <div class="internet-options" id="internet-options" hidden>
            <label><input id="internet-consent" type="checkbox"> Разрешаю отправить поисковые фрагменты этого текста внешнему поисковому сервису для поиска страниц-источников.</label>
          </div>
          <div class="editor-footer">
            <p id="draft-status">Черновик сохраняется на этом Mac</p>
            <button class="button" id="check-button" type="button">Проверить текст <span aria-hidden="true">↗</span></button>
          </div>
        </section>
        <p class="field-help quota-note" id="check-quota-note" hidden></p>
        ${isSaas() ? '<p class="field-help quota-note">До 3 проверок, 60 сравнений модели и 3 правок в сутки. Один поиск кандидатов на отчёт. Смысловые действия доступны после проверки неизменённого текста.</p>' : ''}
        <section id="check-requests" aria-label="Сохранённые отправки проверок" hidden></section>
        <div id="check-error" role="alert"></div>
        <section class="card direct-compare" id="library-tools">
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
      <div id="check-scope">${scopeAside()}</div>
    </div>
    <section class="report-section" id="report-area" aria-live="polite"></section>`;
  const textarea = document.querySelector('#check-text');
  textarea.value = state.text;
  textarea.addEventListener('input', () => {
    saveDraft(textarea.value);
    updateCharacterCount();
    validateTextLength(textarea);
    updateModelActionControls();
  });
  updateCharacterCount();
  validateTextLength(textarea);
  updateDraftStatus();
  for (const [id, mode] of [['mode-internet', 'internet'], ['mode-library', 'library']]) {
    document.getElementById(id).addEventListener('click', () => {
      state.checkMode = mode;
      state.checkModeChosen = true;
      updateCheckModeUI();
    });
    document.getElementById(id).addEventListener('keydown', event => {
      if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
      event.preventDefault();
      state.checkMode = mode === 'internet' ? 'library' : 'internet';
      state.checkModeChosen = true;
      document.getElementById(mode === 'internet' ? 'mode-library' : 'mode-internet').focus();
      updateCheckModeUI();
    });
  }
  document.querySelector('#internet-consent').addEventListener('change', event => {
    state.internetConsent = event.target.checked;
    updateQuotaUI();
  });
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
    updateModelActionControls();
  });
  document.querySelector('#compare-button').addEventListener('click', event => busy(event.currentTarget, 'Qwen сравнивает', async () => {
    if (!state.text.trim()) throw new Error('Вставьте текст для сравнения.');
    const sourceId = document.querySelector('#compare-source').value;
    if (!sourceId) throw new Error('Выберите источник из библиотеки.');
    const text = state.text;
    const revision = state.viewRevision;
    const resultArea = document.querySelector('#direct-judgment');
    const { judgment } = await modelRequest('/api/compare', { text, source_id: sourceId, model: selectedModel() });
    if (!resultArea.isConnected || state.viewRevision !== revision || state.text !== text
      || document.querySelector('#compare-source')?.value !== sourceId) return;
    resultArea.innerHTML = judgmentHTML(judgment);
  }));
  renderReport();
  renderCheckRequests();
  updateCheckModeUI();
  updateModelActionControls();
}
async function checkText() {
  if (!state.text.trim()) throw new Error('Вставьте текст для проверки.');
  if (textLengthError(state.text)) throw new Error(textLengthError(state.text));
  if (internetMode()) return startWebCheck();
  if (!state.sources.length) throw new Error('Сначала добавьте хотя бы один источник или откройте готовый пример.');
  const text = state.text;
  if (isSaas()) {
    const previous = matchingCheckRequest(text);
    if (previous?.status === 'complete') {
      const { report } = await api(`/api/reports/${encodeURIComponent(previous.report_id || previous.request_id)}`);
      acceptCheckReport(report, text);
      return;
    }
    if (previous) throw new Error('Для этого текста есть сохранённая отправка. Используйте её кнопку повторной отправки.');
    if (chars(text).length > quotaRemaining()) throw new Error('Текст превышает остаток дневного лимита. Он сохранён целиком в редакторе.');
    const id = crypto.randomUUID().replaceAll('-', '');
    const entry = { request_id: id, text, source_ids: state.sources.map(source => source.id).sort(), status: 'pending' };
    state.checkRequests[id] = entry;
    return sendCheckRequest(entry);
  }
  const { report } = await api('/api/check', { text });
  acceptCheckReport(report, text);
}

function acceptCheckReport(report, text) {
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
function annotatedText(report, selectedId = state.selectedMatch) {
  const text = chars(report.text);
  const boundaries = new Set([0, text.length]);
  for (const match of report.matches) { boundaries.add(match.start); boundaries.add(match.end); }
  const ordered = [...boundaries].sort((a, b) => a - b);
  const output = [];
  for (let index = 0; index < ordered.length - 1; index++) {
    const start = ordered[index], end = ordered[index + 1];
    const covering = report.matches.filter(match => match.start < end && match.end > start);
    const value = escapeHTML(text.slice(start, end).join(''));
    const selected = covering.find(match => match.id === selectedId);
    const match = selected || covering[0];
    output.push(match ? `<mark tabindex="0" role="button" aria-label="Показать совпадение с ${escapeHTML(match.source_title)}" data-highlight="${escapeHTML(match.id)}" class="${selected ? 'selected' : ''} ${match.kind === 'similar' ? 'fuzzy' : ''}">${value}</mark>` : value);
  }
  return output.join('');
}
function matchCardHTML(match, index) {
  const selected = match.id === state.selectedMatch;
  const label = reviewLabel(match.id);
  return `<div class="match-entry">
    <button class="match-item ${selected ? 'selected' : ''}" data-match="${escapeHTML(match.id)}" type="button">
      <div class="match-kicker">
        <span>Фрагмент ${index + 1}</span>
        <span class="match-kind ${match.kind}">${match.kind === 'exact' ? 'Дословно' : 'Похожая формулировка'}</span>
      </div>
      <strong>${escapeHTML(match.source_title)}</strong>
      <p>${escapeHTML(match.excerpt.slice(0, 120))}${match.excerpt.length > 120 ? '…' : ''}</p>
      ${match.quoted ? '<div class="subtle">Есть признаки оформления цитаты</div>' : ''}
      ${label ? `<div class="finding-badge ${state.report.reviews[match.id].decision}">${escapeHTML(label)}</div>` : ''}
    </button>
    ${state.report?.scope === 'web_search' ? safeLink(match.source_url, 'Открыть страницу-источник') : ''}
    </div>`;
}

function webSourceCount(report) {
  const counts = [report.web?.sources_checked, report.source_snapshots_count, report.stats?.checked_source_count];
  if (counts.includes(0)) return 0;
  return counts.find(value => Number.isInteger(value) && value > 0);
}

function hasWebSources(report) {
  return webSourceCount(report) > 0;
}

function webCheckComplete(report) {
  return hasWebSources(report) && (report.web ? report.web.status === 'complete' && !report.web.errors?.length : report.scope === 'web_search');
}

function webUniqueness(report) {
  const value = report.stats?.uniqueness_percent;
  if (webCheckComplete(report) && Number.isFinite(value) && value >= 0 && value <= 100) {
    return `${value.toLocaleString('ru-RU', { maximumFractionDigits: 1 })}%`;
  }
  const upper = report.stats?.uniqueness_upper_bound_percent;
  return hasWebSources(report) && Number.isFinite(upper) && upper >= 0 && upper <= 100
    ? `Не выше ${upper.toLocaleString('ru-RU', { maximumFractionDigits: 1 })}%` : 'Не установлена';
}

function webStatsHTML(report) {
  const stats = report.stats || {};
  const checked = webSourceCount(report);
  const validWords = hasWebSources(report) && Number.isInteger(stats.total_words) && stats.total_words > 0
    && Number.isInteger(stats.matched_words) && stats.matched_words >= 0 && stats.matched_words <= stats.total_words;
  const percent = validWords ? (100 * stats.matched_words / stats.total_words).toLocaleString('ru-RU', { maximumFractionDigits: 1 }) + '%' : '—';
  return `<div class="stats-row web-stats">
    <div class="stat-card"><strong>${percent}</strong><span>слов с подтверждёнными дословными совпадениями${validWords ? ` · ${number(stats.matched_words)} из ${number(stats.total_words)}` : ''}</span></div>
    <div class="stat-card"><strong>${Number.isInteger(checked) ? number(checked) : '—'}</strong><span>страниц прочитано</span></div>
    <div class="stat-card"><strong class="coverage-status">${webCheckComplete(report) ? 'Завершена' : 'Неполная'}</strong><span>проверка найденных страниц</span></div></div>`;
}

function showComparison(selector) {
  const details = document.querySelector(selector);
  if (!details) return;
  details.focus({ preventScroll: true });
  details.scrollIntoView({ block: 'start', behavior: 'instant' });
}

function bindMatchSelection(area, select) {
  area.querySelectorAll('[data-match], [data-highlight]').forEach(element => {
    const handle = () => select(element.dataset.match || element.dataset.highlight);
    element.addEventListener('click', handle);
    if (element.tagName === 'MARK') {
      element.addEventListener('keydown', event => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          handle();
        }
      });
    }
  });
}

function setReportAddress(surface, report) {
  if (state.view === surface && /^[a-f0-9]{32}$/.test(report.id) && location.hash !== `#${surface}/${report.id}`) {
    window.history?.replaceState(null, '', `#${surface}/${report.id}`);
  }
}

function webCoverageHTML(report) {
  const web = report.web || {};
  const replay = report.offline_replay && typeof report.offline_replay === 'object';
  const savedPages = replay && report.offline_replay.fresh_page_load_attempts === 0;
  return `<section class="card web-coverage">
    <h3>${webCheckComplete(report) ? 'Проверка найденных страниц завершена' : 'Проверка завершена не полностью'}</h3>
    ${replay ? `<p class="warning">Повтор сохранённых данных: используется прежняя поисковая выдача.${savedPages ? ' Источники проверены по сохранённым снимкам.' : ''}</p>` : ''}
    <p>${replay ? 'Использовано сохранённых поисковых ответов' : 'Поисковых запросов выполнено'} ${number(web.queries_completed)} из ${number(web.queries_planned)}.
      ${savedPages ? 'Проверено снимков страниц' : 'Проверено страниц'} ${number(web.sources_checked)}. ${replay ? 'Обработано адресов' : 'Попыток загрузки'} ${number(web.urls_attempted)}.</p>
    <p class="field-help">Уникальность — доля слов без найденных дословных совпадений в загруженных страницах. Поисковые сниппеты не используются как доказательство. Оценка Qwen в этот процент не входит.</p>
    ${web.status !== 'complete' && Number.isFinite(report.stats?.uniqueness_upper_bound_percent) ? '<p class="field-help">По найденным совпадениям указана верхняя граница уникальности. Проверка остальных страниц неполная, окончательный процент не установлен.</p>' : ''}
    ${web.errors?.length ? `<details><summary>Не удалось проверить · ${number(web.errors.length)}</summary><ul>${web.errors.map(error => `<li>${escapeHTML(typeof error === 'string' ? error : error.message || 'Источник недоступен')} ${typeof error === 'object' ? safeLink(error.url) : ''}</li>`).join('')}</ul></details>` : ''}
    <ul class="field-help">${(web.limitations || ['Оценка относится к найденным доступным страницам, а не ко всему интернету.']).map(value => `<li>${escapeHTML(value)}</li>`).join('')}</ul>
  </section>`;
}

function renderReport() {
  const area = document.querySelector('#report-area');
  if (!area) return;
  if (!state.report) {
    area.innerHTML = empty('Здесь появится отчёт', 'После проверки можно открыть каждое совпадение, увидеть источник и предложить правку.');
    return;
  }
  const report = state.report;
  setReportAddress('check', report);
  const stats = report.stats;
  const matches = report.matches;
  const webReport = report.scope === 'web_search';
  const selected = matches.find(match => match.id === state.selectedMatch);
  area.innerHTML = `
    <div id="stale-report" class="warning" hidden>
      Текст в редакторе изменён. Этот отчёт относится к предыдущему варианту.
      Повторите проверку перед применением правок.
      <button class="text-button" id="restore-report-text" type="button">Открыть текст отчёта в редакторе</button>
    </div>
    <div class="section-heading">
      <div><h2>${webReport ? 'Проверка уникальности по интернету' : 'Фрагменты для сверки'}</h2><p>${webReport ? 'Найденные доступные страницы' : 'По библиотеке источников'} · ${escapeHTML(date(report.created_at))}</p></div>
      <div class="action-row">
        <button class="text-button" id="download-reading-report" type="button">↓ Отчёт для чтения</button>
        <button class="text-button" id="download-report" type="button">↓ Данные JSON</button>
      </div>
    </div>
    ${webReport ? `<p class="web-score-label"><strong>${webUniqueness(report)}</strong> · ${webCheckComplete(report) ? 'уникальность по найденным страницам' : 'проверка неполная · окончательный процент не установлен'}</p>${webStatsHTML(report)}` : `
    <div class="stats-row">
      <div class="stat-card"><strong>${Number(stats.similarity_percent || 0).toLocaleString('ru-RU', { maximumFractionDigits: 1 })}%</strong><span>текста с лексическими совпадениями</span></div>
      <div class="stat-card"><strong>${number(matches.length)}</strong><span>найдено фрагментов</span></div>
      <div class="stat-card"><strong>${number(stats.source_count)}</strong><span>источников с совпадениями</span></div>
    </div>`}
    ${webReport ? webCoverageHTML(report) : ''}
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
        webReport ? (hasWebSources(report) ? 'На прочитанных страницах совпадений не найдено' : 'Не удалось прочитать страницы для сравнения') : 'Лексических совпадений в библиотеке не найдено',
        webReport ? 'Это результат ограниченного поиска. Недоступные страницы, пересказы и переводы могут остаться незамеченными.' : 'Глубокие пересказы могут остаться незамеченными. Для сравнения с конкретным источником используйте Qwen.',
        '⌕'
      )}
    <div id="comparison-area" tabindex="-1">${selected ? comparisonHTML(selected) : ''}</div>
    <section class="retrieval-section" id="retrieval-area"></section>`;
  bindMatchSelection(area, id => {
    state.selectedMatch = id;
    state.rewrite = null;
    state.judgment = null;
    renderReport();
    showComparison('#comparison-area');
  });
  document.querySelector('#download-report').addEventListener('click', () => {
    download(`istok-report-${report.id}.json`, JSON.stringify(report, null, 2), 'application/json');
  });
  document.querySelector('#download-reading-report').addEventListener('click', event => busy(event.currentTarget, 'Готовим отчёт', async () => {
    const { filename, html } = await api(`/api/report-exports/${encodeURIComponent(report.id)}`);
    download(filename, html, 'text/html;charset=utf-8');
  }));
  document.querySelector('#restore-report-text').addEventListener('click', () => {
    saveDraft(report.text);
    document.querySelector('#check-text').value = state.text;
    updateCharacterCount();
    validateTextLength(document.querySelector('#check-text'));
    toast('Текст сохранённого отчёта открыт в редакторе.');
  });
  bindComparison(selected);
  updateReportFreshness();
  if (!webReport) { renderRetrieval(); loadRetrieval(report.id); }
  updateModelActionControls();
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
  const running = candidateCheckRunning(retrieval.id) || candidateHasModelJob(retrieval.id, candidate.id);
  return `
    <button class="button small secondary" data-candidate-action="judge" type="button" ${modelAvailable() && !running ? '' : 'disabled'}>Сравнить с Qwen</button>
    ${positive ? `
      <button class="text-button" data-candidate-action="quote" type="button" ${running ? 'disabled' : ''}>Оформить цитату</button>
      <button class="text-button" data-candidate-action="paraphrase" type="button" ${modelAvailable() && !running ? '' : 'disabled'}>Предложить пересказ</button>
    ` : ''}`;
}

function candidateNeedsReview(error) {
  return Boolean(error && typeof error === 'object' && error.code === 'unconfirmed_exact');
}

function candidateErrorHTML(error) {
  if (!error) return '';
  const message = typeof error === 'string' ? error : error.message || error.error || 'Не удалось завершить сравнение.';
  if (!candidateNeedsReview(error)) return `<div class="inline-error">${escapeHTML(message)}</div>`;
  const judgment = error.details?.final_judgment;
  return `
    <section class="candidate-uncertain" role="status">
      <h3>Нужна проверка редактора</h3>
      <p>${escapeHTML(message)}</p>
      <p>Модель сообщила о дословном совпадении. Программная проверка точного совпадения это не подтвердила.</p>
      ${judgment?.explanation ? `<p class="uncertain-explanation">Объяснение модели · ${escapeHTML(judgment.explanation)}</p>` : ''}
      ${judgment?.evidence_text && judgment?.evidence_source ? `
        <div class="comparison-columns">
          <div><h3>Выдержка из проверяемого текста</h3><blockquote>${escapeHTML(judgment.evidence_text)}</blockquote></div>
          <div><h3>Выдержка из источника</h3><blockquote>${escapeHTML(judgment.evidence_source)}</blockquote></div>
        </div>` : ''}
    </section>`;
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
      <div class="candidate-error">${candidateErrorHTML(state.candidateErrors[candidateKey(retrieval, candidate)])}</div>
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
    if (state.view === 'check' && state.report?.id === reportId) renderRetrieval();
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

async function readJobWithRetry(jobId, onRetry) {
  let retries = 0;
  while (true) {
    try {
      return await api(`/api/jobs/${encodeURIComponent(jobId)}`);
    } catch (error) {
      if (error.status === 404) {
        error.message = 'Задание больше недоступно. Возможно, приложение было перезапущено. Сохранённые результаты остаются доступны.';
        throw error;
      }
      if (error.status >= 400 && error.status < 500) throw error;
      onRetry('Потеряна связь, пытаемся восстановить');
      const delay = Math.min(1000 * 2 ** Math.min(retries, 4), 15000);
      retries++;
      await new Promise(resolve => setTimeout(resolve, delay));
    }
  }
}

async function pollRetrieval(reportId, jobId) {
  if (retrievalPolls.has(reportId)) return;
  retrievalPolls.add(reportId);
  try {
    while (retrievalRunning(reportId)) {
      updateRetrievalJobStatus(reportId);
      await new Promise(resolve => setTimeout(resolve, 1000));
      const { job } = await readJobWithRetry(jobId, message => {
        state.retrievalJobs[reportId].stage = message;
        updateRetrievalJobStatus(reportId);
      });
      state.retrievalJobs[reportId] = job;
      saveActiveJobs(RETRIEVAL_JOBS_KEY, state.retrievalJobs);
    }
    const finished = state.retrievalJobs[reportId];
    if (finished.status === 'complete') {
      state.retrievals[reportId] = finished.result;
      toast('Поиск источников завершён. Кандидаты готовы для сверки.');
    }
  } catch (error) {
    if (authError(error)) return;
    state.retrievalJobs[reportId] = { status: 'failed', error: error.message };
    delete state.retrievals[reportId];
    await loadRetrieval(reportId);
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
    if (!publicReportReady(reportId)) throw new Error('Откройте в редакторе текст этого отчёта перед поиском.');
    const operationKey = `retrieval:${reportId}:${mode}`;
    const body = { report_id: reportId, mode, top_k: 5 };
    if (isSaas()) body.operation_id = publicOperationId(operationKey);
    const { job } = await api('/api/retrieval-jobs', body);
    state.retrievalJobs[reportId] = job;
    if (saveActiveJobs(RETRIEVAL_JOBS_KEY, state.retrievalJobs)) acknowledgeOperation(operationKey);
    if (state.view === 'check' && state.report?.id === reportId) renderRetrieval();
    await pollRetrieval(reportId, job.id);
  } catch (error) {
    state.retrievalJobs[reportId] = { status: 'failed', error: error.message };
    saveActiveJobs(RETRIEVAL_JOBS_KEY, state.retrievalJobs);
    if (state.view === 'check' && state.report?.id === reportId) renderRetrieval();
  }
}

function invalidateCandidateLoads(retrievalId) {
  state.candidateGenerations[retrievalId] = (state.candidateGenerations[retrievalId] || 0) + 1;
}

function mergeCandidateOutcomes(retrievalId, entries) {
  invalidateCandidateLoads(retrievalId);
  for (const item of entries) {
    const key = `${retrievalId}:${item.candidate_id}`;
    if (item.judgment) {
      const previous = state.candidateJudgments[key];
      if (previous?.id !== item.judgment.id) delete state.candidateRewrites[key];
      state.candidateJudgments[key] = item.judgment;
      delete state.candidateErrors[key];
    } else if (item.error) {
      state.candidateErrors[key] = item.code || item.details
        ? { message: item.error, code: item.code, details: item.details }
        : item.error;
      delete state.candidateJudgments[key];
      delete state.candidateRewrites[key];
    }
  }
}

async function loadCandidateJudgments(retrievalId, force = false) {
  if (!force && state.candidateLoaded.has(retrievalId)) return;
  state.candidateLoaded.add(retrievalId);
  const generation = state.candidateGenerations[retrievalId] || 0;
  const requestId = (state.candidateLoadRequests[retrievalId] || 0) + 1;
  state.candidateLoadRequests[retrievalId] = requestId;
  try {
    const { judgments } = await api(`/api/candidate-judgments/${encodeURIComponent(retrievalId)}`);
    if (state.candidateLoadRequests[retrievalId] !== requestId) return;
    if ((state.candidateGenerations[retrievalId] || 0) !== generation) {
      state.candidateLoaded.delete(retrievalId);
      return;
    }
    mergeCandidateOutcomes(retrievalId, judgments);
    renderModelJobs();
    const retrieval = state.retrievals[state.report?.id];
    if (state.view === 'check' && retrieval?.id === retrievalId) {
      updateCandidateCards(retrieval);
      renderCandidateCheck(retrieval);
    }
    return true;
  } catch (error) {
    state.candidateLoaded.delete(retrievalId);
    toast(error.message, true);
    return false;
  }
}

function updateCandidateCards(retrieval, refreshRewriteFor = null) {
  document.querySelectorAll('[data-candidate]').forEach(card => {
    if (card.dataset.retrieval !== retrieval.id) return;
    const candidate = retrieval.candidates.find(item => item.id === card.dataset.candidate);
    const key = candidateKey(retrieval, candidate);
    if (candidateRequests.has(key)) return;
    const judgment = state.candidateJudgments[key];
    const error = state.candidateErrors[key];
    card.querySelector('.candidate-judgment').innerHTML = judgment ? judgmentHTML(judgment) : '';
    card.querySelector('.candidate-error').innerHTML = candidateErrorHTML(error);
    if (!state.candidateRewrites[key]) card.querySelector('.candidate-rewrite').replaceChildren();
    else if (candidate.id === refreshRewriteFor) {
      card.querySelector('.candidate-rewrite').innerHTML = candidateRewriteHTML(candidate, retrieval);
      bindCandidateRewrite(card, candidate, retrieval);
    }
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
  let uncertain = 0;
  for (const candidate of retrieval.candidates) {
    const key = candidateKey(retrieval, candidate);
    const relation = state.candidateJudgments[key]?.relation;
    if (Object.hasOwn(counts, relation)) counts[relation]++;
    if (candidateNeedsReview(state.candidateErrors[key])) uncertain++;
    else if (state.candidateErrors[key]) errors++;
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
        <span class="${uncertain ? 'result-fail' : ''}">Нужна проверка редактора <strong>${number(uncertain)}</strong></span>
        <span class="${errors ? 'result-fail' : ''}">Ошибки запросов <strong>${number(errors)}</strong></span>
      </div>
    </section>`;
  document.querySelector('#check-all-candidates').addEventListener('click', () => startCandidateCheck(retrieval));
  const cancel = document.querySelector('#cancel-candidate-check');
  if (cancel) cancel.addEventListener('click', event => busy(event.currentTarget, 'Останавливаем', async () => {
    const { job: cancelled } = await api(`/api/jobs/${encodeURIComponent(job.id)}/cancel`, {});
    state.candidateCheckJobs[retrieval.id] = cancelled;
    saveActiveJobs(CANDIDATE_JOBS_KEY, state.candidateCheckJobs);
    if (state.view === 'check' && state.retrievals[state.report?.id]?.id === retrieval.id) renderCandidateCheck(retrieval);
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
      const { job } = await readJobWithRetry(jobId, message => {
        state.candidateCheckJobs[retrievalId].stage = message;
        updateCandidateCheckProgress(retrievalId);
      });
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
    if (authError(error)) return;
    state.candidateCheckJobs[retrievalId] = { status: 'failed', error: error.message };
    await loadCandidateJudgments(retrievalId, true);
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
  if (!modelAvailable()) { toast('Модель сейчас недоступна. Проверьте настройки сервера.', true); return; }
  state.candidateCheckJobs[retrieval.id] = { status: 'queued', stage: 'Подготавливаем сравнение' };
  renderCandidateCheck(retrieval);
  updateCandidateCards(retrieval);
  updateReportFreshness();
  try {
    if (!publicReportReady(retrieval.report_id)) throw new Error('Откройте в редакторе текст этого отчёта перед сравнением.');
    const operationKey = `candidate-check:${retrieval.id}:${model}`;
    const body = { retrieval_id: retrieval.id, model };
    if (isSaas()) body.operation_id = publicOperationId(operationKey);
    const { job } = await api('/api/candidate-check-jobs', body);
    state.candidateCheckJobs[retrieval.id] = job;
    if (saveActiveJobs(CANDIDATE_JOBS_KEY, state.candidateCheckJobs)) acknowledgeOperation(operationKey);
    if (state.view === 'check' && state.retrievals[state.report?.id]?.id === retrieval.id) renderCandidateCheck(retrieval);
    await pollCandidateCheck(retrieval.id, job.id);
  } catch (error) {
    state.candidateCheckJobs[retrieval.id] = { status: 'failed', error: error.message };
    saveActiveJobs(CANDIDATE_JOBS_KEY, state.candidateCheckJobs);
    const current = state.retrievals[state.report?.id];
    if (state.view === 'check' && current?.id === retrieval.id) {
      renderCandidateCheck(retrieval);
      updateCandidateCards(retrieval);
      updateReportFreshness();
    }
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
      invalidateCandidateLoads(retrieval.id);
      updateReportFreshness();
      card.querySelectorAll('[data-candidate-action]').forEach(control => { control.disabled = true; });
      const action = button.dataset.candidateAction;
      const remote = modelProvider() === 'modal';
      try {
        const payload = { retrieval_id: retrieval.id, candidate_id: candidate.id, model: selectedModel() };
        if (action === 'judge') {
          const { judgment } = await modelRequest('/api/candidate-judge', payload, { report_id: retrieval.report_id });
          if (remote) {
            if (!await loadCandidateJudgments(retrieval.id, true)) throw new Error('Не удалось загрузить актуальную оценку пары. Ответ задания сохранён.');
          } else {
            mergeCandidateOutcomes(retrieval.id, [{ candidate_id: candidate.id, judgment }]);
          }
          delete state.candidateRewrites[key];
          if (card.isConnected) {
            const latest = state.candidateJudgments[key];
            card.querySelector('.candidate-judgment').innerHTML = latest ? judgmentHTML(latest) : '';
            card.querySelector('.candidate-rewrite').replaceChildren();
            card.querySelector('.candidate-error').innerHTML = candidateErrorHTML(state.candidateErrors[key]);
          }
          if (state.view === 'check' && state.retrievals[state.report?.id]?.id === retrieval.id) renderCandidateCheck(retrieval);
        } else {
          const judgment = state.candidateJudgments[key];
          if (!judgment?.id || !['exact', 'paraphrase'].includes(judgment.relation)) {
            throw new Error('Сначала подтвердите совпадение сравнением с Qwen.');
          }
          const { rewrite } = await modelRequest('/api/candidate-rewrite', {
            ...payload, judgment_id: judgment.id, mode: action,
          }, { report_id: retrieval.report_id });
          if (remote) {
            const loaded = await loadCandidateJudgments(retrieval.id, true);
            const latest = state.candidateJudgments[key];
            if (!loaded || !latest || latest.id !== rewrite.judgment_id || !['exact', 'paraphrase'].includes(latest.relation)) {
              throw new Error('Оценка пары изменилась. Сохранённая правка не будет применена.');
            }
          }
          state.candidateRewrites[key] = rewrite;
          if (card.isConnected && state.report?.id === retrieval.report_id) {
            card.querySelector('.candidate-rewrite').innerHTML = candidateRewriteHTML(candidate, retrieval);
            bindCandidateRewrite(card, candidate, retrieval);
          }
        }
      } catch (error) {
        if (action === 'judge') {
          const latestLoaded = remote && await loadCandidateJudgments(retrieval.id, true);
          if (!latestLoaded) {
            mergeCandidateOutcomes(retrieval.id, [{ candidate_id: candidate.id, error: error.message, code: error.code, details: error.details }]);
          }
          if (card.isConnected) {
            const latest = state.candidateJudgments[key];
            card.querySelector('.candidate-judgment').innerHTML = latest ? judgmentHTML(latest) : '';
            card.querySelector('.candidate-rewrite').replaceChildren();
            card.querySelector('.candidate-error').innerHTML = candidateErrorHTML(state.candidateErrors[key]);
          }
          if (state.view === 'check' && state.retrievals[state.report?.id]?.id === retrieval.id) renderCandidateCheck(retrieval);
        }
        if (error.code === 'unconfirmed_exact') {
          toast('Для этой пары нужна проверка редактора.');
          return;
        }
        throw error;
      } finally {
        candidateRequests.delete(key);
        if (state.view === 'check' && state.retrievals[state.report?.id]?.id === retrieval.id) {
          updateCandidateCards(retrieval, candidate.id);
        }
        updateReportFreshness();
        updateModelActionControls();
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
      || state.retrievals[retrieval.report_id]?.id !== retrieval.id || candidateCheckRunning(retrieval.id)
      || candidateHasModelJob(retrieval.id, candidate.id)) {
      toast('Текст изменён. Повторите проверку перед применением правки.', true);
      return;
    }
    const rewrite = state.candidateRewrites[candidateKey(retrieval, candidate)];
    const text = chars(state.text);
    const judgment = state.candidateJudgments[candidateKey(retrieval, candidate)];
    if (!rewrite || !judgment || !['exact', 'paraphrase'].includes(judgment.relation)
      || rewrite.judgment_id !== judgment.id || candidateRequests.has(candidateKey(retrieval, candidate))) {
      toast('Оценка пары изменилась. Получите новую правку перед применением.', true);
      return;
    }
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
  const key = `${state.report.id}:${match.id}`;
  const comment = state.findingDrafts[key] ?? review?.comment ?? '';
  return `
    <div class="finding-review">
      <h3>Решение редактора</h3>
      <p class="field-help">${escapeHTML(reviewLabel(match.id) || 'Сравните фрагменты и сохраните своё решение.')} Доля совпадений отражает исходный результат поиска.</p>
      <input class="field" id="finding-comment" maxlength="2000" value="${escapeHTML(comment)}" aria-label="Комментарий к находке" placeholder="Комментарий · необязательно">
      <div class="action-row">
        <button class="button small secondary" data-finding-decision="confirmed" type="button">Подтвердить</button>
        <button class="button small secondary" data-finding-decision="dismissed" type="button">Отклонить находку</button>
        ${review && review.decision !== 'pending' ? '<button class="text-button" data-finding-decision="pending" type="button">Вернуть на проверку</button>' : ''}
      </div>
      <p class="field-help" id="finding-save-status" role="status"></p>
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
      <p class="field-help" id="comparison-model-unavailable" ${available ? 'hidden' : ''}>Модель сейчас недоступна. Настройки сервера находятся в меню сверху.</p>
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
  const draftKey = `${reportId}:${match.id}`;
  const commentField = document.querySelector('#finding-comment');
  commentField.addEventListener('input', () => {
    state.findingDrafts[draftKey] = commentField.value;
    saveReviewDrafts();
    document.querySelector('#finding-save-status').textContent = 'Комментарий ещё не сохранён в отчёте.';
  });
  document.querySelectorAll('[data-finding-decision]').forEach(button => {
    button.addEventListener('click', () => busy(button, 'Сохраняем решение', async () => {
      const reviewSection = button.closest('.finding-review');
      reviewSection.querySelectorAll('button').forEach(control => { control.disabled = true; });
      const submittedComment = commentField.value;
      const comment = submittedComment.trim();
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
      const changedWhileSaving = state.findingDrafts[draftKey] !== undefined && state.findingDrafts[draftKey] !== submittedComment;
      if (!changedWhileSaving) {
        delete state.findingDrafts[draftKey];
        saveReviewDrafts();
      }
      if (state.report?.id !== reportId) return;
      state.report.reviews = { ...state.report.reviews, [match.id]: review };
      if (currentComparison(reportId, match.id)) {
        if (changedWhileSaving) {
          document.querySelector('#finding-save-status').textContent = 'Решение сохранено. Новые изменения комментария ещё не сохранены.';
        } else {
          renderReport();
        }
      }
      toast('Решение по находке сохранено.');
    }));
  });
  for (const [id, mode] of [['quote-button', 'quote'], ['paraphrase-button', 'paraphrase']]) {
    document.querySelector(`#${id}`).addEventListener('click', event => busy(event.currentTarget, 'Готовим правку', async () => {
      const resultArea = document.querySelector('#rewrite-area');
      const { rewrite } = await modelRequest('/api/rewrite', {
        report_id: reportId, match_id: match.id, mode, model: selectedModel(),
      }, { original_text: match.excerpt, source_title: match.source_title });
      if (!resultArea.isConnected || !currentComparison(reportId, match.id)) return;
      state.rewrite = rewrite;
      document.querySelector('#rewrite-area').innerHTML = rewriteHTML();
      bindRewrite(match);
    }));
  }
  document.querySelector('#judge-button').addEventListener('click', event => busy(event.currentTarget, 'Qwen сравнивает', async () => {
    const resultArea = document.querySelector('#judgment-area');
    const { judgment } = await modelRequest('/api/judge', { report_id: reportId, match_id: match.id, model: selectedModel() });
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
function sourceProvenanceHTML(source) {
  const provenance = source.provenance;
  if (!provenance || !Object.keys(provenance).length) return '';
  return `
    <div class="source-provenance">
      ${provenance.publisher ? `<span>${escapeHTML(provenance.publisher)}</span>` : ''}
      ${provenance.license_url ? safeLink(provenance.license_url, provenance.license_name || 'Лицензия') : ''}
    </div>
    ${provenance.attribution ? `
      <details class="source-attribution">
        <summary>Авторство и условия использования</summary>
        <p>${escapeHTML(provenance.attribution)}</p>
        ${safeLink(provenance.revision_url, 'Версия публикации')}
        ${safeLink(provenance.history_url, 'История авторства')}
      </details>` : ''}`;
}

function renderSourcePacks() {
  const area = document.querySelector('#source-packs-panel');
  if (!area) return;
  if (state.sourcePacks === null) {
    area.innerHTML = '<p class="field-help">Загружаем открытые подборки источников…</p>';
    return;
  }
  area.innerHTML = state.sourcePacks.length ? state.sourcePacks.map(pack => `
    <article class="source-pack-card">
      <div>
        <h2>${escapeHTML(pack.title)}</h2>
        <p>${escapeHTML(pack.description || 'Открытые публикации для сравнения с вашим текстом.')}</p>
        <small>Материалов в подборке ${number(pack.source_count)}${pack.license_name ? ` · ${escapeHTML(pack.license_name)}` : ''}</small>
      </div>
      ${pack.source_count > 0 && pack.installed_count >= pack.source_count
        ? '<span class="source-pack-installed">Уже в библиотеке <span aria-hidden="true">✓</span></span>'
        : `<button class="button secondary small" data-install-pack="${escapeHTML(pack.id)}" type="button">Добавить подборку <span aria-hidden="true">+</span></button>`}
    </article>`).join('') : '<p class="field-help">Открытых подборок пока нет. Можно добавлять свои материалы.</p>';
  area.querySelectorAll('[data-install-pack]').forEach(button => {
    button.addEventListener('click', () => busy(button, 'Добавляем источники', async () => {
      const result = await api('/api/source-packs/install', { pack_id: button.dataset.installPack });
      await refreshSources();
      await loadSourcePacks();
      renderSourceList();
      toast(`Добавлено источников ${number(result.installed_count)}. Уже были в библиотеке ${number(result.already_present_count)}.`);
    }));
  });
}

async function loadSourcePacks() {
  try {
    const { packs } = await api('/api/source-packs');
    state.sourcePacks = packs;
    if (state.view === 'sources') renderSourcePacks();
  } catch {
    const area = document.querySelector('#source-packs-panel');
    if (area) area.innerHTML = '<p class="field-help">Подборка сейчас недоступна. Можно добавлять свои источники вручную.</p>';
  }
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
        ${sourceProvenanceHTML(source)}
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
        await loadSourcePacks();
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
    <section class="card source-packs-section" id="source-packs-panel" aria-live="polite"></section>
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
          <textarea class="field" id="source-text" name="text" required placeholder="Вставьте текст публикации…"></textarea>
          <p class="field-help" id="source-text-limit-error" role="alert"></p>
          <p class="field-help" id="source-text-import-hint" role="status" hidden></p>
        </div>
        <button class="button" type="submit">Добавить в библиотеку <span aria-hidden="true">+</span></button>
        <p class="field-help">${isSaas() ? 'Источники сохраняются в вашем аккаунте и используются в следующих проверках.' : 'Источники сохраняются на вашем Mac и используются в следующих проверках.'}</p>
      </form>
    </section>
    <div class="section-heading source-list-heading">
      <h2>Ваши источники</h2><span class="subtle" id="source-list-count"></span>
    </div>
    <div id="source-list" class="source-list"></div>`;
  renderSourcePacks();
  loadSourcePacks();
  renderSourceList();
  const sourceText = document.querySelector('#source-text');
  const draftFields = { title: document.querySelector('#source-title'), url: document.querySelector('#source-url'), text: sourceText };
  for (const [key, field] of Object.entries(draftFields)) {
    field.value = state.sourceDraft[key] || '';
    field.addEventListener('input', () => {
      state.sourceDraft = Object.fromEntries(Object.entries(draftFields).map(([name, input]) => [name, input.value]));
      try { storageSet(SOURCE_DRAFT_KEY, JSON.stringify(state.sourceDraft)); } catch { /* Keep the current form in memory. */ }
    });
  }
  sourceText.addEventListener('input', () => validateTextLength(sourceText));
  document.querySelector('#source-form').addEventListener('submit', event => {
    event.preventDefault();
    const form = event.currentTarget;
    if (!validateTextLength(form.elements.text)) { form.elements.text.reportValidity(); return; }
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
        state.sourceDraft = {};
        try { storageSet(SOURCE_DRAFT_KEY, '{}'); } catch { /* The saved source is already on the server. */ }
        document.querySelector('#source-text-import-hint').hidden = true;
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
  main.innerHTML = heading('Оценка Qwen',
    'Проверьте, как Qwen сравнивает пару фрагментов с уже известным источником. Сохраните свои исправления.', '', 'Оценка модели') + `
    <section class="card evaluation-top">
      <div><h2>Контрольная проверка</h2>
        <p>Дословные совпадения, пересказы и независимые тексты на одну тему. Источник заранее дан модели.
          Учебные пары помогают искать ошибки и уже использовались при настройке инструкций.</p>
        <p class="field-help" data-processing-disclosure>${escapeHTML(processingDisclosure())}</p>
      </div>
      <button class="button" id="run-evaluation" type="button" ${!modelAvailable() ? 'disabled' : ''}>Запустить оценку <span aria-hidden="true">↗</span></button>
    </section>
    <div class="warning" id="evaluation-model-unavailable" ${modelAvailable() ? 'hidden' : ''}>Модель сейчас недоступна. Настройки можно посмотреть в меню сверху. Учебные примеры доступны для просмотра.</div>
    <p class="field-help" id="evaluation-job-note" hidden></p>
    <div id="evaluation-result"></div>
    <div class="section-heading" style="margin-top:28px"><div><h2>Размеченные примеры</h2><p>Учебные тексты написаны специально для прототипа.</p></div><button class="text-button" id="export-training" type="button">↓ Выгрузить обучение</button></div>
    <div id="example-list"><p class="subtle">Загружаем примеры…</p></div>`;
  updateModelActionControls();
  document.querySelector('#run-evaluation').addEventListener('click', event => busy(event.currentTarget, 'Qwen проходит проверку', async () => {
    const { run } = await modelRequest('/api/evaluate', { model: selectedModel(), split: 'test' });
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
  container.innerHTML = `<div class="section-heading" style="margin-top:25px"><div><h2>Последний запуск</h2><p>${escapeHTML(run.model)} · ${escapeHTML(date(run.created_at))}</p></div></div><div class="stats-row"><div class="stat-card"><strong>${metricValue(metrics.accuracy)}</strong><span>верных меток на примерах</span></div><div class="stat-card"><strong>${metricValue(metrics.precision)}</strong><span>точность положительных ответов</span></div><div class="stat-card"><strong>${metricValue(metrics.recall)}</strong><span>полнота при известном источнике</span></div></div><p class="dataset-note">Завершено ${number(metrics.completed)} из ${number(metrics.total)} примеров · ошибок запросов ${number(metrics.errors)}. Точность находок и полнота учитывают завершённые ответы.<br>Это диагностический прогон на учебных парах, уже использованных при настройке инструкций. Для оценки качества на реальных документах понадобится независимый набор.</p>`;
}
function exampleHTML(example) {
  const result = state.run?.results?.find(item => item.example_id === example.id || item.id === example.id);
  const predicted = result?.predicted ?? result?.relation ?? result?.judgment?.relation;
  const correct = predicted === example.expected;
  const draft = state.exampleDrafts[example.id];
  const selected = draft?.relation || example.review?.relation || example.expected;
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
          value="${escapeHTML(draft?.comment ?? example.review?.comment ?? '')}" placeholder="Почему вы так считаете?">
        <button class="button small secondary" type="submit">Сохранить оценку</button>
        <span class="field-help review-save-status" role="status"></span>
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
    const rememberDraft = () => {
      state.exampleDrafts[form.dataset.review] = {
        relation: form.elements.relation.value, comment: form.elements.comment.value,
      };
      saveReviewDrafts();
      form.querySelector('.review-save-status').textContent = 'Есть несохранённые изменения.';
    };
    form.elements.comment.addEventListener('input', rememberDraft);
    form.elements.relation.addEventListener('change', rememberDraft);
    form.addEventListener('submit', event => {
      event.preventDefault();
      busy(form.querySelector('button'), 'Сохраняем', async () => {
        const submitted = { relation: form.elements.relation.value, comment: form.elements.comment.value };
        const { review } = await api('/api/reviews', {
          example_id: form.dataset.review, relation: submitted.relation, comment: submitted.comment.trim(),
        });
        const draft = state.exampleDrafts[form.dataset.review];
        const changedWhileSaving = draft && (draft.comment !== submitted.comment || draft.relation !== submitted.relation);
        if (!changedWhileSaving) {
          delete state.exampleDrafts[form.dataset.review];
          saveReviewDrafts();
        }
        if (form.isConnected) form.querySelector('.review-save-status').textContent = changedWhileSaving
          ? 'Оценка сохранена. Новые изменения ещё не сохранены.' : 'Оценка сохранена.';
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
    list.innerHTML = reports.length ? reports.map(report => `<article class="card history-row"><div><small>${escapeHTML(date(report.created_at))}</small><h3>${escapeHTML(report.text.slice(0, 85))}${report.text.length > 85 ? '…' : ''}</h3><p>${number(report.stats.total_characters ?? chars(report.text).length)} знаков · ${report.scope === 'web_search' ? `${webUniqueness(report)} · уникальность по интернету` : `${Number(report.stats.similarity_percent || 0).toLocaleString('ru-RU', { maximumFractionDigits: 1 })}% совпадений по библиотеке`}</p></div><button class="button secondary small" data-open-report="${escapeHTML(report.id)}" type="button">Открыть отчёт ↗</button></article>`).join('') : empty('Проверок пока нет', 'Проверьте первый текст, и его отчёт появится здесь.', '↶');
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
  const homeCount = document.querySelector('#home-library-count');
  if (homeCount) homeCount.textContent = number(sources.length);
  document.querySelector('#home-library-fact')?.setAttribute('aria-label', `${number(sources.length)} материалов в вашей библиотеке`);
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
  const provider = modelProvider();
  if (state.modelProvider !== provider) {
    state.modelProvider = provider;
    state.model = restoreModel(provider);
  }
  if (provider === 'modal' && !installedModels().includes(state.model)) {
    const models = installedModels();
    state.model = models.includes(state.status.model.model) ? state.status.model.model : models[0] || '';
  }
  updateModelControls();
}

function updateModelControls() {
  const model = state.status?.model || {};
  const available = modelAvailable();
  const remote = modelProvider() === 'modal';
  document.querySelector('#model-dot').classList.toggle('muted', remote || !available);
  document.querySelector('#model-label').textContent = remote
    ? `${selectedModel() || 'Qwen'} · ${model.configured ? 'сервер настроен' : 'сервер не настроен'}`
    : available ? selectedModel() : model.local_inference_disabled || modelProvider() === 'disabled' ? 'Локальные модели отключены' : 'Модель не подключена';
  const description = remote
    ? `${model.configured ? 'Сервер Modal настроен. Настройки прочитаны без запроса к GPU.' : 'Сервер Modal ещё не настроен.'} ${model.can_submit ? 'Первый запрос может потребовать запуска серверной модели.' : 'Отправка модельных заданий сейчас отключена.'}${model.last_success_at ? ` Последний успешный запрос ${date(model.last_success_at)}.` : ''}${model.error ? ` ${model.error}` : ''}`
    : model.local_inference_disabled || modelProvider() === 'disabled'
      ? 'Локальные модели отключены. Автоматического запуска или скачивания не будет.'
      : available ? 'Подключена локальная модель. Точный поиск работает самостоятельно.' : (model.error || 'Модель сейчас недоступна. Точный поиск по библиотеке продолжает работать.');
  document.querySelector('#model-description').textContent = description;
  document.querySelector('#model-dialog-title').textContent = remote ? 'Серверная модель' : 'Настройки модели';
  updateProcessingLabels();
  updateCheckModeUI();
  const select = document.querySelector('#model-select');
  select.replaceChildren();
  const models = installedModels();
  if (state.model && !models.includes(state.model)) {
    select.add(new Option(`${state.model} · недоступна`, state.model));
  }
  for (const item of models) {
    const name = typeof item === 'string' ? item : item.name;
    select.add(new Option(name, name));
  }
  if (!models.length) select.add(new Option(remote ? 'Серверных моделей пока нет' : 'Доступных моделей пока нет', ''));
  select.value = selectedModel() || '';
  select.disabled = !models.length;
  updateModelActionControls();
}

function updateModelActionControls() {
  const available = modelAvailable();
  for (const id of ['evaluation-model-unavailable', 'comparison-model-unavailable']) {
    const note = document.getElementById(id);
    if (note) note.hidden = available;
  }
  const batchButton = document.querySelector('#check-all-candidates');
  const currentRetrieval = state.retrievals[state.report?.id];
  if (batchButton) batchButton.disabled = !available || !publicReportReady() || !currentRetrieval?.candidates.length || candidateCheckRunning(currentRetrieval.id);
  const hybrid = document.querySelector('#retrieve-hybrid');
  if (hybrid) hybrid.disabled = !state.status?.embedding?.available || !publicReportReady() || retrievalRunning(state.report?.id)
    || Boolean(isSaas() && currentRetrieval);
  const lexical = document.querySelector('#retrieve-lexical');
  if (lexical && isSaas()) lexical.disabled = !publicReportReady() || retrievalRunning(state.report?.id) || Boolean(currentRetrieval);
  const embeddingNotice = document.querySelector('#embedding-unavailable');
  if (embeddingNotice) embeddingNotice.hidden = Boolean(state.status?.embedding?.available);
  document.querySelectorAll('[data-candidate-action]').forEach(button => {
    const card = button.closest('[data-candidate]');
    const key = `${card.dataset.retrieval}:${card.dataset.candidate}`;
    if (!candidateRequests.has(key)) button.disabled = !publicReportReady() || (button.dataset.candidateAction !== 'quote' && !available) || candidateCheckRunning(card.dataset.retrieval)
      || candidateHasModelJob(card.dataset.retrieval, card.dataset.candidate);
  });
  const model = selectedModel();
  const target = { report_id: state.report?.id, match_id: state.selectedMatch, model };
  const controls = [
    ['compare-button', '/api/compare', { text: state.text, source_id: document.querySelector('#compare-source')?.value || '', model, ...(isSaas() ? { report_id: state.report?.id } : {}) }],
    ['paraphrase-button', '/api/rewrite', { ...target, mode: 'paraphrase' }],
    ['quote-button', '/api/rewrite', { ...target, mode: 'quote' }],
    ['judge-button', '/api/judge', target],
    ['run-evaluation', '/api/evaluate', { model, split: 'test' }],
  ];
  for (const [id, path, body] of controls) {
    const button = document.getElementById(id);
    const pending = (modelProvider() === 'modal' || isSaas()) && pendingModelRequest(path, body);
    if (button) button.disabled = Boolean(button.dataset.busy) || !publicReportReady() || (id !== 'quote-button' && !available) || Boolean(pending);
    if (id === 'run-evaluation') {
      const note = document.querySelector('#evaluation-job-note');
      if (note) {
        note.hidden = !pending;
        note.textContent = pending && ['missing', 'delivery_unknown', 'rejected'].includes(pending.status)
          ? 'Отправка этой оценки не подтверждена. Используйте повторную отправку в уведомлении о задании.'
          : 'Оценка этой модели уже запущена. Состояние и сохранённые ответы показаны в задании сверху.';
      }
    }
  }
  updateQuotaUI();
}
async function restoreLinkedReport(surface, id, revision) {
  try {
    const { report } = await api(`/api/reports/${id}`);
    if (state.viewRevision !== revision || location.hash !== `#${surface}/${id}` || report.id !== id) return;
    state.rewrite = null;
    state.judgment = null;
    if (surface === 'home' && report.scope === 'web_search') {
      if (!state.homeText) {
        const field = document.querySelector('#home-text');
        if (field) field.value = report.text;
        state.homeText = report.text;
        try { storageSet(HOME_DRAFT_KEY, report.text); } catch { /* The report is still readable. */ }
      }
      state.homeReport = report;
      state.homeMatch = report.matches[0]?.id || null;
      renderHomeResult();
    } else if (surface === 'check') {
      state.report = report;
      state.selectedMatch = report.matches[0]?.id || null;
      if (!state.text) {
        saveDraft(report.text);
        const field = document.querySelector('#check-text');
        if (field) {
          field.value = report.text;
          updateCharacterCount();
          validateTextLength(field);
        }
      }
      renderReport();
      document.querySelector('#report-area')?.scrollIntoView({ block: 'start' });
    }
  } catch (error) {
    if (state.viewRevision === revision && !authError(error)) toast('Не удалось открыть сохранённый отчёт. Проверьте историю в своём аккаунте.', true);
  }
}

function renderView() {
  window.IstokMotion?.teardown();
  state.viewRevision += 1;
  if (!state.sessionReady) {
    main.innerHTML = '<section class="card auth-loading"><h1>Открываем рабочее пространство…</h1><p>Проверяем сессию без запуска моделей.</p></section>';
    return;
  }
  document.body.classList.toggle('auth-view', !privateAccess());
  if (isSaas() && !privateAccess()) {
    document.body.classList.toggle('home-view', false);
    document.querySelector('#breadcrumb-title').textContent = 'Вход по почте';
    renderAuthScreen();
    return;
  }
  const savedReport = /^#(home|check)\/([a-f0-9]{32})$/.exec(location.hash);
  const requested = savedReport ? savedReport[1] : location.hash.slice(1) || 'home';
  state.view = viewNames[requested] ? requested : 'home';
  const field = requested === 'home' ? 'homeReport' : 'report';
  const restoreId = savedReport && state[field]?.id !== savedReport[2] ? savedReport[2] : null;
  if (restoreId) state[field] = null;
  if (isSaas() && state.view === 'evaluation') state.view = 'check';
  document.body.classList.toggle('home-view', state.view === 'home');
  document.querySelectorAll('[data-view]').forEach(link => {
    const active = link.dataset.view === state.view;
    link.classList.toggle('active', active);
    if (active) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current');
  });
  document.querySelector('#breadcrumb-title').textContent = viewNames[state.view];
  document.title = `${viewNames[state.view]} · Исток`;
  if (state.view === 'home') renderHome();
  else if (state.view === 'check') renderCheck();
  else if (state.view === 'sources') renderSources();
  else if (state.view === 'evaluation') renderEvaluation();
  else renderHistory();
  if (restoreId) restoreLinkedReport(requested, restoreId, state.viewRevision);
  updateProcessingLabels();
  updateModelActionControls();
  renderModelJobs();
}
document.querySelector('#model-settings').addEventListener('click', () => document.querySelector('#model-dialog').showModal());
document.querySelector('#model-select').addEventListener('change', event => {
  state.model = event.target.value;
  try {
    storageSet(modelProvider() === 'modal' ? MODAL_MODEL_KEY : MODEL_KEY, state.model);
  } catch {
    // The selected model still applies to this open tab.
  }
  updateModelControls();
});
document.querySelector('#refresh-model').addEventListener('click', event => busy(event.currentTarget, 'Проверяем', async () => {
  await refreshStatus();
  toast(modelProvider() === 'modal' ? 'Настройки сервера обновлены. Запрос к GPU не выполнялся.'
    : modelAvailable() ? 'Настройки модели обновлены.' : 'Модель сейчас недоступна.', !modelAvailable());
}));
window.addEventListener('hashchange', renderView);
document.querySelector('#logout-button').addEventListener('click', event => busy(event.currentTarget, 'Выходим', async () => {
  saveDraft(state.text);
  saveReviewDrafts();
  await api('/api/auth/logout', {});
  state.sessionTransition = true;
  location.reload();
}));

async function bootstrap() {
  renderView();
  try {
    await refreshSession();
    if (state.sessionTransition) return;
    state.sessionReady = true;
    hydrateSessionStorage();
    updateAccountUI();
    renderView();
  } catch (error) {
    main.innerHTML = `<section class="card auth-loading"><h1>Не удалось открыть сервис</h1><p>${escapeHTML(error.message)}</p><button class="button" id="retry-bootstrap" type="button">Попробовать ещё раз</button></section>`;
    document.querySelector('#retry-bootstrap').addEventListener('click', bootstrap);
    return;
  }
  renderModelJobs();
  if (!privateAccess()) return;
  for (const entry of Object.values(state.modelJobs)) {
    if (entry.status !== 'rejected') observeModelJob(entry, true).catch(() => {});
  }
  for (const [reportId, job] of Object.entries(state.retrievalJobs)) {
    pollRetrieval(reportId, job.id);
  }
  for (const [retrievalId, job] of Object.entries(state.candidateCheckJobs)) {
    pollCandidateCheck(retrievalId, job.id);
  }
  const results = await Promise.allSettled([refreshSources(), refreshStatus()]);
  for (const result of results) if (result.status === 'rejected') toast(result.reason.message, true);
  if (state.view === 'sources') renderSourceList();
}
bootstrap();

setInterval(async () => {
  if (!state.sessionReady || state.sessionTransition) return;
  try {
    if (isSaas()) await refreshSession();
    if (privateAccess()) await refreshStatus();
  } catch (error) {
    if (!privateAccess()) return;
    state.status = { ...state.status, model: { ...state.status?.model, available: false, can_submit: false, models: [], error: error.message } };
    updateModelControls();
  }
}, 15000);
