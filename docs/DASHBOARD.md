# Панель администратора

Этот модуль подготовлен для подключения основным чатом. Сам по себе коммит не публикует `/dashboard` и не меняет production. Обычная регистрация в бете пока не подключена: существующий SaaS email OTP использует `plagiarism/saas.py`, `auth_provider.py` и `AccessLedger`, а beta явно работает с `saas=None`. Покупок и платёжного backend в этом модуле нет.

## Подключение в `cloud/web_beta.py`

Импортировать `from cloud import dashboard`. В `create_server`, после создания beta и HTTP server:

```python
service.dashboard = dashboard.DashboardRuntime(
    beta, dashboard.DashboardConfig.from_env(),
    # Необязательно: существующий AccessLedger для просмотра его metadata.
    # Это не включает SaaSRuntime и не меняет guest tenant IDs.
    account_ledger=None,
    # После интеграции Pool: plagiarism.search_pool_admin.route
    pool_route=None,
)
```

Конструктор также устанавливает `beta.dashboard`. В самом начале `BetaHandler._dispatch`, до определения guest/owner/API identity:

```python
if dashboard.dispatch(self, method):
    return
```

`dispatch` обслуживает `/dashboard`, `/dashboard/`, `/dashboard.html`, `/dashboard.js`, `/dashboard.css` и всё пространство `/api/dashboard`/`/api/dashboard/*`. Административный запрос не создаёт guestspace, не вызывает beta admission/mutation и не расходует его квоту. Неизвестный административный маршрут сначала требует admin auth, затем возвращает 404. Сессия и login — явные исключения: anonymous session возвращает `authenticated:false`; login проверяет пароль после origin/rate checks. Все ответы имеют no-store/noindex и защитные заголовки; JSON body ограничен 8192 байтами.

После определения итогового `self.guest`, но **до** `admit_space`, tenant lease и `mutation`, вызвать:

```python
if not (method == 'POST' and path in {'/api/owner/login', '/api/owner/logout'}):
    dashboard.guard_account(self.server.beta, self.guest)
```

Это относится к guest cookie, owner cookie и аутентифицированному `/api/v1/*` bearer, включая GET session/status/quota, отчёты/экспорт, job polling, API и все check paths. Не помещать guard внутрь `BetaRuntime.identity`: его `except ValueError` поймает `AccessError` и может создать новый guest вместо отказа.

В `owner_login` вызвать `dashboard.guard_account(self, guest)` **после** существующего разрешения `existing[0] if existing else guest` и **до** admission, mutation и adoption. Проверять стабильный owner ID, а не случайного гостя, с которого выполнен вход. Logout остаётся доступным заблокированному аккаунту. Внутри каждой queued operation для correction/library/web check повторить guard перед реальной работой. Уже исполняющийся внешний запрос нельзя отменить задним числом; ожидающий запрос должен получить `account_blocked` до вызова провайдера.

Статика основного сайта остаётся публичной и не создаёт guestspace. Блокировка относится к аккаунту, а не к физическому человеку: анонимный посетитель способен получить новый guest-cookie.

## Конфигурация и сессия

Администратор использует отдельные новые username/password. `ISTOK_OWNER_*`, guest cookie, owner cookie и ключ API никогда не служат запасным admin-входом. В коде нет действующих логинов/паролей.

Загрузчик принимает либо `ISTOK_DASHBOARD_USERNAME`, `ISTOK_DASHBOARD_SALT`, `ISTOK_DASHBOARD_DIGEST`, либо `ISTOK_DASHBOARD_CONFIG` — путь к JSON только с полями `username`, `salt`, `digest`. Файл должен быть обычным файлом mode0600, не symlink. Соль — 32 случайных байта в hex; digest — scrypt output 32 байта, `n=32768,r=8,p=1` (функция `password_digest`). Файл с raw password загрузчик отклоняет. Production получает эти значения через приватную конфигурацию; в образ/manifest они не включаются.

В локальном ROOT/.runtime существуют отдельные файлы handoff credentials и hash config, mode0600; их содержимое нельзя печатать в логи/чаты, коммитить или показывать на screenshot. Путь raw handoff: `ROOT/.runtime/dashboard-credentials-20261115.json`. Его передаёт пользователю основной чат отдельно.

Cookie `istok_dashboard_admin` — HttpOnly, host-only, Path=/, SameSite=Strict, Secure для HTTPS, TTL 1 час. DB хранит только SHA256 токена и fingerprint hash-конфигурации. Повторный login заменяет сессию из предъявленного admin-cookie. Logout удаляет только текущую сессию, и повторное использование токена даёт 401; смена config инвалидирует старые сессии. Лимит login: 10 попыток/IP и 40 суммарно за 15 минут; IP хранится только HMAC-хэшем, попытки старше окна удаляются. `X-Forwarded-For` не используется как доверенный IP. За общим reverse proxy действует дополнительный общий лимит.

