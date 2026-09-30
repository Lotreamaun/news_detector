## 1. Метаданные документа с портала

- [ ] 1.1 Добавить в `app/services/rss_parser.py` `fetch_document(external_id) -> FeedEntry | None`: `GET http://publication.pravo.gov.ru/api/Document?eoNumber=<id>` через `_fetch_with_retries`, разбор через `_entry_from_item`; любая ошибка/«Документ не найден» → `None` + лог, без исключений; докстринг
- [ ] 1.2 Проверить вручную: `fetch_document` для существующего id возвращает настоящее название, для `9999999999999999` — `None`

## 2. Хелперы в `app/bot/handlers.py`

- [ ] 2.1 Добавить `_has_real_title(article, external_id) -> bool` (False, если записи нет, `title` пуст или равен `external_id`)
- [ ] 2.2 Добавить хелпер применения метаданных: по результату `fetch_document` проставить записи `title`, `url`, `level` (`classify_level_for_title`); при `None` — псевдозаголовок `external_id` и канонический url, как сейчас

## 3. `_summarize_and_reply`

- [ ] 3.1 Лечение: сразу после загрузки `cached`, если `not _has_real_title(cached, external_id)`, один раз вызвать `fetch_document` и при успехе обновить запись в БД
- [ ] 3.2 Перенести расчёт `auto_bypass` после лечения и считать его из `cached.level` (убрать отдельную выборку `Article.level`)
- [ ] 3.3 Ветка кэша: передавать `title_known=_has_real_title(cached, external_id)` в `_summary_final`
- [ ] 3.4 Успешный путь и ветка `GigaChatError`: при создании новой записи сначала `fetch_document` и хелпер из 2.2; `title_known` брать из `_has_real_title`
- [ ] 3.5 `_deliver_summary_failure`: выбирать формат по `_has_real_title(cached, external_id)` вместо `cached is not None`; обновить докстринг

## 4. Ручная проверка

- [ ] 4.1 `python -c "from app.core.config import Config; print(Config.load())"` и `python -c "import app.bot.handlers"`
- [ ] 4.2 В боте: `/summary <id не из ленты>` — настоящее название жирным, ссылка, «Полный текст»; повторный `/summary` — то же из кэша
- [ ] 4.3 В боте: `/summary` для старой записи с `title == external_id` (например, документ из исходного бага) — запись вылечена, название настоящее
- [ ] 4.4 Запасной путь: временно подменить URL портала на недоступный (или id, которого нет на портале) — ответ без жирного id, бот не падает
- [ ] 4.5 `/summary <id закона из ленты>` — название жирным, запроса к `/api/Document` в логе нет
