## 1. Распознавание «нет информации» (`app/services/importance.py`)

- [x] 1.1 Добавить константы `_NO_INFO = -2` и `NO_INFO_FALLBACK_SCORE = 1` (с комментарием, почему `1`, а не `2`) и регулярку `"level"\s*:\s*null\b` для сломанного JSON
- [x] 1.2 `_parse_level`: валидный JSON с `"level": null` → `_NO_INFO`; в регулярном фолбэке — то же самое; docstring обновить
- [x] 1.3 `_ask`: принять `ref: str | None = None`, вернуть `_NO_INFO` без второй попытки (проверить, что существующая ветка `level is not None` это уже делает), в строку «невалидный ответ» добавить `ref`; обновить Returns в docstring

## 2. `score_importance`: откат на название, `1`, проброс `GigaChatError`

- [x] 2.1 Keyword-only `ref`, повтор по названию при `-1` или `_NO_INFO` (если был `body`), перед повтором запомнить `refused = result == -1`; итог `_NO_INFO` → `REFUSAL_FALLBACK_SCORE`, если `refused`, иначе `NO_INFO_FALLBACK_SCORE`, с `logger.warning`; `ref` во всех строках лога
- [x] 2.2 Убрать `try/except GigaChatError` — ошибка пробрасывается; `None` теперь только «невалидный ответ»; docstring модуля и функции (Returns/Raises) обновить
- [x] 2.3 Сверить комбинации исходов с таблицей в design.md, п. 2
- [x] 2.4 `scripts/calibrate_importance.py`: обернуть вызов `score_importance` в `except GigaChatError` → `score = None`, как было

## 3. Лимит невалидных циклов (`app/services/scheduler.py`)

- [x] 3.1 Константа `IMPORTANCE_MAX_INVALID_CYCLES = 3` рядом с `IMPORTANCE_BACKLOG_*` (с комментарием: счётчик в памяти, сброс при перезапуске)
- [x] 3.2 `_rate_article`: после предфильтра — при `not final` и счётчике `bot_data["importance_invalid"]` ≥ лимита вернуть `None` без запроса текста и LLM (`logger.debug`)
- [x] 3.3 `_rate_article`: вызов `score_importance(..., ref=article.external_id)` в `except GigaChatError` → `logger.warning` с id, `return None`, счётчик не трогать
- [x] 3.4 `_rate_article`: `score is None` → инкремент счётчика, `logger.info` с id и номером цикла, на достижении лимита — `logger.warning` «N циклов с невалидным ответом» (один раз, без «подряд»); успешная оценка → `pop` из счётчика; docstring обновить
- [x] 3.5 Обновить docstring `_rate_backlog` (исключение актов с исчерпанным лимитом, п. 3.6)
- [x] 3.6 `_rate_backlog`: исключить в SQL `external_id` с исчерпанным лимитом (`Article.external_id.notin_(…)`, условие только при непустом списке)

## 4. Подборка (`app/services/weekly_review.py`, `app/services/scheduler.py`)

- [x] 4.1 `weekly_review.py`: константа `WEEKLY_MIN_ACTS_WITHOUT_WAIT = 3` рядом с `WEEKLY_MIN_IMPORTANCE` и чистая функция `expected_selection(window)` с docstring (`include` или без решения с оценкой ≥ `WEEKLY_MIN_IMPORTANCE`, без требования саммари, в порядке подборки); `final_selection` выразить через неё (+ саммари)
- [x] 4.2 `_send_weekly_digest`: откладывать, только если есть неоценённые, `len(expected_selection(window)) < WEEKLY_MIN_ACTS_WITHOUT_WAIT` и время до воскресенья 21:00; иначе — `logger.warning` со списком id неоценённых и отправка; после попытки 2 повторной проверки нет; docstring обновить

## 5. Проверка и документация

- [x] 5.1 Ручная проверка без квоты, `score_importance`: скрипт в scratchpad с фейковым клиентом (`complete` отдаёт заданные ответы или бросает `GigaChatError`) — `null` по тексту → уровень по названию; `null` дважды → `1` (ровно 2 вызова); отказ по тексту + `null` по названию → `2` (2 вызова); только название + `null` → `1` (1 вызов); сломанный ответ → `None` (2 вызова); двойной отказ → `2`; `GigaChatError` → исключение
- [x] 5.2 Ручная проверка без квоты, `_rate_article`: тот же скрипт с фейковыми `context.bot_data`, `session_maker`/`_update_article` (монкипатч) — 3 невалидных цикла → 4-й без вызова LLM; `final=True` после лимита → вызов есть; 3 × `GigaChatError` → счётчик пуст, вызов есть; успех → счётчик очищен; акт с исчерпанным лимитом не попадает в выборку `_rate_backlog`
- [x] 5.3 Ручная проверка без квоты, подборка: скрипт с фейковым окном — `expected_selection`/`final_selection`; ветка `_send_weekly_digest` с монкипатчем `_load_weekly_window`, `_rate_article`, `_summarize_in_background`, `_weekly_run_*`, `_send_notification`. Случаи: неоценённый + 3 ожидаемых (один без саммари) → отправка; неоценённый + 2 → перенос; то же после воскресенья 21:00 → отправка; без неоценённых + 1 → отправка
- [x] 5.4 `python -c "from app.core.config import Config; print(Config.load())"` и импорт `app.services.scheduler` проходят
- [x] 5.5 CLAUDE.md: в описании оценки важности / бэклога упомянуть ответ без уровня (название → `1`, после отказа → `2`) и лимит 3 невалидных цикла до финального прохода (с исключением из выборки добора); в абзаце недельной подборки «waits while window candidates are unscored» → «…unscored, unless ≥ 3 acts already qualify (`WEEKLY_MIN_ACTS_WITHOUT_WAIT`)»
