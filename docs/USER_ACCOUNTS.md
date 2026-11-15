# Обычные аккаунты в бете

Локальный модуль готовит регистрацию по логину и паролю с сохранением прежнего guestspace. Публикация и интеграция в основной handler выполняются основным чатом; наличие этого документа само по себе не означает, что регистрация уже работает на домене. Почта, проверка адреса и восстановление по письму не подключены; письма не отправляются. Оплата и платные планы не добавлены.

Используются существующие `AccessLedger` sessions/devices/CSRF/rate primitives и cookie names `istok_session`/`istok_device`. `SaaSRuntime`/`EmailProvider` не включаются, provider-based OTP код остаётся самостоятельным и неизменённым. Локальный password adapter не вызывает `ensure_identity` с выдуманной верификацией. Его DTO всегда содержит `email_verified:false`, `auth_method:"password"`, `plan:"free"`.

## Точный контракт подключения

```python
from cloud import dashboard, user_auth

service.dashboard = dashboard.DashboardRuntime(beta, dashboard.DashboardConfig.from_env(), pool_route=pool_route,
                                               business_writes_enabled=False)  # True только в writer
service.accounts = user_auth.UserAuthRuntime(
    beta,
    registration_enabled=False,  # aware-reader; True только в writer
)
```

Оба runtime прикрепляются также к `beta.dashboard` и `beta.accounts`. В начале `BetaHandler._dispatch`, до любого определения tenant/guest/admission:

```python
if dashboard.dispatch(self, method):
    return
if user_auth.dispatch(self, method):
    return
```

Нормальные запросы с cookie сохраняют прежнюю owner-логику. После `owner_identity` и `beta.identity`, но **до** `guard_account`, admission, tenant lease и mutation:

```python
self.user = None
if not self.owner and not (method == 'POST' and path in {'/api/owner/login', '/api/owner/logout'}):
    resolved = beta.accounts.resolve(self.headers, self.guest)
    self.guest = resolved.guest
    self.user = resolved.user
dashboard.guard_account(beta, self.guest)
```

Owner login/logout остаются исключениями из обычного resolver/admission gate. Последняя строка выше тоже сохраняет существующее logout-исключение из dashboard-инструкции. Regular account **не** устанавливает `self.owner`: гостевые технические лимиты и запреты owner/API-key/admin операций продолжают действовать. `/api/v1/*` bearer использует прежнюю scoped authentication; он всё равно проверяет block по каноническому guest ID. Resolver не заменяет API bearer.

Внутри `owner_login`, в его существующем `BEGIN IMMEDIATE`, **после** `guest = existing[0] if existing else guest`, **до** admission/mutation/adoption:

```python
beta.accounts.guard_owner_adoption(guest)  # внутри метода: self.accounts...
dashboard.guard_account(beta, guest)
```

Это обязательная часть race-защиты: регистрация берёт `BEGIN IMMEDIATE` той же beta SQLite, проверяет owner target и держит lock до commit credentials/account/binding в user DB и записи защитного счётчика. Порядок блокировок всегда beta → user DB. Если регистрация выиграла, первый owner login не усыновит её пространство; если owner выиграл, регистрация отклоняется. `_admit_space` нельзя безусловно оборачивать owner-adoption guard: обычный зарегистрированный пользователь должен пользоваться своим пространством.

Существующие queued entry checks из dashboard-инструкции остаются. Normal account root, reports, status, polling и check paths проходят resolver/guard до доступа к данным. Не помещать guard/resolve внутрь `BetaRuntime.identity` с его `except ValueError`: `AccessError` не должен превращаться в новый guest. При `/api/session` основной handler может включить `self.user` как безопасный DTO, сохраняя beta/local режим. Ссылку на `/account` и namespace клиентских черновиков по итоговому canonical beta ID добавляет основной чат в свой `web/app.js`.

## Reader → writer и откат

`registration_enabled=False` — безопасное значение по умолчанию. GET auth bootstrap, login/logout, чтение bindings, защита claimed guest и owner-adoption guard работают в обоих ролях на одной схеме. Reader запрещает только создание нового аккаунта через POST register: `503 user_registration_disabled`, без новых account/credential/binding rows. Writer передаёт `True`. UI показывает фактическую доступность регистрации, не технические названия ролей.

После первой регистрации старый guest-only релиз не является допустимым rollback: он не знает, что гостевой cookie уже недостаточен для доступа. Fallback должен содержать этот reader/resolver и все guards. Нельзя откатывать пользовательскую DB или очищать bindings/markers при source rollback. Структурный bootstrap и создание/отзыв сессий происходят и в reader; флаг не обещает физическую read-only БД.

## Регистрация, вход и выход

GET `/api/account/session` выдаёт/проверяет device, создаёт signed beta guest cookie при необходимости, возвращает `csrf_token`, `user|null`, `authenticated`, `owner`, `claimed_guest`, `blocked`, `registration_enabled`, `registration_allowed`. Эта control-plane точка позволяет войти/выйти даже при блокировке, но не возвращает отчёты, лимиты, тексты или содержимое tenant. Запись нового device на GET явно persist-ится; существующий valid device не создаёт новых записей. Создание ограничено IP/global rate.

POST `/api/account/register` и `/login` принимают **только** `{username,password}`; POST `/logout` — `{}`. Все POST требуют точный Origin сайта, валидный device и `X-Istok-CSRF`, привязанный к device и предъявленному session token. JSON до 8192 байт; duplicate cookies/headers и неизвестные поля отклоняются. Нет bearer/owner/admin fallback в normal registration. Owner mode нужно завершить прежде, чем регистрировать обычный аккаунт.

