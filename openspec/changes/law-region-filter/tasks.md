## 1. Модель данных

- [ ] 1.1 Расширить `user_filters` для `dimension=region` (или добавить колонку `region`), миграция
- [ ] 1.2 Добавить `articles.region TEXT` (default `federal`, индекс) + миграция
- [ ] 1.3 `py_compile` моделей, проверка создания таблиц

## 2. Справочник и резолвер региона

- [ ] 2.1 Создать `app/services/region_dictionary.py` — 89 субъектов + алиасы (`Москва`/`МСК`, `Чита`→`Забайкальский край`)
- [ ] 2.2 Реализовать `app/services/region_resolver.py:resolve_region(text)` — нормализация (`lower`, `ё→е`, удаление `г./обл./край`, лемматизация `pymorphy2`), `rapidfuzz`/`difflib` с порогом 80, возврат `(code, name, score)` или `None`
- [ ] 2.3 Unit-тест резолвера (ровный ввод, опечатка, город→регион, неизвестный)

## 3. Классификатор статьи

- [ ] 3.1 Реализовать `app/services/region_classifier.py:classify_region(title, summary)` — поиск региона в `title+summary` (fallback 1000 знаков `original_text`) по справочнику, иначе `federal`
- [ ] 3.2 Интегрировать в `rss_parser._entry_from_item`/`_save_article` — сохранять `Article.region`
- [ ] 3.3 Тест классификатора (региональный заголовок → код, федеральный → `federal`)

## 4. Онбординг-визард шага «Регион»

- [ ] 4.1 `ConversationHandler` шаг «Регион» (3/3): текстовый ввод → `resolve_region` → сообщение «Это Забайкальский край?» с `[Да]`/`[Нет, ввести ещё раз]` + `[Пропустить → Все]`, до 3 попыток
- [ ] 4.2 Хендлеры `region:yes` / `region:no` / `region:skip` → запись в `user_filters` (`dimension=region`), завершение визарда
- [ ] 4.3 Связать порядок визарда `Сфера (1/3)` → `Сила (2/3)` → `Регион (3/3)`, `/start` для новых, `/settings` → `Регион` с текущим значением

## 5. Применение фильтра

- [ ] 5.1 `app/services/scheduler.py:_notify_users` — фильтр по `region` (OR внутри, AND с `level`+`sphere`)
- [ ] 5.2 `app/bot/handlers.py:latest` — фильтровать по `region` (Все → без фильтра)
- [ ] 5.3 `notify_me`/`summary` — уважают фильтр региона

## 6. Проверка

- [ ] 6.1 `py_compile`, `node --check` если есть JS, `openspec validate --changes`, добавление `rapidfuzz`/`pymorphy2` в `requirements.txt` если нужно
- [ ] 6.2 Ручная проверка: ввод «Забайкалский край» (опечатка) → предположение `Забайкальский край` → Да → приходит только `75`/`federal`, `/settings` меняет, `/latest` респект
