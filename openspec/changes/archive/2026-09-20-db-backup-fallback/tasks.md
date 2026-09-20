## 1. Конфигурация

- [x] 1.1 Добавить в `app/core/config.py` поля `DB_BACKUP_DIR` (str, default `./data/backups`),
      `DB_BACKUP_INTERVAL_HOURS` (int, default 24, через `_get_int_env`),
      `DB_BACKUP_RETENTION_COUNT` (int, default 7, через `_get_int_env`).
- [x] 1.2 Добавить эти же переменные с дефолтами и коротким комментарием в `.env.example`.

## 2. Сервис бэкапа (`app/services/backup.py`)

- [x] 2.1 Функция разбора пути к файлу SQLite из `config.DATABASE_URL`
      (`sqlalchemy.engine.make_url(...).database`); возвращает `None`/явный маркер, если
      схема URL не `sqlite`/`sqlite+aiosqlite`.
- [x] 2.2 Функция `create_backup(db_path, backup_dir) -> Path`: создаёт `backup_dir`, если
      его нет, формирует имя файла `<basename>_<YYYYmmdd_HHMMSS>.db`, выполняет
      `VACUUM INTO` через синхронный `sqlite3` в `asyncio.to_thread`.
- [x] 2.3 Функция `rotate_backups(backup_dir, retention_count)`: сортирует файлы бэкапа по
      имени (=по времени), удаляет все, кроме `retention_count` самых новых.
- [x] 2.4 Функция-оркестратор `run_backup_job(context)` (сигнатура job-callback PTB,
      как у `check_legislation_updates`): резолвит путь БД, при не-SQLite логирует
      предупреждение и выходит; иначе вызывает `create_backup` + `rotate_backups`; при любом
      исключении — логирует ошибку и рассылает уведомление в `config.ADMIN_CHAT_IDS` через
      `context.bot.send_message` (с обработкой `Forbidden` per chat, не прерывая рассылку
      остальным), не поднимая исключение дальше.

## 3. Интеграция в приложение (`app/main.py`)

- [x] 3.1 В `_schedule_jobs` зарегистрировать `run_backup_job` в `JobQueue` через
      `run_repeating(interval=config.DB_BACKUP_INTERVAL_HOURS * 3600, name="backup_database")`,
      по аналогии с `check_legislation_updates`.
- [x] 3.2 В `_post_init` (или отдельной функции, вызываемой оттуда, по аналогии с
      `_run_initial_check`) вызвать `run_backup_job` один раз сразу при старте — обход
      известной ловушки `run_repeating(first=0)`.

## 4. Документация

- [x] 4.1 Добавить в `README.md` раздел «Восстановление БД из бэкапа» с шагами из
      `design.md` (Migration Plan): остановить бота → скопировать файл из `DB_BACKUP_DIR`
      поверх текущего пути `DATABASE_URL` → запустить бота → смоук-тест `/start`, `/latest`.
- [x] 4.2 Упомянуть `DB_BACKUP_DIR`/`DB_BACKUP_INTERVAL_HOURS`/`DB_BACKUP_RETENTION_COUNT` в
      разделе окружения `README.md` (если там уже перечисляются переменные `.env`).

## 5. Проверка

- [x] 5.1 Локально запустить бота, убедиться, что при старте в `DB_BACKUP_DIR` появляется
      файл бэкапа, а лог содержит запись об успешном бэкапе.
- [x] 5.2 Проверить ротацию: временно выставить `DB_BACKUP_RETENTION_COUNT=1`, вызвать бэкап
      дважды (например, перезапуском), убедиться, что в `DB_BACKUP_DIR` остаётся один файл.
- [x] 5.3 Проверить отказоустойчивость: сделать `DB_BACKUP_DIR` недоступной для записи
      (например, `chmod 000`) и убедиться, что бот не падает, ошибка в логе есть, и (при
      настроенном `ADMIN_CHAT_IDS`) приходит уведомление в Telegram.
- [x] 5.4 Проверить восстановление по инструкции из README: остановить бота, подменить
      `news_detector.db` файлом из `DB_BACKUP_DIR`, запустить бота, убедиться, что
      пользователи/фильтры/статьи на месте.
