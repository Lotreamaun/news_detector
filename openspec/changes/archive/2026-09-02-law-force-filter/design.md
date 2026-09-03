## Context

См. proposal.md — Why: нужен первый срез персонализации. Сейчас `User` хранит только `telegram_id/is_active`, `Article` — `external_id/title/summary/url/original_text` без `level`. `scheduler._notify_users` рассылает всем `is_active` одинаково, `handlers.start` не спрашивает фильтры. `rss_parser` уже мапит `FKZ` через `is_important`/`documentTypeId`.

## Goals / Non-Goals

**Goals:**
- Отдельная сущность `UserFilter` (или `user_levels`) для 6 уровней, default «Все», персистентна.
- `Article.level` из `documentTypeId` на этапе `_entry_from_item`, до сохранения.
- Визард 1 шаг (Сила) на `/start`/`/settings` с Inline toggle, без LLM, без блокировки остального бота.
- Фильтр применяется в `_notify_users` и `latest` (и `notify_me`).

**Non-Goals:**
- Сфера и регион — следующие срезы, не здесь.
- Не меняем `original_text`/`summary` пайплайн, только тег `level`.
- Не делаем WebApp для визарда — только Inline.

## Decisions

### 1. Модель — отдельная таблица `user_filters` vs JSON-колонка
Выбираем отдельную таблицу `user_filters` (`user_id FK`, `level TEXT`, PK `(user_id, level)`) — нормализовано, индексируется, легко `WHERE level IN (...)`. Альтернатива `User.filter_json TEXT` — проще, но запросы `LIKE` и миграции тяжелее. Для 6 значений оверхед минимален.

### 2. Article.level — колонка vs отдельная meta
Добавляем колонку `articles.level TEXT` с индексом, default `UNKNOWN`, заполняем в `rss_parser._entry_from_item` через `LEVEL_MAP: documentTypeId → level`. Альтернатива отдельная `article_meta` — лишний JOIN. Миграция — `ALTER TABLE ADD COLUMN` + бэкфилл для старых записей `UNKNOWN`.

### 3. Визард — PTB ConversationHandler vs ручной FSM в БД
Используем `ConversationHandler` с `CallbackQueryHandler` для toggle, но состояние (выбранные уровни) храним в `context.user_data` + при `Далее` — в `user_filters` (транзакция). При рестарте бота `user_data` теряется, но `user_filters` остаётся — визард просто читает из БД. Альтернатива — полностью в БД `onboarding_state` — тяжелее для MVP.

### 4. Toggle-логика
Сообщение шага «Сила» — одно сообщение с 6 кнопками `level:FKZ` + `[Выбрать всё]` + `[Далее]`. Каждый `callback_data=level:FKZ` тогглит в `context.user_data['levels']` и редактирует сообщение (меняет `☑`/`☐`). `Далее` — `DELETE FROM user_filters WHERE user_id=?; INSERT ...` + `Этот шаг завершён`.

### 5. Применение фильтра
`scheduler._notify_users`: `await session.scalars(select(User).where(User.is_active))` → для каждого `user` грузим `levels = select(level from user_filters where user_id=user.id)`; если пусто → `Все` (пропускаем фильтр), иначе `if article.level not in levels and article.level != UNKNOWN → skip`. `latest`: `SELECT * FROM articles WHERE level IN (...) ORDER BY id DESC LIMIT 10` (если фильтр `Все` — без WHERE).

## Risks / Trade-offs

- [Стартые статьи без level (UNKNOWN) не дойдут до фильтр-юзеров] → для `UNKNOWN` считаем `matches` только если фильтр «Все» или пользователь явно включил `UNKNOWN` (скрытая опция, не показываем в UI).
- [Миграция на проде с большими articles] → `ADD COLUMN` с `SERVER_DEFAULT='UNKNOWN'` — мгновенно, бэкфилл не нужен, старые остаются `UNKNOWN`.
- [Спавн ConversationHandler конфликтует с другими] → отдельный `ConversationHandler` с `entry_points=[CommandHandler('settings')]` и `fallbacks`, `per_user=True`.

## Migration Plan

1. Миграция `user_filters` + `ALTER TABLE articles ADD COLUMN level`.
2. Деплой кода (обратная совместимость: пустой `user_filters` = «Все»).
3. Роллбэк: `DROP TABLE user_filters` + игнор `level` в запросах — бот снова шлёт всем.

## Open Questions

- Нужен ли в UI отдельный уровень `UNKNOWN` — пока скрываем, считаем только для «Все».
