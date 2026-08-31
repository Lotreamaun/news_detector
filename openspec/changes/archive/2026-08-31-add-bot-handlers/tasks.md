## 1. Хендлер /help

- [x] 1.1 Добавить константу `HELP_TEXT` в `handlers.py` — строка со списком команд и описаниями
- [x] 1.2 Добавить функцию `help_command(update, context)` в `handlers.py` — отправляет `HELP_TEXT` через `reply_text`

## 2. Хендлер /latest

- [x] 2.1 Импортировать `Article` из `app.models` в `handlers.py`
- [x] 2.2 Добавить функцию `latest(update, context)` в `handlers.py` — выборка 10 последних статей (`Article.id.desc()`), форматирование MarkdownV2, обработка пустого результата и статей без саммари

## 3. Регистрация хендлеров

- [x] 3.1 Импортировать `latest` и `help_command` в `app/main.py`
- [x] 3.2 Добавить `CommandHandler("latest", latest)` и `CommandHandler("help", help_command)` в `_build_application`
