'use strict';
(() => {
  const $ = id => document.getElementById(id);
  let csrf = '', mode = 'login', session = null;
  function notice(message = '', error = false) { $('notice').textContent = message; $('notice').classList.toggle('error', error); }
  async function api(path, body) {
    const response = await fetch(path, {method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: body === undefined ? {} : {'Content-Type': 'application/json', 'X-Istok-CSRF': csrf}, body: body === undefined ? undefined : JSON.stringify(body)});
    const result = await response.json(); if ('csrf_token' in result) csrf = result.csrf_token;
    if (!response.ok) throw new Error(result.error || 'Не удалось выполнить запрос. Попробуйте позже.');
    return result;
  }
  function setMode(next) {
    mode = next === 'register' && session?.registration_allowed ? 'register' : 'login';
    const registering = mode === 'register';
    $('auth-title').textContent = registering ? 'Создать аккаунт' : 'Войти в аккаунт';
    $('intro').textContent = registering ? 'Регистрация сохранит текущее гостевое пространство за вашим аккаунтом: отчёты и словарь останутся на месте.' : 'Вход откроет сохранённые данные вашего аккаунта. Данные текущего гостевого пространства останутся отдельно.';
    $('submit').textContent = registering ? 'Создать аккаунт и сохранить данные' : 'Войти';
    $('confirm-field').hidden = !registering; $('confirm-password').required = registering;
    $('password').autocomplete = registering ? 'new-password' : 'current-password'; $('password').minLength = registering ? 15 : 1;
    $('password').value = ''; $('confirm-password').value = '';
    for (const [id, selected] of [['login-tab', !registering], ['register-tab', registering]]) { $(id).classList.toggle('selected', selected); $(id).setAttribute('aria-pressed', String(selected)); }
  }
  function render(payload) {
    session = payload; const profile = payload.authenticated || payload.owner;
    $('auth-panel').hidden = profile; $('profile').hidden = !profile;
    $('password').value = ''; $('confirm-password').value = '';
    if (profile) {
      $('profile-title').textContent = payload.owner ? 'Вы вошли как владелец' : 'Вы вошли в аккаунт';
      $('profile-copy').textContent = payload.owner ? 'Для обычной регистрации или входа завершите режим владельца.' : payload.blocked ? 'Доступ аккаунта приостановлен администратором.' : 'Ваши отчёты и словарь доступны при входе с другого устройства.';
      $('profile-note').textContent = payload.owner ? 'Вход владельца и обычный аккаунт имеют отдельные права.' : 'Вход по логину и паролю. Подтверждение и восстановление через почту пока не подключены.';
      $('account-id').textContent = payload.user ? `Аккаунт ${payload.user.id.slice(0, 12)}` : '';
      $('logout').hidden = payload.owner; $('owner-link').hidden = !payload.owner;
    } else {
      $('availability').textContent = payload.blocked ? 'Доступ текущего аккаунта приостановлен. Можно войти в другой аккаунт.' : payload.claimed_guest ? 'Это гостевое пространство закреплено за аккаунтом. Для доступа войдите в него.' : !payload.registration_enabled ? 'Регистрация временно отключена. Вход в существующий аккаунт доступен.' : 'Новый аккаунт сохранит данные этого гостевого пространства.';
      $('register-tab').disabled = !payload.registration_allowed;
      setMode(mode);
    }
  }
  $('login-tab').addEventListener('click', () => { setMode('login'); notice(); });
  $('register-tab').addEventListener('click', () => { setMode('register'); notice(); });
  $('auth-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (mode === 'register' && $('password').value !== $('confirm-password').value) { notice('Пароли не совпадают.', true); $('confirm-password').focus(); return; }
    $('submit').disabled = true; notice(mode === 'register' ? 'Сохраняем аккаунт…' : 'Входим…');
    try { await api(`/api/account/${mode}`, {username: $('username').value, password: $('password').value}); render(await api('/api/account/session')); notice('Готово. Данные аккаунта доступны на сайте.'); }
    catch (error) { notice(error.message, true); }
    finally { $('submit').disabled = false; }
  });
  $('logout').addEventListener('click', async () => {
    $('logout').disabled = true; notice('Выходим…');
    try { await api('/api/account/logout', {}); mode = 'login'; render(await api('/api/account/session')); notice('Вы вышли из аккаунта. Гостевой режим доступен.'); }
    catch (error) { notice(error.message, true); }
    finally { $('logout').disabled = false; }
  });
  (async () => { try { render(await api('/api/account/session')); notice(); } catch (error) { notice(error.message, true); } })();
})();
