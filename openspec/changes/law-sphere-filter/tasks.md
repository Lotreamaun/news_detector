## 1. Модель данных

- [ ] 1.1 Расширить `app/models/user_filter.py` (или `user.py`): добавить `dimension` (`level`/`sphere`) или отдельную колонку `sphere`, миграция, связь в `User`
- [ ] 1.2 Добавить `articles.spheres TEXT` (CSV, default `other`, индекс) + миграция
- [ ] 1.3 `py_compile` моделей, проверка создания таблиц

## 2. Классификатор сферы (без LLM)

- [ ] 2.1 Создать `app/services/sphere_keywords.py` — заготовки словарей (15-30 лемм на сферу, 12 сфер, уточняются на реализации)
- [ ] 2.2 Реализовать `app/services/sphere_classifier.py:classify_sphere(title, summary)` — лемматизация `pymorphy2`, `title+summary` (fallback 1000 знаков `original_text`), подсчёт уникальных лемм, порог `>=2` → сфера, multi-label до 3, иначе `other`
- [ ] 2.3 Интегрировать в `app/services/rss_parser.py` (`_entry_from_item`/`_save_article`) — сохранять `Article.spheres`
- [ ] 2.4 Unit-тест классификатора (финансовая статья → `finance`, природа → `nature`, смешанная → обе, неизвестная → `other`)

## 3. Онбординг-визард шага «Сфера»

- [ ] 3.1 `ConversationHandler` шаг «Сфера»: 12 toggle-кнопок `[Природа ☑]` + `[Выбрать всё]` + `[Далее]` (3×4 сетка), state в `context.user_data`
- [ ] 3.2 Хендлеры `sphere:*` / `spheres:all` / `spheres:next` → запись в `user_filters` (DELETE+INSERT по `dimension=sphere`)
- [ ] 3.3 Порядок визарда: `Сфера` (1/3) → `Сила` (2/3, из law-force-filter) — связать, `/start` для новых, `/settings` → `Сфера` с текущими ☑

## 4. Применение фильтра

- [ ] 4.1 `app/services/scheduler.py:_notify_users` — фильтр по сферам `OR` + AND с фильтром по силе (`level` из law-force-filter)
- [ ] 4.2 `app/bot/handlers.py:latest` — фильтровать по сферам (Все → без фильтра, иначе `WHERE spheres IN`)
- [ ] 4.3 `notify_me`/`summary` — уважают фильтр сфер или показывают вне фильтра (как сейчас)

## 5. Проверка

- [ ] 5.1 `py_compile` всех изменённых модулей, `openspec validate --changes`, `pymorphy2` в `requirements.txt`
- [ ] 5.2 Ручная проверка: новый юзер → выбрать `[Финансы]` → приходит только `finance`, `/settings` меняет, `/latest` респект, `other` только для «Все»
