## 1. Бэкенд: общие определения рядов и сравнение (`app/services/stats.py`)

- [ ] 1.1 До правок сохранить ответ `stats.trend()` на копии локальной БД (JSON в scratchpad) — эталон для сверки в 1.3
- [ ] 1.2 Вынести `_trend_series(config)`: 10 описаний `{key, title, column, where, value, journal}` в нынешнем порядке, условия — как в `trend()` (`_not_admin`, ref `ok` у саммари, `system=True` у сбоев проверок, `finished_at IS NOT NULL` у подборки)
- [ ] 1.3 Переписать `trend()` на `_trend_series` (прежние `weekly()`, `from_journal` для `journal=True`, `min(v, 1)` для `digest`); сверить ответ с эталоном из 1.1 — `series` и `weeks` совпадают
- [ ] 1.4 `compare_periods(unit, now)` — чистая функция по design.md, п. 2; проверить из консоли: среда 15:00 МСК (неделя), 31 марта 12:00 (весь февраль), 1-е число 00:05, понедельник 00:05
- [ ] 1.5 `compare(session_maker, config, unit, now=None)`: по каждому ряду значение за `[start, end)` двух отрезков; `None` у журнальных рядов, если журнала нет или `tracking_since > start`; `digest` — число без `min`; `feedback` из `feedback_summary`; форма ответа — design.md, п. 4; docstring

## 2. API (`app/webapp/admin.py`)

- [ ] 2.1 `handle_stats`: словарь построителей `pulse` / `trend` / `compare_week` / `compare_month` (`functools.partial` для `unit`), неизвестный `view` → 400; обновить docstring модуля и функции (`?view=pulse|trend|compare_week|compare_month`)

## 3. Фронт: режимы «Тренда» (`app/webapp/static/admin.html`)

- [ ] 3.1 `trendMode` + `localStorage` `adminTrendMode` (неизвестное → `weeks`), `statsView()`, слоты `sv.compare_week` / `sv.compare_month`; `statsStatus`, `updateStatsClock`, конец `loadStats`, `setStatsKind` и восстановление при старте работают через `statsView()`
- [ ] 3.2 Переключатель `.seg.seg-wide` («Недели | Неделя к неделе | Месяц к месяцу», `role="tablist"`/`tab`, `data-key="mode:…"`) первым элементом вида; `setTrendMode(mode)`: сохранить, перерисовать из кэша, `loadStats(statsView(), true)`; возврат фокуса на кнопку режима после перерисовки
- [ ] 3.3 Стили `.seg-wide`: на всю ширину, `flex: 1`, перенос текста, `min-height: 40px`; проверить на ширине 320–360 px

## 4. Фронт: таблица сравнения (`admin.html`)

- [ ] 4.1 `compareView(d)`: переключатель, `block` с заголовком режима, ключом пояснения `trend.compare_<unit>` и подписью отрезков («Сейчас: … · Было: …», «весь прошлый месяц, дд.мм–дд.мм» при `previous.end == current.start`), строки рядов, `feedbackBlock`, `allHintsLink('trend')`
- [ ] 4.2 Строка ряда: название через `hintLink`, «prev → cur», изменение по design.md, п. 6 (=, ▲/▼ с U+2212, процент только при `prev > 0`, «нет данных» без изменения, `digest` в неделях — «да»/«нет»), `aria-label` изменения словами
- [ ] 4.3 `TREND_BETTER` и классы `.cmp-good` / `.cmp-bad` / нейтральный, светлая и тёмная тема; на узком экране значения переносятся под название

## 5. Фронт: нажимаемые столбики (`admin.html`)

- [ ] 5.1 `spark()`: столбик 14 / промежуток 6 / высота 40 (`viewBox` и CSS 234×40), прозрачные зоны нажатия на всю высоту с `data-i` и `<title>`, один обработчик `click` на `svg`
- [ ] 5.2 `trendPick` по ключу ряда (по умолчанию — текущая неделя, сброс после загрузки `trend`); нажатие обновляет на месте классы столбиков (`.sel`, приглушение остальных, сплошная обводка у «нет данных») и строку «эта неделя: N» / «неделя с дд.мм: N» / «нет данных»; повторное нажатие — к текущей неделе
- [ ] 5.3 Клавиатура и диктор: `tabindex="0"`, `role="slider"`, `aria-valuemin/max/now/valuetext`, ←/→, Home/End, Esc; видимый `:focus-visible`

## 6. Пояснения (`admin.html`)

- [ ] 6.1 Переписать тексты `trend.*` в `HINTS` без «за неделю» с уточнениями из design.md, п. 8 (active, with_summary, digest, журнальные ряды); сверить каждый с `_trend_series` / `compare()`
- [ ] 6.2 Ключи `trend.compare_week` и `trend.compare_month`; фраза о режимах в `HINT_TREND_NOTE`; `openAllHints('trend')` в режиме сравнения добавляет первой группу «Сравнение» с ключом текущего режима

## 7. Проверка и документация

- [ ] 7.1 `python -c "from app.core.config import Config; print(Config.load())"` и импорт `app.services.stats`, `app.webapp.admin` проходят
- [ ] 7.2 Локальный WebApp, вход по ссылке `/admin`, ширина телефона и десктоп, светлая и тёмная тема: три режима переключаются, режим запоминается после перезагрузки; в сравнении цифры совпадают с ручным SQL по одному-двум рядам; «нет данных» при журнале, начавшемся внутри прошлого отрезка (временно подставить `now` или сузить данные на копии БД); `?view=foo` → 400
- [ ] 7.3 Столбики: тап/клик по прошлой неделе, по пунктирной, повторный тап, другие ряды не меняются, прокрутка не прыгает; с клавиатуры ←/→/Home/End/Esc, VoiceOver объявляет значение
- [ ] 7.4 Пояснения: заголовок таблицы сравнения открывает своё пояснение; «Все пояснения» в режиме сравнения начинаются с «Сравнения»; при открытой шторке режимы не переключаются
- [ ] 7.5 Проверка в Telegram Mini-App на телефоне (после деплоя): тап по столбику, переключатель на узком экране, тёмная тема
- [ ] 7.6 CLAUDE.md, абзац «Admin panel and stats»: режимы «Тренда» (`view=compare_week|compare_month`, тот же отрезок прошлого периода, «нет данных» для неполного журнала) и тап по столбику
