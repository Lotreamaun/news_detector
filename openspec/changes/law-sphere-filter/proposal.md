## Why

После среза по силе (law-force-filter) пользователи всё равно получают шум внутри одной силы — юристу по финансам не нужны законы про ЖКХ. Нужен второй срез онбординга — фильтр по сфере регулирования (природа, здравоохранение, финансы, стройка...), но без LLM: ключевые слова + целевые формы дают 70-80% точности для MVP, 0₽ и <10мс, детерминированно. Словари уточним на реализации, сейчас фиксируем механику.

## What Changes

- Модель: `UserFilter` расширить полем `sphere` (enum, multi), `Article` — полем `spheres TEXT` (CSV или JSON, multi-label), default «Все сферы» = пустой фильтр.
- Классификатор `classify_sphere(title, summary)` без LLM: нормализация → лемматизация (`pymorphy2` или `natasha`) → подсчёт вхождений лемм из словарей сфер → `score` по уникальным леммам, порог `>=2` → сфера, иначе `other`. Точка проверки — `title + summary` (fallback первые 1000 знаков `original_text`). Допускается multi-label (2-3 сферы).
- Словари сфер — заготовки по 15-30 лемм на сферу, конкретика на реализации (природа/здравоохранение/финансы/стройка/уголовное/гражданское+арбитраж/IP+IT/трудовое/образование/оборона/транспорт/административное).
- Онбординг-визард шаг 1/3 «Сфера»: 12 кнопок Inline с toggle ☑, `[Выбрать всё]`/`[Далее]`, состояние в `context.user_data` → `user_filters`.
- Применение: `scheduler._notify_users` и `handlers.latest` фильтруют по `spheres` (OR: статья подходит если хоть одна её сфера в фильтре юзера; фильтр «Все» = пропуск).

## Capabilities

### New Capabilities
- `law-sphere-filter`: фильтр законов по сфере регулирования в онбординге и рассылке (модель, классификатор по ключевым словам/леммам без LLM, UI-визард, применение).

### Modified Capabilities
<!-- нет изменений существующих -->

## Impact

- `app/models/user_filter.py` / `app/models/article.py`: добавить `sphere(s)`.
- Новый `app/services/sphere_keywords.py` (словари) + `app/services/sphere_classifier.py` (`classify_sphere`).
- `app/services/rss_parser.py`: вызывать классификатор при `_entry_from_item`/`_save_article`.
- `app/bot/handlers.py`: шаг визарда «Сфера» (`ConversationHandler`).
- `app/services/scheduler.py`: `matches()` по сферам.
- Зависимость: `pymorphy2` (или `pymorphy3`, `natasha`) для лемматизации.
- Без внешних LLM-API, без миграций вне `user_filters`/`articles.spheres`.
