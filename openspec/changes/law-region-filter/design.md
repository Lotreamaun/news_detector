## Context

См. proposal.md — Why. После `law-force-filter` (`level`) и `law-sphere-filter` (`spheres` без LLM) остался третий срез — регион. Сейчас `User` без региона, `Article` без `region`, `scheduler._notify_users` фильтрует только по `level`/`spheres`. Пользователь вводит «Забайкальский край»/«Чита» с опечатками — нужно нормализовать и подтвердить.

## Goals / Non-Goals

**Goals:**
- Фильтр `region` (89 субъектов + алиасы городов, default «Все») с нормализацией, нечётким поиском и подтверждением `[Да]/[Нет]`.
- `Article.region` из `title/summary` по тому же справочнику.
- Визард шаг 3/3 «Регион» (текстовый ввод + подтверждение) и применение в `_notify_users`/`latest` (AND с силой/сферой).

**Non-Goals:**
- Не трогаем силу/сферу — дополняем.
- Не делаем пагинацию 89 кнопок — только свободный ввод + подтверждение (пагинация — позже если нужно).
- Не вводим LLM для региона.

## Decisions

### 1. Модель — расширить `user_filters` до `dimension=region`
На момент написания `UserFilter` в коде — это `(user_id, level)`, никакой обобщённой `dimension`/`value` схемы ещё нет. Схему `user_filters(user_id, dimension, value)` вводит `law-sphere-filter` (design.md, решение №1) — этот change на неё опирается и **зависит** от того, что `law-sphere-filter` будет применён первым. Если `law-region-filter` реализуется раньше или независимо, нужно либо самому ввести эту миграцию, либо синхронизироваться с `law-sphere-filter`. `dimension='region'`, `value` — ISO-код `75` или `all`. Альтернатива отдельная `user_regions` — лишняя таблица. Миграция — добавить строки, `all` = отсутствие строк как и для других измерений.

### 2. Справочник регионов — словарь + алиасы
Файл `app/services/region_dictionary.py`: 89 субъектов `{code, name, aliases[]}` где `aliases` включает `Москва`/`МСК`/`Московская область`, `Забайкальский край`/`Чита`/`Чита (Забайкалье)`. Источник — `okato`/`iso3166` + ручные алиасы городов. Альтернатива — внешний API `dadata` — платно, отклонено.

### 3. Нормализация + нечёткий поиск — где и как
`app/services/region_resolver.py: resolve_region(input: str) -> (code, name, score)`:
- Нормализация: `lower`, `ё→е`, удалить `г.`, `обл.`, `край`, `республика`, trim, лемматизация `pymorphy2` (опционально, для «Забайкальской» → «забайкальский»).
- Поиск: `rapidfuzz` (или `difflib` из stdlib) по `name` + `aliases` с `score >=80` → лучший. Порог 80 — баланс (опечатка «Забайкалский» → 91, «Масква» → 85). Альтернатива чистое `difflib` — медленнее, но без зависимости; выбираем `rapidfuzz` если уже есть, иначе `difflib`.
- Возврат: `None` если `<60`, иначе `(code, name)`.

### 4. Подтверждение Inline
Визард состояние `context.user_data['region_input']` + `attempt` (до 3). После `resolve_region` → сообщение «Это Забайкальский край?» с `[Да]`/`[Нет, ввести ещё раз]`. `Да` → `DELETE+INSERT user_filters`, `Нет` → `await message.reply_text("Введи ещё раз")` и `return` в тот же шаг. `Пропустить → Все` — сразу `all`.

### 5. Классификатор `Article.region`
`app/services/region_classifier.py:classify_region(title, summary)` — тот же `region_dictionary` + леммы, поиск в `title+summary` (fallback 1000 знаков `original_text`), первое вхождение региона с вхождением `Губернатора X края` → `X`, иначе `federal`. Если несколько — берём первый.

### 6. Применение — AND между измерениями
`scheduler._notify_users`: `if article.region != 'all'/'federal' and article.region not in user_regions and 'all' not in user_regions → skip`. Комбинируется `AND` с `level` и `spheres` (все три должны совпасть или быть `Все`).

## Risks / Trade-offs

- [Пользователь ввёл «Москва» — это город или область?] → резолвер вернёт `Москва (город)` с `code=77`, а `Московская область` — `50`, показываем полный `name` в подтверждении, пользователь выберет.
- [Опечатка сильная, score 60-79] → не подтверждаем автоматом, просим ввести ещё раз, после 3 попыток — `Все`.
- [Регион в title не в начале, а в конце] → поиск по всему `title+summary`, не только началу.
- [89 субъектов — алиасы неполные] → fallback `federal`, расширять по логам.

## Migration Plan

1. Миграция `user_filters` (dimension=region) + `ALTER TABLE articles ADD COLUMN region TEXT DEFAULT 'federal'`.
2. Деплой резолвера/классификатора (обратная совместимость: пустой `region` = `federal` = «Все»).
3. Роллбэк: удалить строки `dimension=region` + `DROP COLUMN region` — фильтр снова «Все».

## Open Questions

- Нужна ли пагинация 89 кнопок как альтернатива вводу — пока нет, только свободный ввод + подтверждение; остальные вопросы решим на реализации (как просил).
