# Исток: действующая эксплуатация после отзыва токенов

Текущий релиз — **v36**, source `f60282b400dcf319ea851c7d191b0ac6e363cafc`, image `im-JBmrCaneOkN0ngRTCO1G95`. [Дневной итог](DAY_REPORT_2026-11-15.md). Ночные инструкции для v33/v34 и `night-release-20261115` больше не являются действующим runbook. Их доказательства сохраняются как история; старый verifier может снова принять отозванный токен, поэтому возврат на него запрещён.

## Зафиксированная квалификация

- Day operators: `ROOT/.runtime/day-release-20261115/`, frozen manifest `f1494521…`; точный writer-binding C4 `7ae55801…` относится к source f602/role writer. Нельзя подставлять reader- или night-review вместо него.
- Existing app `ap-6QF6luAbJw2g5O3hqIZqTw`, Volume `vo-6iq0jAz30K9eADRW6OYbCr`, environment main. CPU 0.25, память 512 MiB, min0/max1, без GPU; monthly cap180, spend limit0. Владелец и Qwen metadata сохранены.
- Full source C4 `61aac3a8…`, 1165 Python (1skip), 98 Node, CI 43698916676/две версии Python. В образе 76 файлов, frontend 19 побайтно сохранён; default v11.
- `final-r2/backup-attempt-001`:88 файлов/82 SQLite/79 пространств, pending 0. Локально через HTTP восстановлены 36 отчётов/36 экспортов/1 Qwen; действующие строки и квоты совпали. Это тестовая копия, не восстановление production.
- `final-r2/image-proof.json` и `http-proof.json`: actualimage/source, owner/foreign404,194 GET/55assetsSEO, currentledger171/AI 2/import 1/cash 0; новых поисков/моделей 0. Проверка Google-index отдельно не выполнялась.

## Только совместимый откат к reader

Подготовленная и проверенная цель: **reader v35/source 55e7c1e9267594afb3616f33fbd76220dc117026/image im-I0iQIXwgJrM4T7jhedTiAK**. Полная связка deployment/image/HTTP/source/coverage сохранена в `security-r1/fallback.json` внутри DAY directory. Откат не выполнялся; время восстановления не измерено.

Откат меняет только код и оставляет **текущие** Volume, denylist, ключи, отчёты и ledger. Старые SQLite, счётчики или pre-revocation backup поверх текущих данных не восстанавливать. Уже записанные отзывы reader понимает; его новые logout снова cookie-only. Ранее допущенные задания отзывом не отменяются, а неподтверждённое удалённое сохранение не считается успешным logout.

Для операции нужен тот же bound checkout `ROOT/.runtime/worktrees/night-search-20261115` с чистым tracked tree и точным writer SHA из `final-r2/config.json`: **f60282b**, даже когда цель отката — reader55e. Документационный ref сохраняют отдельно. Перед переключением проверить, что поздний diff только документационный и все 76 runtime-байтов совпадают с `final-r2/deployment.json`; при локальных изменениях остановиться и сохранить их. Проверку `source_identity` не обходить, config не переписывать.

После такой проверки закрепление выполняют в том же checkout:

```sh
ISTOK_ROOT='/Users/air/Documents/ChatGPT/спиздеть с запада'
ISTOK_SOURCE="$ISTOK_ROOT/.runtime/worktrees/night-search-20261115"
ISTOK_OPS="$ISTOK_ROOT/.runtime/day-release-20261115"
ISTOK_PY="$ISTOK_ROOT/.runtime/modal-venv/bin/python"
export MODAL_CONFIG_PATH="$ISTOK_ROOT/.runtime/modal-config.toml"
export ISTOK_RUNTIME_DIR="$ISTOK_ROOT/.runtime"

test -f "$ISTOK_OPS/final-r2/config.json" &&
  test "$(git -C "$ISTOK_SOURCE" rev-parse --show-toplevel)" = "$ISTOK_SOURCE" &&
  test -z "$(git -C "$ISTOK_SOURCE" status --porcelain)" &&
  git -C "$ISTOK_SOURCE" switch --detach f60282b400dcf319ea851c7d191b0ac6e363cafc
```

Продолжать к rollback только после успешного exit0 этого блока; ошибка проверки не разрешает следующую операцию. `ISTOK_SOURCE` — указанный SAMEBOUND checkout, `ISTOK_OPS` — **day-release-20261115**, `ISTOK_PY` — существующий `.runtime/modal-venv/bin/python`, а `MODAL_CONFIG_PATH` и `ISTOK_RUNTIME_DIR` указывают на основной ROOT/.runtime. Новые worktree, paid hosting, другой Volume или generic `modal_web.py --deploy` здесь не используются.

Только при инциденте текущего writer и отсутствии прежнего неизвестного rollback-intent:

```sh
"$ISTOK_PY" "$ISTOK_OPS/rollback.py" final-r2
```

Оператор проверяет новый qualified reader, фиксирует текущее логическое состояние, переключает код один раз, проверяет настоящий image и сохранность текущих данных, затем HTTP. История Modal после отката может получить новый номер; image/source цели важнее предполагаемого номера. **Не выбирать «предыдущую версию» из истории и не запускать bare rollback на v33/v34.**

## Неопределённый исход, копии и повторный выпуск

Publish/rollback intents одноразовые. Ошибка CLI может возникнуть уже после переключения image: сначала сверить официальный history, image и runtime; не удалять intent и не повторять операцию автоматически. Исходные proofs и неудачи сохраняются. Опубликованный sealed release нельзя повторно выпускать, переименовывая его результаты.

Перед новым выпуском нужен новый проверенный source/CI/C4/role binding и свежая копия **текущих** данных с pending 0, полным инвентарём/SQLite и локальным HTTP-восстановлением. Изменение данных после снимка требует следующей нумерованной попытки, а не возврата старых строк ради совпадения счётчика. Транспорт сохраняет полный JSON в existingVolume и сверяет bytes/SHA при download; сбой передачи не разрешает повторять исходную изменяющую операцию. Привязка Modal читается отдельным процессом с timeout90s.

Новые поисковые прогоны в этом релизе не выполняются: v13 не прошёл fresh activation gate, default остаётся v11. Нельзя сбрасывать сайт 171/180, API239/250 или расход предыдущих опытов. Пустая выдача и непрочитанные страницы не доказывают уникальность.

## История

[NIGHT_REPORT](NIGHT_REPORT_2026-11-15.md), исходный `RELEASE_OPERATIONS_2026-11-15.v1.md`, прошлые Git-ревизии этого файла и `ROOT/.runtime/night-release-20261115/` сохраняют ночные факты. Их v33/v34 fallback и команды не применять после writer. Актуальные source/image/data/HTTP receipts — в DAY directory. Документационный commit отделяется от опубликованного runtime; документация сама новый образ не создаёт.
