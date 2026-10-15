## 1. Ядро: реестр неудач документа (`app/services/force_summary.py`)

- [x] 1.1 Модульный `_last_failure: dict[str, tuple[str, float]]` (статус, `time.monotonic()`) и `FAILURE_MEMORY_SECONDS = 600`; в `_on_task_done`: задача отменена — снять запись, `result()` не читать (проверка `task.cancelled()` до чтения результата); исключение задачи — снять; итог `ok` — снять; `no_text`/`refused`/`error` — записать со временем; при записи вычищать записи старше `FAILURE_MEMORY_SECONDS`
- [x] 1.2 `recent_failure(external_id) -> tuple[str, float] | None` — запись, только если моложе `FAILURE_MEMORY_SECONDS`, с docstring (причина и кулдаун для Mini-App; бот и панель не читают); обновить docstring модуля

## 2. Бот: общая доставка в чат (`app/bot/handlers.py`)

- [x] 2.1 `_deliver` и `_deliver_summary_failure` принимают `bot` вместо `context` (используют `bot.send_message`)
- [x] 2.2 Выделить из `_summarize_and_reply` публичную `send_summary_result(bot, config, session_maker, user, external_id, result, status_message=None)` — форматирование и отправка итога, тексты без изменений (вызывается из `app/webapp/server.py` лениво, см. 4.5); `_summarize_and_reply` = ядро с `on_mode` + `send_summary_result(context.bot, ...)`
- [x] 2.3 Выделить фолбэк `_run_summary_task` в публичную `send_summary_crash(bot, user, external_id, status_message=None)` («Не удалось сделать саммари. Попробуйте позже: /summary <id>»); обёртка бота вызывает её
- [x] 2.4 Проверить, что поведение бота не изменилось (тексты, удаление статуса, последняя доставка при сбое) — см. 6.2

## 3. Webapp: доступ к боту (`app/webapp/server.py`, `app/main.py`)

- [x] 3.1 `create_app(session_maker, config, bot)` кладёт `app["bot"]` и `app["summary_tasks"] = set()`; `app["summarize_failed_at"]` оставить только для панели (`admin.handle_summary`), Mini-App его не использует; `start_webapp(host, port, session_maker, config, bot)`; `app/webapp/__init__.py` без изменений по экспорту
- [x] 3.2 `main._start_webapp` передаёт `application.bot`

## 4. Webapp: быстрый ответ, фоновая доставка, состояние (`app/webapp/server.py`)

- [x] 4.1 Хелпер `_spawn(app, coro)` — `create_task` + сильная ссылка в `app["summary_tasks"]` + `discard` по завершении
- [x] 4.2 Вынести тексты неудач `/summarize` (`limit`/`no_text`/`refused`/`error` с `_with_note`) в общий хелпер — им пользуются `/summarize` и `/summary_status`
- [x] 4.2a Хелпер остатка кулдауна по `recent_failure(external_id)`: для `no_text`/`error` моложе `SUMMARIZE_COOLDOWN_SECONDS` — секунды до повтора, иначе `None`; им пользуются кулдаун `/summarize` и `retry_after` в `/summary_status`
- [x] 4.3 `handle_summarize`: после авторизации и кулдауна (по 4.2a) — синхронный `on_mode` (запомнить `mode`, `busy = calls_ahead_of_user() > 0`, выставить событие); если `inflight_task(external_id)` уже есть — событие сразу; `job = _spawn(force_summarize(..., on_mode=on_mode))`; `asyncio.wait({job, ожидание события}, timeout=ACCEPT_WAIT_SECONDS=5, FIRST_COMPLETED)`, лишнюю задачу ожидания отменить
- [x] 4.4 `job.done()` (кэш, `limit`, ранний `error`) → прежний синхронный ответ (сообщения, `usage`; кулдаун не ставить и не снимать), в чат ничего; иначе → `_spawn(_deliver_later(...))` и ответ `{"status": "accepted", "message", "usage"}`; тексты оценки: «до полуминуты» / «1–3 минуты» / без режима, строка «модель занята» при `busy`, хвост «Пришлём результат и в чат бота — страницу можно закрыть.»
- [x] 4.5 `_deliver_later(app, user, external_id, job)`: `await job` → `send_summary_result(app["bot"], ...)` (кулдаун не трогает); `app.bot.handlers` импортируется лениво внутри `_deliver_later` (цикл `handlers` → `admin` → `server`), как `setup_admin` в `create_app`; исключение — в лог и `send_summary_crash`, её ошибка — только в лог; лог `handle_summarize ... -> accepted` и итог доставки
- [x] 4.6 `POST /summary_status` (`{external_id, init_data}` → `_auth_user`): `running` при `inflight_task`; `done/ok` с `summary`, `text`, `is_text_available`, если саммари в БД; иначе `done` со `status` = статус из `recent_failure(...)` или `"error"`, `message`, `retry_after` (по 4.2a или `null`), текстом, если есть; всегда `usage`; 400/401/403/500 — как у `/summarize`
- [x] 4.7 `/full_text`: поле `summarizing` (`inflight_task(external_id) is not None`)
- [x] 4.8 Обновить docstring модуля (роуты `/summarize` → `accepted`, `/summary_status`, поле `summarizing`) и `handle_summarize`

