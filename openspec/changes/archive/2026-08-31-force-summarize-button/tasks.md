## 1. Модель и лимит

- [x] 1.1 Создать модель `SummarizationUsage` (id, user_id FK, month `YYYY-MM`, count, уникальность (user_id, month)) в `app/models/usage.py`
- [x] 1.2 Экспортировать модель и добавить в `app/models/__init__.py`
- [x] 1.3 Добавить константу `FORCE_SUMMARIZE_MONTHLY_LIMIT` (default 10) в `Config` (`app/core/config.py`)

## 2. Кнопка на уведомлении

- [x] 2.1 В `_build_notification` (`app/services/scheduler.py`) добавить `InlineKeyboardMarkup` с кнопкой «Сделать саммари» (`callback_data="force_sum:summary:<external_id>"`) и возвращать разметку с сообщением

## 3. Callback-обработчик

- [x] 3.1 Добавить функцию `force_summarize` в `app/bot/handlers.py`: парсинг `callback_data`, проверка лимита пользователя за текущий месяц, отказ при исчерпании с понятным сообщением
- [x] 3.2 Реализовать в `force_summarize` принудительную саммаризацию: `get_legal_text` → при отсутствии текста `ocr_document_text` → `summarize`, обновление `Article.summary`/`original_text` при наличии записи, отправка саммари нажавшему пользователю
- [x] 3.3 Оставить `# TODO: Рассмотреть платный тариф — бесконечная принудительная саммаризация` в месте проверки лимита
- [x] 3.4 Инкрементировать счётчик пользователя за месяц при успешном вызове

## 4. Регистрация и проверка

- [x] 4.1 Зарегистрировать `CallbackQueryHandler` для `force_summarize` в `_build_application` (`app/main.py`)
- [x] 4.2 py_compile изменённых модулей (scheduler, handlers, main, models/usage, models/__init__, config)
- [x] 4.3 Логическая проверка сценариев из specs/force-summarization (кнопка / нет текста OCR / лимит исчерпан / сброс по месяцу)
