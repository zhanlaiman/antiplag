"use strict";

(() => {
  const labels = {serpapi: "SerpApi", tavily: "Tavily", serper: "Serper", exa: "Exa"};
  const codes = {
    not_checked: "Квота ещё не проверена.", free_verified: "Бесплатная квота подтверждена.",
    quota_unconfirmed: "Бесплатный тариф не подтверждён. Подключение заблокировано.",
    provider_unavailable: "Провайдер не ответил. Попробуйте проверить позже.",
    quota_exhausted: "Доступный бесплатный бюджет исчерпан.",
    balance_unconfirmed: "Остаток провайдера не подтверждён. Нужен отдельный подтверждённый бесплатный бюджет."
  };
  const errors = {
    pool_invalid_input: "Проверьте провайдера и формат ключа.",
    pool_body_too_large: "Ключ слишком длинный.", pool_check_required: "Проверьте бесплатную квоту перед подключением.",
    pool_check_rate_limited: "Подождите несколько секунд перед повторной проверкой.",
    pool_grant_required: "На сервере нет действующего одобренного бюджета для этого ключа и аккаунта.",
    pool_recovery_required: "Сохранение не завершено. Нужна помощь владельца сервера с восстановлением пула.",
    pool_storage_unavailable: "Хранилище ключей недоступно. Изменение не подтверждено.",
    dashboard_csrf_invalid: "Обновите страницу и повторите действие."
  };
  const $ = id => document.getElementById(id);
  let csrf = "", busy = false, writable = true;
  const number = value => Number.isInteger(value) && value >= 0 ? value.toLocaleString("ru-RU") : "—";
  const date = value => typeof value === "number" && Number.isFinite(value)
    ? new Intl.DateTimeFormat("ru-RU", {day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit"}).format(new Date(value * 1000)) : "Не проверялось";
  function el(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  function notice(text, kind = "") {
    $("notice").className = "notice " + kind;
    $("notice").textContent = text;
    $("notice").hidden = false;
  }
  function sessionExpired() {
    $("workspace").hidden = true;
    csrf = "";
    location.replace("/dashboard?next=pool");
  }
  async function api(path, payload) {
    const options = {credentials:"same-origin", cache:"no-store", redirect:"error", headers:{Accept:"application/json"}};
    if (payload !== undefined) {
      options.method = "POST";
      options.headers["Content-Type"] = "application/json";
      options.headers["X-CSRF-Token"] = csrf;
      options.body = JSON.stringify(payload);
    }
    let response, result;
    try {
      response = await fetch(path, options);
      result = await response.json();
    } catch (_) {
      throw new Error("Не удалось связаться с сервером. Проверьте соединение и обновите данные.");
    }
    if (response.status === 401) {
      sessionExpired();
      throw new Error("Войдите в панель администратора.");
    }
    if (!response.ok) throw new Error(errors[result.code] || (response.status === 403
      ? "Доступ запрещён. Обновите страницу и войдите в панель администратора."
      : "Изменение не подтверждено. Обновите данные и повторите действие."));
    return result;
  }
  function setBusy(value) {
    busy = value;
    $("api-key").disabled=value||!writable;$("provider").disabled=value||!writable;
    $("workspace").setAttribute("aria-busy", String(value));
    for (const button of document.querySelectorAll("button")) button.disabled = value || button.dataset.blocked === "true";
  }
  function render(pool) {
    $("workspace").hidden = false;
    $("monthly-limit").textContent = number(pool.monthly_limit);
    $("monthly-used").textContent = number(pool.monthly_used);
    $("runtime-note").textContent = pool.runtime_connected ? "По данным серверного учёта" : "Серверный учёт пока не подключён";
    if(!writable)$("runtime-note").textContent+=" · Только просмотр";
    $("revision").textContent = "Версия " + number(pool.revision);
    const health = {disabled:["Отключён","neutral"],unknown:["Не подтверждён","warning"],healthy:["Подключён",""],stale:["Проверка устарела","warning"],blocked:["Заблокирован","danger"],exhausted:["Квота исчерпана","warning"],recovery_required:["Восстановление","danger"]};
    const providerRows = [];
    for (const provider of pool.providers) {
      const row = el("tr"); row.append(el("td", labels[provider.provider] || "Провайдер"));
      const state = health[provider.health] || health.unknown;
      const status = el("td"); status.append(el("span",state[0],"badge "+state[1]));
      if (provider.low_quota) status.append(el("small","Мало бесплатных запросов","low-note"));
      const remaining = el("td",number(provider.provider_remaining));
      remaining.append(el("small",provider.balance_source === "provider_live" ? "По API провайдера" : "Остаток провайдера не подтверждён"));
      const local = el("td", number(provider.local_used)+" / "+number(provider.local_remaining));
      if (provider.grant_approved) local.append(el("small","Одобренный бюджет: "+number(provider.grant_remaining)+" · до "+date(provider.grant_expires_at)));
      row.append(status, remaining, local, el("td",date(provider.checked_at))); providerRows.push(row);
    }
    $("provider-rows").replaceChildren(...providerRows);
    $("key-count").textContent = number(pool.credentials.length);
    $("empty-state").hidden = pool.credentials.length > 0;
    const credentials = [];
    for (const credential of [...pool.credentials].reverse()) {
      const item = el("li", undefined,"credential");
      const top = el("div",undefined,"credential-top");
      top.append(el("span",labels[credential.provider],"credential-title"),el("span",credential.active?"Активный ключ":"Сохранён", "badge "+(credential.active?"":"neutral")));
      const mask = el("p",undefined,"credential-details"); mask.append(el("code",credential.fingerprint+" · ••••"+credential.last4));
      const details = el("p",codes[credential.code] || "Квота не подтверждена.","credential-details");
      const checked = el("p","Проверено: "+date(credential.checked_at)+(credential.checked_at&&!credential.fresh?" · обновите проверку":""),"credential-details");
      item.append(top,mask,details,checked);
      if (credential.grant) item.append(el("p","Бюджет на момент подключения: "+number(credential.grant.remaining)+" · срок до "+date(credential.grant.expires_at)+". Текущий остаток — в таблице выше.","credential-details"));
      if (credential.provider === "exa") item.append(el("p","Exa: до $0.004 из подтверждённого бесплатного гранта за запрос; денежные расходы $0.","credential-details"));
      const actions = el("div",undefined,"credential-actions");
      const addAction = (title, action, payload, blocked=false) => {
        const button = el("button",title,"button secondary"); button.type="button";button.disabled=blocked||busy||!writable;button.dataset.blocked=String(blocked||!writable);
        button.addEventListener("click",()=>mutate(action,payload));actions.append(button);
      };
      addAction("Проверить квоту","validate",{credential_id:credential.credential_id},pool.recovery_required);
      if (credential.active) addAction("Отключить","disable",{provider:credential.provider},pool.recovery_required);
      else {
        const live = ["serpapi","tavily"].includes(credential.provider);
        const blocked = pool.recovery_required || live&&(!credential.free_verified||!credential.fresh||!credential.guard_remaining);
        addAction("Подключить","activate",{credential_id:credential.credential_id},blocked);
      }
      item.append(actions);credentials.push(item);
    }
    $("credential-rows").replaceChildren(...credentials);
    $("save-key").dataset.blocked=String(pool.recovery_required||!writable);
    if (pool.recovery_required) notice("Сохранение пула не завершено. Новые подключения заблокированы до восстановления владельцем сервера.","error");
    else if (!pool.runtime_connected) notice("Ключи сохранены в хранилище. Серверный учёт расходов пока не подключён.","warning");
  }
  async function mutate(action, payload) {
    if (busy) return;
    if (!writable) {notice("Панель временно доступна только для просмотра.","warning");return;}
    setBusy(true);$("form-error").hidden=true;
    notice(action==="validate"?"Проверяем бесплатную квоту…":"Сохраняем изменение…");
    try {
      const result = await api("/api/dashboard/pool/"+action,payload);
      render(result.pool);
      if (!result.pool.recovery_required && result.pool.runtime_connected) notice(action==="validate"?"Проверка завершена. Результат показан у ключа.":"Изменение сохранено.","success");
    } catch (error) {
      notice(error.message,"error");
      if (action==="stage") {$("form-error").textContent=error.message;$("form-error").hidden=false;}
    } finally {setBusy(false);}
  }
  $("key-form").addEventListener("submit",event=>{
    event.preventDefault();if(busy)return;
    const payload={provider:$("provider").value,api_key:$("api-key").value};
    $("api-key").value=""; // No key survives submission in DOM or browser storage.
    mutate("stage",payload);
  });
  $("refresh").addEventListener("click",async()=>{
    if(busy)return;setBusy(true);notice("Обновляем данные…");
    try {const pool=await api("/api/dashboard/pool");render(pool);if(pool.runtime_connected&&!pool.recovery_required)notice("Данные обновлены. Остатки провайдеров — на время последней проверки.","success");}
    catch(error){notice(error.message,"error");}finally{setBusy(false);}
  });
  async function start(){
    try{
      const session=await api("/api/dashboard/session");
      if(session.authenticated!==true||typeof session.csrf_token!=="string"){sessionExpired();return;}
      csrf=session.csrf_token;writable=session.business_writes_enabled!==false;
      const pool=await api("/api/dashboard/pool");render(pool);
      if(pool.runtime_connected&&!pool.recovery_required)notice("Данные загружены. Выберите сохранённый ключ для проверки квоты.");
      setBusy(false);
    }catch(error){notice(error.message,"error");$("refresh").disabled=false;}
  }
  start();
})();
