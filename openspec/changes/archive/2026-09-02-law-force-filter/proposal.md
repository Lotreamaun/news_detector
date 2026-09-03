## Why

Пользователи получают все законы подряд и тонут в шуме — юристу по крипте не нужны постановления про ЖКХ. Первый вертикальный срез онбординга — фильтр по юридической силе (ФКЗ/ФЗ/Указ/ПП/ведомственные) — самый дешёвый (без LLM, по `documentTypeId`), но уже даёт 80% пользы и валидирует весь пайплайн `UserFilter → ArticleMeta → _notify_users` перед более сложными сферой/регионом.

## What Changes

- Модель `UserFilter` (или `user_filters`): `user_id`, `level` enum (`FKZ`, `FZ`, `DECREE`, `GOV_RESOLUTION`, `DEPARTMENTAL`, `REGIONAL`), `enabled` — множественный выбор, default `Все`.
- `Article` / `ArticleMeta`: поле `level` (мапинг `documentTypeId` → enum, fallback `UNKNOWN`), заполняется в `rss_parser._entry_from_item`.
- Онбординг-визард шаг 2/3 «Сила»: 6 Inline-кнопок с toggle ☑, `[Выбрать всё]`/`[Далее]`, состояние в `ConversationHandler` + `user_filters` в БД.
- Применение: `scheduler._notify_users` фильтрует `if article.level in user.enabled_levels` иначе skip; `/latest` и `/notify_me` тоже уважают фильтр.
- Триггер: `/start` для новых + `/settings` для существующих, кнопка `Пропустить → Все`.

## Capabilities

### New Capabilities
- `law-force-filter`: фильтр законов по юридической силе в онбординге и рассылке (модель, классификация по `documentTypeId`, UI-визард, применение к уведомлениям).

### Modified Capabilities
<!-- нет изменений существующих REQUIREMENTS -->

## Impact

- `app/models/user.py` / `app/models/article.py` (+ новый `user_filter.py` / поля): миграция, индексы.
- `app/services/rss_parser.py`: `is_important` → обобщить в `classify_level(documentTypeId)`.
- `app/bot/handlers.py`: новый `ConversationHandler` для визарда, `/settings`, `/start` ветвление.
- `app/services/scheduler.py`: `matches()` логика в `_notify_users`.
- Тесты: `user_filter`/`article` level, `scheduler` фильтр.
- Без LLM, без новых внешних зависимостей.
