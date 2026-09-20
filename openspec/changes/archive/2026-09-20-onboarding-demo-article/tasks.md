## 1. Схема БД

- [x] 1.1 Добавить `Article.is_demo: bool` (`default=False`,
      `server_default=text("0")`) в `app/models/article.py`, по образцу
      `notified`/`level`.
- [x] 1.2 Добавить `_ensure_is_demo_column(connection)` в
      `app/core/database.py` (по образцу
      `_ensure_channel_verified_column`/`_ensure_article_notified_column`:
      `PRAGMA table_info(articles)` + идемпотентный
      `ALTER TABLE articles ADD COLUMN is_demo BOOLEAN NOT NULL DEFAULT 0`)
      и вызвать её из `init_db_schema()`.

## 2. Подготовка контента демо-статьи

- [x] 2.1 Выбрать один реальный ФЗ 2026 года, вышедший в свет более 30 дней
      назад от текущей даты (чтобы не попадать в окно `/latest` по времени
      даже без фильтра `is_demo`), с готовым текстом на pravo.gov.ru.
- [x] 2.2 Получить текст закона существующим пайплайном
      (`get_legal_text()`; `ocr_document_text()`, если готовой редакции нет)
      и прогнать через `Summarizer.summarize()`, используя
      `SUMMARY_MIN_LEN`/`SUMMARY_MAX_LEN` из `.env`.
- [x] 2.3 Зафиксировать результат как константы в новом модуле
      `app/services/demo_article.py`: `DEMO_ARTICLE_EXTERNAL_ID =
      "demo-onboarding-example"`, `DEMO_ARTICLE_TITLE`, `DEMO_ARTICLE_URL`,
      `DEMO_ARTICLE_TEXT`, `DEMO_ARTICLE_SUMMARY`,
      `DEMO_ARTICLE_PUBLISHED_AT`, `DEMO_ARTICLE_LEVEL = "FZ"`.

## 3. Идемпотентная вставка при старте

- [x] 3.1 Добавить `_ensure_demo_article(application)` в `app/main.py`:
      проверяет наличие строки с `external_id ==
      DEMO_ARTICLE_EXTERNAL_ID`; если нет — вставляет `Article` из констант
      `demo_article.py` с `is_demo=True`, `notified=True` (не проходит через
      `scheduler`/`_notify_users`).
- [x] 3.2 Вызвать `_ensure_demo_article(application)` из `_post_init`, после
      `_run_initial_check` (аналогично `_run_initial_backup`).
- [x] 3.3 Обернуть в `try/except` с логированием (по образцу остальных шагов
      `_post_init`) — сбой не должен ронять старт бота.

## 4. Fallback в демо-уведомлении

- [x] 4.1 В `_send_example_notification` (`app/bot/handlers.py`) добавить
      пятую fallback-ступень: если все текущие запросы вернули `None`,
      выбрать `select(Article).where(Article.is_demo == True).limit(1)`.
- [x] 4.2 Убедиться, что демо-статья рендерится тем же `_build_notification`,
      что и обычные (без спецкейсов в форматировании).

## 5. Исключение демо-статьи из /latest

- [x] 5.1 Добавить `.where(Article.is_demo.is_(False))` в `stmt` внутри
      обработчика `latest()` (`app/bot/handlers.py:519-533`).

## 6. Проверка

- [x] 6.1 На чистой БД (без пред-существующих `FKZ`/`FZ`) прогнать
      `python -m app.main`, убедиться, что демо-статья появилась в БД
      ровно один раз со второго запуска подряд (идемпотентность) и что
      никаких уведомлений существующим пользователям не отправилось.
- [x] 6.2 Через `/start` → «Как это работает?» на пустой по `FZ`/`FKZ` БД
      убедиться, что показывается демо-статья, а не сообщение «Пока нет
      законов для примера».
- [x] 6.3 С реальной свежей `FZ`-статьёй в БД убедиться, что демо-статья не
      показывается (реальная в приоритете).
- [x] 6.4 Прогнать `/latest` и убедиться, что демо-статья не входит в
      выдачу, даже если её `published_at` формально попадает в окно 30 дней.
