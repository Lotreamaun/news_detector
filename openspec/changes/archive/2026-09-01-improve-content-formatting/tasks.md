## 1. Нормализация текста (включая заголовки)

- [x] 1.1 Добавить хелпер `_normalize_text(text) -> str` в `app/services/rss_parser.py`: удаление HTML-тегов и сущностей (`<br />` и т.п.), схлопывание множественных пробелов/`\t`/неразрывных пробелов, удаление избыточных пустых строк
- [x] 1.2 Применять `_normalize_text` к результату `_html_to_text`, к распознанному OCR-тексту и к саммари (в месте сборки сообщения)
- [x] 1.3 Нормализовать заголовок документа в `_entry_from_item` (rss_parser.py) через `_normalize_text` уже при сохранении `Article.title` — фикс `<br />` в title («…№ 111<br /> „Об утверждении…“»)
- [x] 1.4 Нормализовать `title` в `_format_summary` перед `escape_markdown` (заголовок сообщения не должен содержать `<br />`)
- [x] 1.5 py_compile изменённых модулей (rss_parser, handlers)

## 2. MarkdownV2 — во всех сообщениях по умолчанию

- [x] 2.1 Добавить хелпер `_format_summary(summary, title) -> str` в `app/bot/handlers.py`: жирный заголовок, корректное экранирование `escape_markdown(version=2)` для `parse_mode="MarkdownV2"`
- [x] 2.2 Применить `_format_summary` в `/latest`, `/summary`/кнопке (`_summarize_and_reply` → `_deliver`)
- [x] 2.3 Применить MarkdownV2-форматирование в уведомлениях: переработать `_build_notification`/`_notify_users` (scheduler.py) и `/notify_me` (handlers.py) — собирать текст тем же стилем (жирный заголовок, ссылка), отправлять с `parse_mode="MarkdownV2"`
- [x] 2.4 Убедиться, что красивое форматирование показывается во всех сообщениях по дефолту, а не только после принудительной саммаризации

## 3. Убрать дисклеймер из сообщений (перенесён в WebApp)

- [x] 3.1 Убрать `_AI_DISCLAIMER` из `_format_summary` (удалить `with_disclaimer`, дисклеймер не показывается в чате) и `_AI_DISCLAIMER_TEXT` из `_build_notification`; саммари и так очевидно от ИИ
- [x] 3.2 Дисклеймер «распознано с помощью ИИ» показывается только внутри WebApp (`law-text-webapp`) вверху перед текстом закона — в сообщениях бота не нужен

## 4. Починка notify_me (leftover WebApp)

- [x] 4.1 Починить `notify_me` (handlers.py): убрать `context.bot_data["config"].WEBAPP_URL` (поля нет в Config), вызывать `_build_notification(article)` и отправлять с `parse_mode="MarkdownV2"`

## 5. Проверка

- [x] 5.1 py_compile всех изменённых модулей (config, handlers, scheduler, rss_parser)
- [x] 5.2 Логическая проверка сценариев specs/content-formatting (нормализация, заголовок `<br />`, разметка, отсутствие дисклеймера в чате)
