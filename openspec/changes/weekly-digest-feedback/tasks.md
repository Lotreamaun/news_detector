## 1. Модель (`app/models/`)

- [ ] 1.1 `app/models/weekly_feedback.py`: `WeeklyFeedback` — PK (`run_id` → `weekly_digest_runs.id`, `telegram_id`), `verdict` (`up`/`down`), `reason` (SMALLINT, NULL), `answered_at` (design.md, решение 1)
- [ ] 1.2 Экспорт в `app/models/__init__.py` (докстринг и `__all__`); на копии прод-бэкапа `init_db_schema()` создаёт таблицу и не трогает существующие

## 2. Сервис оценок (`app/services/weekly_feedback.py`)

- [ ] 2.1 Константы: префикс `wf`, `UP`/`DOWN`, `REASONS` (4 подписи в порядке design.md, решение 3), подписи и всплывашки
- [ ] 2.2 `feedback_rows(run_id, verdict, reason) -> list[list[InlineKeyboardButton]]`: строка 👍/👎 с ✓ у выбранного; при `down` — две строки причин по 2 с ✓ у выбранной
- [ ] 2.3 `merge_markup(markup, rows)`: берёт строки исходной клавиатуры без кнопок `wf:` (кнопка «Дайджест» с `ids` как есть) и добавляет `rows` (решение 4)
- [ ] 2.4 `parse_callback(data) -> (run_id, action) | None` для `wf:<id>:up|down|r<0-3>`; всё прочее → `None`
- [ ] 2.5 `record_vote(session_maker, run_id, telegram_id, action) -> (verdict, reason) | None`: строки `weekly_digest_runs` нет → `None`; иначе upsert по правилам решения 5 (`up` сбрасывает причину, `down` сохраняет прежнюю, `r<n>` → `down` + причина)
- [ ] 2.6 `feedback_summary(session, config, limit=8)`: последние периоды с `finished_at`; получили — разные `telegram_id` в `notify.sent` с `ref = weekly` в [`started_at`, `finished_at`]; 👍, 👎, причины и «без причины» из `weekly_feedback`; администраторы исключены везде; периоды с «получили» = 0 пропускаются; доли от получивших (решение 7)

## 3. Подборка (`app/services/scheduler.py`)

- [ ] 3.1 `_weekly_run_start` возвращает `id` строки периода (новой или существующей)
- [ ] 3.2 `_build_weekly_message(articles, webapp_url, run_id)`: к клавиатуре `_build_digest` добавляется `feedback_rows(run_id, None, None)`
- [ ] 3.3 `_send_weekly_digest`: сообщение строится после `_weekly_run_start` с его `id`; пустая неделя и остальной порядок проверок не меняются
- [ ] 3.4 `build_weekly_preview`: `run_id = 0`

## 4. Бот (`app/bot/handlers.py`, `app/main.py`)

- [ ] 4.1 Обработчик `weekly_feedback_vote` на `^wf:\d+:(up|down|r[0-3])$`: `run_id = 0` → `query.answer("Предпросмотр — оценка не сохраняется")`; `record_vote` вернул `None` → `query.answer()`; иначе всплывашка по действию и `edit_message_reply_markup(merge_markup(query.message.reply_markup, feedback_rows(...)))`, «Message is not modified» игнорируется
- [ ] 4.2 Регистрация в `app/main.py` через `require_channel_verified` (как остальные кнопки)

## 5. Панель (`app/services/stats.py`, `app/webapp/static/admin.html`)

- [ ] 5.1 `trend()` добавляет поле `feedback` из `feedback_summary`
- [ ] 5.2 `trendView`: после рядов — панель «Оценка подборки» через `block(...)` с ключом пояснения: строка на период (дата пятницы по Москве, «получили N», «👍 N · X%», «👎 N · X%»), под ней мелко причины с ненулевыми числами и «без причины»; пусто → «Оценок пока нет»
- [ ] 5.3 `HINTS['trend.feedback']` (что это / как считается: получившие — разные люди с доставленной подборкой этого периода; доли от получивших; один голос на человека, последний; администраторы исключены; последние 8 подборок) и ключ в группе «Тренда» в `HINT_GROUPS` после рядов

## 6. Проверка и документация

- [ ] 6.1 `python -c "from app.core.config import Config; print(Config.load())"`, импорт `app.main` и `app.services.weekly_feedback` проходят
- [ ] 6.2 `/test_digest weekly` локально: кнопки оценки под «Дайджестом», нажатие даёт «Предпросмотр — оценка не сохраняется», сообщение не меняется, в `weekly_feedback` пусто
- [ ] 6.3 Локальная отправка подборки (свой бот-токен, копия бэкапа, время подборки на ближайшие минуты; себя временно убрать из `ADMIN_CHAT_IDS`): 👍 → ✓ и «Спасибо!»; 👎 → ✓ и четыре причины; причина → ✓; снова 👍 → причины скрыты, в БД `up` без причины; «Дайджест» после правок открывается с тем же списком
- [ ] 6.4 Без `WEBAPP_URL`: под подборкой только строка оценки, правки клавиатуры работают
- [ ] 6.5 «Тренд»: панель показывает период с получившими, голосами и причинами; голос администратора не учитывается; пояснение открывается с заголовка и есть в «Все пояснения»
- [ ] 6.6 CLAUDE.md: в разделе «Weekly important digest» абзац про оценку (кнопки `wf:`, 4 причины, один голос на период, `weekly_feedback`, панель в «Тренде»); строки `app/services/weekly_feedback.py` и `app/models/weekly_feedback.py` в таблице модулей; `weekly-digest-feedback` в списке спек после архивации