Регистрация требует именно входящий валидный подписанный guest cookie: если `beta.identity` создал новый fallback, запрос отклоняется и клиент должен выполнить bootstrap. Она закрепляет только текущий unclaimed/non-owner/non-blocked guestspace, не копирует и не переименовывает данные. Account ID — новый random64, guest ID и каталог остаются прежними. После claim старый signed guest cookie без matching account session получает `401 account_login_required` на всех data/session/check путях.

Вход всегда возвращает сохранённый guest ID аккаунта. Другой текущий guestspace не объединяется с ним и не удаляется. Выход отзывает только предъявленную account session. Если текущий beta cookie указывает на claimed space/невалиден, браузер получает новый signed anonymous guest cookie без admission или tenant creation; replay старого claimed cookie остаётся запрещён. Если вход выполнялся из другого unclaimed guestspace, logout сохраняет его cookie и доступ к его прежним гостевым данным. Содержимое и DB старых пространств никогда не удаляются.

Сессия использует существующий TTL 12 часов; opaque token хранится как SHA256, cookie HttpOnly, Secure на HTTPS, SameSite=Lax, host-only. Password/CSRF/session не пишутся в localStorage. Логин NFKC/casefold, 3–32 символа латиницы/цифр/`._-`; первый символ — буква/цифра. Хранится только HMAC логина, не raw username или email. Пароль 15–256 символов, проверяются очевидные слабые варианты; scrypt `n32768,r8,p1,dklen32`, общий с admin process-wide лимит 2 одновременных hashes. На перегрузку оба транспорта возвращают `429 auth_busy`, `Retry-After:5`. Неизвестный логин проходит dummy scrypt с той же стоимостью; ошибка входа общая. Rate events ограничивают device/IP/target/global и переживают restart. За reverse proxy IP может быть общим: `X-Forwarded-For` не принимается как доверенный источник.

## Хранение и защита повреждённого состояния

Файлы под persistent beta root: `private/user-access.sqlite3`, `private/user-access.sqlite3.key`, `runtime/user-access-initialized`. Все приватные файлы mode0600. Ключ принадлежит `AccessLedger`; его потеря не заменяется новым автоматически. Новые таблицы в **той же** user AccessLedger DB: `local_credentials(account_id,login_key UNIQUE,salt,digest)` и `account_guest_bindings(account_id,guest_id UNIQUE,bound_at)`, FK к accounts. `accounts.email_key=''` — документированный sentinel отсутствия email, не HMAC логина.

До constructor DDL существующая DB проверяется на обязательные таблицы/колонки, PK, unique login/guest, FK и целостные account–credential–binding triples. Live session/claim/resolver/public-bindings тоже проверяют triples. Ongoing connections используют mode=rw, поэтому потерянная DB не создаётся чтением. Missing/corrupt DB/key/schema, orphan triples и повреждённые идентификаторы закрывают доступ с безопасным 503, не anonymous fallback.

Marker содержит `user-access-v1:N`, монотонный минимум числа созданных аккаунтов. После атомарного commit новой регистрации, пока ещё удерживается beta claim lock, он заменяется через fsync/atomic replace до успешного ответа. Live/restart проверка отвергает DB с количеством ниже marker. Поэтому удаление всех связанных строк или восстановление более старой user DB отдельно от marker не открывает прежний guest-cookie. Удаление аккаунтов в этом релизе не реализовано: нужны отдельная политика и миграция защитных anchors. При backup/restore все три файла и соответствующие tenant данные должны принадлежать одному согласованному snapshot; marker/DB не удаляются ради обхода 503.

Runtime требует точного совпадения account count и marker. Если marker update не удался после commit, последующие login/session тоже закрыты с 503, без выдачи cookie. Constructor также берёт beta claim lock для всего bootstrap/preflight/marker update (`claim(None)`): конкурентный reader initializer не может понизить marker writer. Проверенная целостная DB с более высоким count после такой неуспешной регистрации может быть безопасно доведена вперёд при restart под этим lock; потеря строк ниже уже сохранённого marker не восстанавливается автоматически. FK сессий/операций и полные, не partial, уникальные индексы credentials/binding обязательны. Неожиданный тип объекта `owner_account` закрывает claim, а не означает отсутствие владельца.

Dashboard использует только valid triples: одна строка по canonical guest ID с `registered_account_id` и source `beta_local_password`; метрики берутся из прежнего tenant. Raw login, email, salt/digest и password в inventory не извлекаются. Блокировки guest/account aliases объединяются через OR; `block_sources` объясняет источник. Block/unblock через любой alias меняет оба в одной admin transaction с одной canonical audit записью. API bearer и старый guest тоже видят block через canonical alias lookup.

## Проверка

`python3 -m unittest tests.test_user_auth tests.test_dashboard tests.test_access tests.test_saas tests.test_owner_session_revocation`; `node --check web/account.js` и `web/dashboard.js`. Tests используют настоящие временные SQLite и HTTP adapter с теми же integration hooks, новый пароль генерируется для QA, провайдеры/почта не вызываются. Проверяются owner/register race, два одновременных signup на guest, reader/login/logout, роль и CSRF, old-cookie replay, two-browser data separation, alias block/unblock, lost file/key/schema/triples/whole registry rows, hash contention и persist device GET. Браузерная QA `/account` выполняется отдельно с синтетическими данными; результаты находятся в external devlog, не среди пользовательских данных. Это не заменяет проверку parent-owned deployed handler.