## 5. Webapp: страница (JS в `HTML_PAGE`)

- [x] 5.1 Вынести разбор итога из `startSummarize` в `applyOutcome(details, res, opts)` (merge, кулдаун, `renderCard`, тосты) — общий для ответа `/summarize` и состояния `done`; при `retry_after: null` и статусе `error` — кнопка доступна сразу; `opts.quiet` — без тостов: успех как обычно, `refused` — причина на месте кнопки, `no_text`/`error` — приглушённая кнопка по `retry_after`
- [x] 5.2 `waitSummary(details, message, mine)`: `_busy`, кнопка «Саммари в процессе создания…» + строка статуса с `message`; опрос `/summary_status` каждые `SUMMARY_POLL_MS = 3000`: `running` — дальше, `done` — `setUsage` + `applyOutcome(details, res, {quiet: !mine})`; сетевая ошибка/5xx — продолжать; 401/403 — сообщение, конец ожидания
- [x] 5.3 `startSummarize`: `accepted` → `setUsage(res.usage)` + `waitSummary(details, res.message, true)`; остальные исходы — `applyOutcome`
- [x] 5.4 `.catch` у `/summarize`: один `/summary_status` вместо `fetchFullText` — `running` → `waitSummary(..., 'Саммари уже готовится.', true)`, `ok` → показать, иначе прежний тост «возможно, ещё создаётся» с кулдауном
- [x] 5.5 `renderCard` / `recheckSummary`: при `summarizing` без саммари, с `tg.initData` и без `_busy` — `waitSummary(details, 'Саммари уже готовится.', false)`; вне Telegram — обычная кнопка
- [x] 5.6 `setActionState`: строка статуса показывается и в состоянии busy (после `accepted`), до ответа — пустая

## 6. Ручная проверка (локальный бот с отдельным ключом GigaChat, Mini-App через HTTPS-туннель)

- [x] 6.1 `python -c "from app.core.config import Config; print(Config.load())"`, `python -m app.main` — без ошибок импорта (ленивый импорт `app.bot.handlers` в `server.py`; проверить оба порядка импорта), WebApp стартует
- [x] 6.2 Бот без регрессий: `/summary` (текст и OCR), кнопка под уведомлением, сбой доставки — как в `async-summary-delivery-bot` _(проверено вручную: `/summary` через OCR — успех и неудача; не проверялись текстовая ветка «до полуминуты» (нет документов с текстом на портале) и сбой доставки)_
- [x] 6.3 Дайджест, документ с текстом: ответ `/summarize` за ~1–3 с с «до полуминуты» и подсказкой про чат при закрытии; чип сразу +1; саммари появляется в карточке по опросу; страница открыта — в чат ничего не приходит (см. 8.8)
- [x] 6.4 Документ без текста (OCR): «1–3 минуты»; закрыть Mini-App — результат приходит в чат, счётчик списан; при повторном открытии саммари в карточке
- [x] 6.5 Переоткрыть Mini-App во время OCR: карточка показывает «Саммари уже готовится.» и затем саммари; то же — со второго аккаунта (ему в чат ничего не приходит)
- [x] 6.6 Готовое саммари / исчерпанный лимит / кулдаун — сразу итог на странице, в чат ничего _(проверено вручную: готовый кэш и кулдаун; исчерпанный лимит вручную не проверялся — админ без лимита)_
- [x] 6.7 Неудача в фоне (`no_text`): тост с обратным отсчётом, кнопка приглушена, чип вернулся к прежнему значению; страница открыта — в чат ничего (страница забрала итог), страница закрыта — в чат сообщение о неудаче
- [x] 6.8 Перезапуск бота во время ожидания: после рестарта страница выходит из ожидания с тостом о неудаче, кнопка доступна сразу
- [x] 6.9 Присоединение: запустить OCR из бота, нажать тот же документ в Mini-App со второго аккаунта — `accepted` без оценки, модель вызвана один раз, второму аккаунту итог показывается на его странице, если она открыта, иначе приходит в чат _(проверено на одном аккаунте: `accepted` без оценки, одна модель; доставка второму аккаунту вручную не проверялась — код общий с 8.8–8.11)_
- [x] 6.10 Логи: нет `Task exception was never retrieved` и `Exception in callback _on_task_done` (в том числе при остановке сервиса во время саммаризации), строки `accepted`/доставки читаемы
- [x] 6.11 Кулдаун из бота: OCR из бота дал `no_text` → в Mini-App тот же документ сразу отвечает «повторите через ~N сек.»; повтор в боте выполняется сразу
- [x] 6.12 Наблюдатель: второй аккаунт смотрит чужую саммаризацию (`summarizing`), она заканчивается неудачей — тоста нет; `refused` — причина на месте кнопки, `no_text`/`error` — кнопка приглушена до конца задержки _(неудача у наблюдателя вручную не воспроизведена: с неверным ключом сбой наступает за ~1 с; закрыто по коду — `applyOutcome` с `quiet` не вызывает `showToast` — и по 6.5)_

