## 1. Признак псевдозаголовка

- [ ] 1.1 Добавить в `app/bot/handlers.py` хелпер `_has_real_title(article, external_id) -> bool` (False, если `article is None`, `title` пуст или `title == external_id`) с докстрингом

## 2. Применение во всех ветках `_summarize_and_reply`

- [ ] 2.1 Ветка кэша (`cached.summary` задан): передавать в `_summary_final` `title_known=_has_real_title(cached, external_id)`
- [ ] 2.2 `_deliver_summary_failure`: ветвиться по `_has_real_title(cached, external_id)` вместо `cached is not None` (псевдозаголовок → `_summary_refused_plain`); обновить докстринг
- [ ] 2.3 Успешный путь (ветка `article is not None`): `title_known = _has_real_title(article, external_id)`
- [ ] 2.4 Ветка `GigaChatError` (ветка `art is not None`): `title_known = _has_real_title(art, external_id)`

## 3. Ручная проверка

- [ ] 3.1 Проверить конфиг и импорт: `python -c "from app.core.config import Config; print(Config.load())"` и `python -c "import app.bot.handlers"`
- [ ] 3.2 В боте: `/summary <id не из ленты>` дважды — оба ответа в формате «саммари + id: … + Читать на портале + Полный текст», без жирного id
- [ ] 3.3 В боте: `/summary <id закона из ленты>` — заголовок по-прежнему жирный
