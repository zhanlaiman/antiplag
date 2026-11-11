'use strict';
const setupForm = document.querySelector('#search-setup-form');
setupForm.addEventListener('submit', async event => {
  event.preventDefault();
  const field = document.querySelector('#search-api-key');
  const button = setupForm.querySelector('button');
  const result = document.querySelector('#setup-result');
  button.disabled = true;
  result.textContent = 'Сохраняем подключение…';
  try {
    const response = await fetch('/api/local-search-config', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider: 'tavily', api_key: field.value.trim() }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Не удалось сохранить подключение.');
    field.value = '';
    result.textContent = 'Ключ сохранён. Можно перейти к проверке и выполнить первый живой запрос.';
  } catch (error) {
    result.textContent = error.message || 'Не удалось сохранить подключение.';
  } finally {
    button.disabled = false;
  }
});