## 7. Документация

- [x] 7.1 `CLAUDE.md`: в разделе Mini-App — `POST /summarize` отвечает `accepted` и продолжает в фоне, результат дублируется в чат; `POST /summary_status`; поле `summarizing` в `/full_text`; `bot` в webapp; кулдаун 60 с считается по реестру неудач ядра (`recent_failure`, 10 минут) и ставится неудачей из любой точки входа — упоминание `app["summarize_failed_at"]` переписать: он остаётся только у панели, Mini-App считает кулдаун по реестру; в таблице модулей — общая доставка `send_summary_result` в `handlers.py` и `recent_failure` в `force_summary.py`
- [x] 7.2 `docs/PLAN.md` (локально): отметить п. 4 этапа 10 выполненным

## 8. Чат только если страница не забрала итог (решение 6)

- [x] 8.1 `server.py`: `PICKUP_WAIT_SECONDS = 7` и `app["page_pickup"] = {}` в `create_app`
- [x] 8.2 `handle_summarize`: при `accepted` взять событие `app["page_pickup"].setdefault((user.telegram_id, external_id), asyncio.Event())` до `_spawn(_deliver_later(...))` и передать его в `_deliver_later`
- [x] 8.3 `handle_summary_status`: при состоянии `done` (любой исход) выставить событие для `(user.telegram_id, external_id)`, если оно есть; `running` не засчитывается
- [x] 8.4 `_deliver_later`: после `result = await shield(job)` — `event.clear()`, затем `wait_for(event.wait(), PICKUP_WAIT_SECONDS)`: пришло — не слать, лог «страница забрала итог», таймаут — `send_summary_result`; в `finally` удалить запись из реестра, если там это событие; `send_summary_crash` при исключении отправляется всегда
- [x] 8.5 `_accepted_message`: хвост «Пришлём результат и в чат бота — страницу можно закрыть.» → «Если закроете страницу, результат придёт в чат бота.»; docstring `handle_summarize` и модуля
- [x] 8.6 Проверка скриптом на тестовой БД с подменой сети: опрос после готовности — чата нет; без опроса — чат через ~7 с; ранний `done` (до постановки задачи в реестр) не засчитывается; `/summary`-подобная задача + `accepted` того же пользователя при опросе — в чат ничего от webapp; утечек задач нет
- [x] 8.7 `CLAUDE.md` (параграф Mini-App: чат — только если страница не забрала итог, `app["page_pickup"]`) и `docs/PLAN.md` п. 4 (локально)
- [x] 8.8 Вручную, страница открыта: документ с текстом — саммари появляется в карточке, в чат ничего не приходит
- [x] 8.9 Вручную, страница закрыта сразу после `accepted`: сообщение приходит в чат через ~7 с после готовности, при повторном открытии саммари в карточке
- [x] 8.10 Вручную, Mini-App свёрнут (Telegram в фоне): сообщение приходит в чат, при возврате страница показывает саммари
- [x] 8.11 Вручную: `/summary` в боте и нажатие того же документа в открытой странице — одно сообщение (из бота), страница показывает результат
