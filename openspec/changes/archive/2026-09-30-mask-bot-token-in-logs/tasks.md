## 1. Код-фикс

- [x] 1.1 В `_configure_logging()` (`app/main.py`) после настройки хендлеров поднять
      логгеры `httpx` и `httpcore` до `logging.WARNING` (с коротким комментарием, что
      `httpx` на INFO пишет URL с токеном бота).
- [x] 1.2 Проверить конфиг: `python -c "from app.core.config import Config; print(Config.load())"`.
- [x] 1.3 В `_configure_logging()` добавить форматтер, заменяющий точное значение
      `config.TELEGRAM_BOT_TOKEN` на `***` в итоговой строке, и повесить его на
      консольный и файловый хендлеры.

## 2. Локальная проверка

- [x] 2.1 Запустить бота локально (`python -m app.main`) с `LOG_LEVEL=INFO`, дождаться
      нескольких циклов `getUpdates`, остановить.
- [x] 2.2 Убедиться, что `grep -c "api.telegram.org/bot" <LOG_FILE>` по свежим строкам
      даёт 0 и что токен (`grep -F "$TELEGRAM_BOT_TOKEN"`) в логе не встречается.
- [x] 2.3 Повторить 2.1–2.2 с `LOG_LEVEL=DEBUG` и убедиться, что строка `Set Bot API URL`
      содержит `bot***`, а не токен.

## 3. Выкатка в прод

- [x] 3.1 Ветка `fix/mask-bot-token-in-logs`, коммит, PR в `main`, merge — автодеплой Dockhost.
- [x] 3.2 В логах Dockhost после деплоя нет строк `HTTP Request: … api.telegram.org/bot…`.

## 4. Отложено до переезда на боевого бота

Не блокирует этот change. При переезде: прописать токен боевого бота в env Dockhost;
тестовый токен (уже утёкший в логи) отозвать через `/revoke` в @BotFather или удалить
тестового бота.
