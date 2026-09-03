## 1. Модель данных

- [x] 1.1 Создать `app/models/user_filter.py` (`user_id FK → users.id`, `level TEXT`, PK `(user_id, level)`) + связь в `User`
- [x] 1.2 Добавить `articles.level TEXT` (индекс, default `UNKNOWN`) + миграция `ALTER TABLE` + `LEVEL_MAP` константа
- [x] 1.3 `py_compile` моделей, проверка БД/создания таблиц

## 2. Классификация уровня

- [x] 2.1 Обобщить `is_important` → `classify_level(documentTypeId) -> level` в `app/services/rss_parser.py` (маппинг ФКЗ/ФЗ/Указ/ПП/ведомственные/региональные)
- [x] 2.2 Вызывать в `_entry_from_item` при создании `FeedEntry`/`Article` и сохранять `Article.level`
- [x] 2.3 Unit-тест маппинга (известные GUID → уровни, неизвестный → UNKNOWN)

## 3. Онбординг-визард шага «Сила»

- [x] 3.1 `ConversationHandler` для визарда: экран «Сила» с 6 toggle-кнопками `[ФКЗ ☑]` + `[Выбрать всё]` + `[Далее]`, state в `context.user_data`, редактирование сообщения
- [x] 3.2 Хендлеры `callback_data=level:*` (toggle), `levels:all`, `levels:next` → запись в `user_filters` (DELETE+INSERT), завершение
- [x] 3.3 Ветвление `/start` (новый → визард, существующий → приветствие) и `/settings` (показать текущие ☑/☐ + вход в визард)

## 4. Применение фильтра

- [x] 4.1 `app/services/scheduler.py:_notify_users` — загрузить `levels` для каждого `user`, пропустить если `article.level not in levels` (пустой фильтр = Все, `UNKNOWN` только для Все)
- [x] 4.2 `app/bot/handlers.py:latest` — фильтровать `SELECT ... WHERE level IN (...)` (Все → без фильтра, до 10)
- [x] 4.3 `notify_me`/`summary` — уважают фильтр или явно показывают вне фильтра (как сейчас, без hidden)

## 5. Проверка

- [x] 5.1 `py_compile` всех изменённых модулей, `openspec validate --changes`
- [x] 5.2 Ручная проверка: новый юзер → визард → выбрать `[ФКЗ]` → приходит только ФКЗ, `/settings` меняет, `/latest` респект
