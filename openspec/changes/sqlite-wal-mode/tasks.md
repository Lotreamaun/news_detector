## 1. Режим журнала и ожидание в `app/core/database.py`

- [x] 1.1 Константа `_SQLITE_BUSY_TIMEOUT_SECONDS = 10` с комментарием, почему 10 с (design D2). `init_database` передаёт `connect_args={"timeout": _SQLITE_BUSY_TIMEOUT_SECONDS}` в `create_async_engine` только если `make_url(database_url).get_backend_name() == "sqlite"`. Docstring `init_database` дополнить
- [x] 1.2 Функция `_enable_wal(engine)` с docstring: только для `engine.dialect.name == "sqlite"`, отдельное `engine.connect()`, `PRAGMA journal_mode=WAL`, чтение ответа. `wal` → INFO с режимом; иной ответ → WARNING с полученным режимом; любое исключение → WARNING с причиной, без проброса (design D1). Логгер модуля `logging.getLogger(__name__)`
- [x] 1.3 `init_db_schema` вызывает `_enable_wal(engine)` первым шагом, до `engine.begin()`. Docstring `init_db_schema` дополнить: перед созданием таблиц включается WAL

## 2. Служебные файлы и документация

- [x] 2.1 `.gitignore`: рядом с `*.db` добавить `*.db-wal` и `*.db-shm` (design D5)
- [x] 2.2 `README.md`, «Восстановление БД из бэкапа»: после шага остановки бота добавить шаг — удалить `<путь БД>-wal` и `<путь БД>-shm`, если они есть, с примером `rm -f data/news_detector.db-wal data/news_detector.db-shm` и пояснением, почему это обязательно (design D5)
- [x] 2.3 `CLAUDE.md`: в строке `app/core/database.py` таблицы «Key modules» и в абзаце о SQLite в «Deployment» отметить режим WAL, ожидание блокировки 10 с, файлы `-wal`/`-shm` рядом с БД

## 3. Проверка

- [x] 3.1 `python -c "from app.core.config import Config; print(Config.load())"` и импорт `app.main` без ошибок
- [x] 3.2 Скрипт в scratchpad (не в репозитории), на временном файле БД во временном каталоге, без сети и без `data/` проекта. Проверить: (а) после `init_database` + `init_db_schema` на новой базе `PRAGMA journal_mode` = `wal`, `PRAGMA busy_timeout` = `10000`, в логе INFO о режиме; (б) база, заранее созданная в режиме `delete` с данными, после `init_db_schema` в `wal`, данные на месте; повторный `init_db_schema` проходит без ошибок; (в) отдельное соединение `sqlite3` держит открытую транзакцию чтения (`BEGIN; SELECT ...`), а запись через сессию движка фиксируется сразу, без ожидания; (г) отдельное соединение держит запись (`BEGIN IMMEDIATE`) ~2 с и отпускает → запись через движок ждёт и фиксируется; держит ~12 с → запись через движок падает с `database is locked` примерно через 10 с; (д) пользователь записан, соединение движка открыто (checkpoint не прошёл, `-wal` не пустой), `backup.create_backup` → в копии, открытой отдельно, пользователь есть; рядом с копией нет `-wal`/`-shm`; (е) `sqlite+aiosqlite://` (база в памяти) → WARNING с режимом `memory`, `init_db_schema` завершается; (ж) переключение режима падает (база в `delete`, отдельное соединение держит `BEGIN EXCLUSIVE` дольше 10 с во время `init_db_schema`) → WARNING с причиной, `init_db_schema` завершается после освобождения
- [x] 3.3 Проверить чтением кода, что для не-SQLite URL `connect_args` не передаётся и `_enable_wal` ничего не делает (драйвера другой СУБД в `requirements.txt` нет, запуск не проверить)
- [x] 3.4 Локальный запуск бота (`python -m app.main`) на копии рабочей базы: в логе старта строка о режиме `wal`, рядом с БД появились `-wal`/`-shm`, `/start` и `/today` отвечают, первый бэкап создан. После штатной остановки (Ctrl+C) файла `-wal` нет

## 4. Деплой и отметки

- [x] 4.1 `docs/PLAN.md`: отметить п. 7 P0 выполненным, кратко что сделано (WAL при старте, `busy_timeout` 10 с, шаг восстановления в README)
- [ ] 4.2 После деплоя на прод (вместе с остальным P0): в логе старта строка о режиме `wal`, нет WARNING о режиме журнала. Если WARNING есть — разобрать причину до анонса
