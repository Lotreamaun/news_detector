## 1. Dockerfile

- [x] 1.1 Создать `Dockerfile`: `FROM python:3.14-slim`, `WORKDIR /app`, `COPY requirements.txt`
      + `pip install -r requirements.txt`, `COPY . .`, `ENV PYTHONPATH=.`, `EXPOSE 8080`,
      `CMD ["python", "-m", "app.main"]`.
- [x] 1.2 Добавить `.dockerignore` (`venv/`, `.git/`, `data/`, `logs/`, `.env`, `*.db`,
      `__pycache__/`) — секреты и локальные данные не должны попасть в образ.
- [x] 1.3 Локально собрать образ (`docker build -t news-detector .`) и убедиться, что сборка
      проходит без ошибок.

## 2. docker-compose

- [x] 2.1 Создать `docker-compose.yml`: сервис `bot` (`build: .`, `env_file: .env`,
      `restart: unless-stopped`, volumes `./data:/app/data` и `./logs:/app/logs`).
- [x] 2.2 Локально проверить `docker compose up -d --build` на пустом `data/` — бот стартует,
      создаёт схему БД, пишет логи в примонтированный `./logs`.

## 3. Конфигурация

- [x] 3.1 Дополнить `.env.example` переменными, которые уже читает `app/core/config.py`, но
      которых нет в примере: `FORCE_SUMMARIZE_MONTHLY_LIMIT` (default 10),
      `LOG_RETENTION_DAYS` (default 30).
- [x] 3.2 Добавить в `.env.example` прод-примечание рядом с `WEBAPP_HOST`/`WEBAPP_PORT`: для
      Dockhost (и вообще контейнерного деплоя) нужен `WEBAPP_HOST=0.0.0.0`, иначе внешний
      трафик до aiohttp-сервера не дойдёт (дефолт `127.0.0.1` — только для локальной
      разработки).

## 4. Документация деплоя (README)

- [x] 4.1 Добавить в `README.md` раздел «Деплой на Dockhost»: создание контейнера/проекта из
      этого репозитория, перенос `.env` на сервер вручную (какие переменные обязательны для
      прода: `TELEGRAM_PROXY_URL`, `WEBAPP_HOST=0.0.0.0`, `WEBAPP_URL=<домен Dockhost>`),
      `mkdir -p data logs`, `docker compose up -d --build`.
- [x] 4.2 Задокументировать настройку internal route в панели Dockhost: привязка
      динамического домена к сетевому сервису контейнера на порт 8080 (порт должен быть
      объявлен у контейнера как открытый для входящего TCP-трафика).
- [x] 4.3 Задокументировать смоук-тест после первого деплоя: `/start`, `/latest`, `/summary` +
      кнопка «Полный текст» открывает WebApp по HTTPS, бот получает апдейты через
      `TELEGRAM_PROXY_URL` (RU-сегмент блокирует `api.telegram.org` напрямую).

## 5. Прод-развёртывание на Dockhost

- [ ] 5.1 Создать проект/контейнер на Dockhost из репозитория.
- [ ] 5.2 Создать `.env` на сервере вручную со всеми прод-значениями (см. задачу 4.1).
- [ ] 5.3 Настроить internal route домен → порт 8080 в панели Dockhost; на месте проверить,
      действительно ли достаточно `EXPOSE 8080` в Dockerfile, или нужен дополнительный шаг
      в панели/конфиге Dockhost (открытый вопрос из `design.md`).
- [ ] 5.4 Выполнить `mkdir -p data logs` и `docker compose up -d --build` на сервере.
- [ ] 5.5 Пройти смоук-тест из задачи 4.3 на реальном проде и убедиться, что `is_first_run`
      не рассылает старые законы всем подписчикам.
