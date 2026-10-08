## Why

Бот сейчас запускается только вручную (`python -m app.main`) на машине разработчика — нет
контейнеризации и нет задокументированного процесса выкладки на боевой сервер. Целевая
платформа (Dockhost, RU PaaS с Docker) уже зафиксирована в `docs/vision.md`, но черновой
чек-лист в `docs/PLAN.md` (Этап 9) остаётся невыполненным. Нужно закрыть этот пробел, чтобы
бот можно было развернуть как постоянно работающий сервис.

## What Changes

- Добавить `Dockerfile` (python:3.14-slim, зависимости из `requirements.txt`, `EXPOSE 8080`,
  `CMD ["python", "-m", "app.main"]`).
- Добавить `docker-compose.yml`: один сервис `bot`, `env_file: .env`, volumes для `./data`
  (SQLite) и `./logs` (ротация логов), `restart: unless-stopped`.
- Дополнить `.env.example` недокументированными переменными, которые уже читает
  `app/core/config.py`: `FORCE_SUMMARIZE_MONTHLY_LIMIT` (default 10), `LOG_RETENTION_DAYS`
  (default 30) — плюс прод-примечание про `WEBAPP_HOST=0.0.0.0` (в контейнере дефолтный
  `127.0.0.1` недоступен снаружи).
- Добавить в `README.md` раздел о выкладке на Dockhost: создание контейнера из образа,
  настройка internal-маршрута домен → порт 8080 (сетевой сервис Dockhost), перенос `.env` на
  сервер вручную, `mkdir -p data`, `docker compose up -d --build`.
- Не переносить существующую локальную `news_detector.db` — прод стартует с чистой БД
  (`init_db_schema()` создаёт таблицы на пустом volume).

Никаких изменений в логике `app/` — это чисто упаковка/конфигурация для деплоя, код уже
поддерживает нужные переменные окружения.

## Capabilities

### New Capabilities
- `deployment`: контейнеризация бота (Dockerfile, docker-compose) и процесс ручной выкладки на
  Dockhost — включая работающий снаружи WebApp через динамический домен Dockhost.

### Modified Capabilities
(нет — поведение существующих capability не меняется, только способ запуска процесса)

## Impact

- Новые файлы: `Dockerfile`, `docker-compose.yml`.
- Изменённые файлы: `.env.example` (новые переменные + прод-примечание), `README.md` (раздел
  деплоя).
- Код `app/` не меняется.
- Прод-`.env` создаётся вручную на сервере (не в репозитории), с `TELEGRAM_PROXY_URL`
  (сервер в Москве, `api.telegram.org` заблокирован из РФ), `WEBAPP_HOST=0.0.0.0`,
  `WEBAPP_URL=<динамический домен Dockhost>`.