GET `/api/dashboard/session` после входа: `{authenticated:true,csrf_token,expires_at}`. POST кроме login требует точный Origin сайта и `X-CSRF-Token` со значением `csrf_token`. Cookie/CSRF не сохраняются в localStorage. При недоступной/повреждённой базе сессий и блокировок операции закрываются с безопасным 503; потерянная база не создаётся чтением автоматически.

## Pool и API

`require_admin(handler,method)` вызывает `handler.server.dashboard.authorize(headers,mutating=...)` и записывает principal в `handler.dashboard_admin`. Допустим также вызов `runtime.authorize(headers,mutating=True/False)` из отдельного адаптера.

`runtime.pool_route` должен иметь сигнатуру `route(handler,method,path,body,*,authorize)`. Dashboard вызывает его только для `/api/dashboard/pool` или его потомков **после** admin auth/Origin/CSRF, передавая `authorize=require_admin`. Pool может повторить guard, но не должен читать vault до него. На `/dashboard/pool`, `/pool.js`, `/pool.css` основной static handler тоже вызывает `require_admin(handler,'GET')`; unauthenticated HTML можно перенаправить на фиксированный `/dashboard?next=pool`. Значение next не должно превращаться в произвольный redirect.

Маршруты аккаунтов: GET `/api/dashboard/accounts?offset=0`, GET `/api/dashboard/audit?offset=0`, POST `/api/dashboard/accounts/{64hex}/block` или `/unblock` с `{reason}`. Причина — 1–240 символов без управляющих символов; изменение и audit фиксируются одной транзакцией. Нельзя менять неизвестный ID. Admin DB: `beta.root/private/dashboard.sqlite3`; login sessions, ограниченные по времени HMAC IP attempts, account_blocks и admin_audit не изменяют owner revocation DB. История admin действий сохраняется, список выдаётся по 50 последних записей с offset. Причина вводится администратором без текстов/PII.

## Значения статистики

Список составляется из реальных beta guests/active_guests/owner, каталогов tenant и существующих admin controls. Cookieless status polling не создаёт фиктивных аккаунтов. Опциональный `account_ledger` читает только существующие SaaS `accounts.id,created_at`; email/email_key и session tokens не извлекаются. Это отдельный источник, не доказательство, что зарегистрированный аккаунт подключён к beta.

- `web_checks_admitted`: beta checks минус unstarted_checks, всего/сегодня. Это принятые поисковые задания, включая некоторые сбои/ожидание, а не число готовых отчётов.
- `reports_total/reports_active`: сохранённые отчёты всех режимов, отдельно с учётом корзины. `characters_*`: сумма имеющегося целого `stats.total_characters` по всем сохранённым отчётам, включая корзину; отчёты без счётчика подсчитаны отдельно. Это не сумма знаков всех исторических попыток.
- `last_usage`: точный timestamp последней записи, если он имеется; иначе последний известный день действия по UTC; иначе null. `first_seen_month` имеет точность месяца. Это не «последний вход».
- `errors_*`: только сохранённые failed model-job, безопасный allowlist кодов; произвольный код заменяется `job_failed`. Error text/details не загружаются. Ошибки HTTP, не сохранённые как job, в эти числа не входят.
- `queue`: реальный active/capacity shared coordinator для library/web; это не история очереди и не очередь correction.
- `payments.configured=false,purchases=null`: нет подключённого источника покупок. Бесплатный/owner доступ не выдаётся за оплаченную подписку.

Tenant базы открываются SQLite mode=ro. Не создаётся Store и не запускаются миграции/загрузка текстов; symlink не читается. Недоступная или старая схема показывается как «нет данных», не нулевое использование. Итоги в верхней части UI по знакам/ошибкам относятся к текущей странице.

## Проверка и откат

`python3 -m unittest tests.test_dashboard tests.test_web_beta tests.test_owner_session_revocation tests.test_saas tests.test_access` и `node --check web/dashboard.js`. `tests/test_dashboard.py` содержит реальный HTTP/SQLite integration adapter для описанного изменения parent-owned handler. Это проверка seam, а не доказательство публикации или интеграции в ещё не изменённый parent.

Для rollback убрать публикацию dashboard assets/API и конфигурацию admin login; сохранить созданную DB и `guard_account`, если блокировки должны продолжать действовать. Возврат к старому серверу без guard отключит блокировки и требует явного решения оператора. Не удалять базу ради обхода 503. Конструктор с `config=None` сохраняет работу guard при закрытом admin-входе. DB находится под постоянным beta root; marker `runtime/dashboard-initialized` запрещает повторный пустой bootstrap после потери DB. При интеграции необходимо включить оба файла в backup/restore и убедиться, что persist callback фиксирует Volume. Здесь Volume и текущие учётные записи не изменялись.
