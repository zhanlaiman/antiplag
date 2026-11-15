'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const number = value => Number(value).toLocaleString('ru-RU');
  const date = value => new Date(value * 1000).toLocaleString('ru-RU');
  let csrf = '', offset = 0, selected = null, businessWrites = true;
  const kinds = {guest: 'Гость', owner: 'Владелец', registered: 'Зарегистрирован'};
  function notice(message = '', error = false) { $('notice').textContent = message; $('notice').classList.toggle('error', error); }
  function loginView() {
    csrf = ''; $('dashboard-panel').hidden = true; $('logout').hidden = true; $('login-panel').hidden = false;
    $('accounts').replaceChildren(); $('audit').replaceChildren();
    if ($('block-dialog').open) $('block-dialog').close();
  }
  async function api(path, body) {
    const response = await fetch(path, {method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: body === undefined ? {} : {'Content-Type': 'application/json', 'X-CSRF-Token': csrf}, body: body === undefined ? undefined : JSON.stringify(body)});
    const result = await response.json();
    if (!response.ok) {
      if (response.status === 401 && result.code !== 'dashboard_login_failed') loginView();
      throw new Error(result.error || 'Не удалось выполнить запрос. Обновите панель.');
    }
    return result;
  }
  function element(tag, text, className) {
    const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (className) node.className = className; return node;
  }
  function appendAccountIDs(parent, account) {
    for (const [label, value, className] of [
      ['ID аккаунта', account.registered_account_id, 'registered-account-id'],
      ['ID пространства', account.id, 'canonical-guest-id']
    ]) {
      if (value) parent.append(element('span', label, 'account-id-label'), element('p', value, `account-id ${className}`));
    }
  }
  function renderAccounts(data) {
    $('total').textContent = number(data.total); $('queue').textContent = `${data.queue.active} / ${data.queue.capacity}`;
    const available = data.accounts.filter(a => a.metrics.available);
    $('characters').textContent = number(available.reduce((n, a) => n + a.metrics.characters_today, 0));
    $('errors').textContent = number(available.reduce((n, a) => n + a.metrics.errors_today, 0));
    const unavailable = data.accounts.length - available.length;
    if (unavailable) { $('characters').textContent += ' + нет данных'; $('errors').textContent += ' + нет данных'; }
    const summary = data.summary || {}, summaryNumber = value => Number.isInteger(value) && value >= 0 ? number(value) : 'Нет данных';
    $('usage-summary').textContent = [
      `Зарегистрированные: ${summaryNumber(summary.registered_total)}`, `Гостевые ID: ${summaryNumber(summary.guest_total)}`,
      `Владельцы: ${summaryNumber(summary.owner_total)}`, `Проверки сегодня: ${summaryNumber(summary.checks_today)}`,
      `Аккаунты с проверками сегодня: ${summaryNumber(summary.checking_accounts_today)}`
    ].join(' · ');
    const registration = data.registration.beta_connected || data.registration.ledger === 'connected' ? 'подключён' : 'не подключён';
    $('sources').textContent = `Учёт зарегистрированных пользователей: ${registration}. Оплата не подключена. Гостевой ID может относиться к браузеру; история включает тестовые ID. Это учёт ID и аккаунтов, а не уникальных людей или посещений.`;
    $('updated').textContent = `Обновлено ${new Date(data.as_of).toLocaleString('ru-RU')} · день UTC ${data.day_utc}`;
    $('accounts').replaceChildren(); $('empty').hidden = data.accounts.length > 0;
    for (const account of data.accounts) {
      const row = document.createElement('tr'), identity = document.createElement('td');
      identity.append(element('strong', kinds[account.kind] || 'Аккаунт'));
      appendAccountIDs(identity, account);
      if (account.first_seen_month) identity.append(element('span', `Первое действие: ${account.first_seen_month} (месяц)`, 'small'));
      const state = document.createElement('td'); state.append(element('span', account.blocked ? 'Заблокирован' : 'Доступен', `status${account.blocked ? ' blocked' : ''}`));
      if (account.block_reason) state.append(element('p', account.block_reason, 'reason'));
      const checks = element('td', `${number(account.web_checks_admitted.today)} / ${number(account.web_checks_admitted.total)}`);
      const metrics = account.metrics;
      const reports = element('td', metrics.available ? `${number(metrics.reports_active)} / ${number(metrics.reports_total)}` : 'Нет данных');
      const characters = element('td', metrics.available ? `${number(metrics.characters_today)} / ${number(metrics.characters_total)}` : 'Нет данных');
      if (metrics.unknown_character_reports) characters.append(element('span', `${metrics.unknown_character_reports} отчётов без счётчика`, 'small'));
      if (metrics.errors_total) { const details = document.createElement('details'); details.append(element('summary', `Сбои: ${metrics.errors_today} сегодня / ${metrics.errors_total} всего`)); for (const error of metrics.recent_errors) details.append(element('p', error.code)); reports.append(details); }
      const last = account.last_usage;
      const usage = element('td', !last ? 'Нет данных' : last.precision === 'day' ? `${last.at} (день UTC)` : new Date(last.at).toLocaleString('ru-RU'));
      const action = document.createElement('td'), button = element('button', account.blocked ? 'Разблокировать' : 'Блокировать');
      button.disabled = !businessWrites;
      button.type = 'button'; button.addEventListener('click', () => {
        selected = account; $('block-title').textContent = account.blocked ? 'Разблокировать аккаунт' : 'Блокировать аккаунт';
        $('block-target').replaceChildren(); appendAccountIDs($('block-target'), account);
        $('reason').value = ''; $('block-error').textContent = ''; $('confirm-block').textContent = account.blocked ? 'Разблокировать' : 'Блокировать';
        $('block-dialog').showModal(); $('reason').focus();
      }); action.append(button); row.append(identity, state, checks, reports, characters, usage, action); $('accounts').append(row);
    }
    $('previous').disabled = offset === 0; $('next').disabled = !data.has_more;
    $('page-label').textContent = data.total ? `${offset + 1}–${Math.min(offset + data.limit, data.total)} из ${data.total}` : '0 аккаунтов';
  }
  function renderAudit(data) {
    const labels = {login: 'Вход администратора', logout: 'Выход администратора', block: 'Блокировка', unblock: 'Разблокировка'};
    $('audit').replaceChildren();
    for (const event of data.events) { const li = document.createElement('li'); li.append(element('time', date(event.at)), element('span', labels[event.action] || 'Действие')); if (event.account_id) li.append(element('p', event.account_id, 'account-id')); if (event.reason) li.append(element('span', ` · ${event.reason}`)); $('audit').append(li); }
    if (!data.events.length) $('audit').append(element('li', 'Действий пока нет.'));
  }
  async function refresh() {
    $('refresh').disabled = true; notice('Загружаем данные…');
    try { const [accounts, audit] = await Promise.all([api(`/api/dashboard/accounts?offset=${offset}`), api('/api/dashboard/audit')]); renderAccounts(accounts); renderAudit(audit); notice(businessWrites ? '' : 'Панель доступна только для просмотра. Изменения временно отключены.'); }
    catch (error) { notice(error.message, true); }
    finally { $('refresh').disabled = false; }
  }
  async function authenticated(session) {
    businessWrites = session.business_writes_enabled !== false;
    csrf = session.csrf_token; $('password').value = ''; $('login-panel').hidden = true; $('dashboard-panel').hidden = false; $('logout').hidden = false;
    if (new URLSearchParams(location.search).get('next') === 'pool') { location.assign('/dashboard/pool'); return; }
    await refresh();
  }
  $('login-form').addEventListener('submit', async event => {
    event.preventDefault(); const button = event.submitter; button.disabled = true; notice('Проверяем данные входа…');
    try { await authenticated(await api('/api/dashboard/login', {username: $('username').value, password: $('password').value})); }
    catch (error) { notice(error.message, true); }
    finally { button.disabled = false; }
  });
  $('logout').addEventListener('click', async () => { $('logout').disabled = true; try { await api('/api/dashboard/logout', {}); loginView(); notice('Вы вышли из панели.'); } catch (error) { notice(error.message, true); } finally { $('logout').disabled = false; } });
  $('refresh').addEventListener('click', refresh);
  $('previous').addEventListener('click', () => { offset = Math.max(0, offset - 50); refresh(); });
  $('next').addEventListener('click', () => { offset += 50; refresh(); });
  $('cancel-block').addEventListener('click', () => $('block-dialog').close());
  $('block-form').addEventListener('submit', async event => {
    event.preventDefault(); if (!selected) return; $('confirm-block').disabled = true; $('block-error').textContent = '';
    try { await api(`/api/dashboard/accounts/${selected.id}/${selected.blocked ? 'unblock' : 'block'}`, {reason: $('reason').value}); $('block-dialog').close(); await refresh(); }
    catch (error) { $('block-error').textContent = error.message; }
    finally { $('confirm-block').disabled = false; }
  });
  (async () => { try { const session = await api('/api/dashboard/session'); if (session.authenticated) await authenticated(session); else { loginView(); notice(session.configured === false ? 'Вход администратора ещё не настроен.' : ''); } } catch (error) { loginView(); notice(error.message, true); } })();
})();
