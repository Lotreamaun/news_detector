## 1. Модель и схема БД

- [x] 1.1 Добавить поле `channel_verified: Mapped[bool]` в `app/models/user.py` (`Boolean()`, `default=True`, `server_default=text("1")`, по образцу `is_active`)
- [x] 1.2 Добавить в `app/core/database.py` функцию `_ensure_channel_verified_column`, вызываемую из `init_db_schema()` после `create_all`: через `PRAGMA table_info(users)` проверить наличие колонки `channel_verified`, при отсутствии выполнить `ALTER TABLE users ADD COLUMN channel_verified BOOLEAN NOT NULL DEFAULT 1`
- [x] 1.3 Вручную проверить на локальной копии `data/*.db` (или тестовой БД), что рестарт бота на уже существующей БД добавляет колонку без потери данных и без ошибок

## 2. Конфигурация

- [x] 2.1 Добавить `REQUIRED_CHANNEL_ID: str | None` в `Config` (`app/core/config.py`), необязательное поле, без записи в обязательные при `Config.load()`
- [x] 2.2 Добавить `REQUIRED_CHANNEL_ID=` (пусто, с комментарием) в `.env.example`
- [x] 2.3 При `Config.load()` логировать WARNING, если `REQUIRED_CHANNEL_ID` не задан ("гейт подписки отключён")

## 3. Сервис проверки подписки

- [x] 3.1 Создать `app/services/channel_subscription.py` с `async def is_subscribed(bot: Bot, telegram_id: int, channel_id: str) -> bool`, использующей `bot.get_chat_member`
- [x] 3.2 Трактовать статусы `member`/`administrator`/`creator` как подписан, `left`/`kicked` — как не подписан
- [x] 3.3 Обернуть вызов `get_chat_member` в try/except: любое исключение (бот не в канале, сетевая ошибка и т.п.) → лог WARNING, возврат `False` (fail-closed)
- [x] 3.4 Если `channel_id` не задан (гейт отключён по Decision из design.md) — функция возвращает `True` без вызова Telegram API

## 4. Обработчики бота

- [x] 4.1 В обработчике `/start` (`app/bot/handlers.py`) при создании нового пользователя явно выставлять `channel_verified=False`
- [x] 4.2 Добавить новый `CallbackQueryHandler` "Проверить подписку" (например `callback_data="check_subscription"`): вызывает `is_subscribed`, при `True` — обновляет `channel_verified=True` в БД и показывает подтверждение; при `False` — сообщает об отсутствии подписки, оставляет кнопку доступной
- [x] 4.3 Зарегистрировать новый handler в `app/main.py` (`_build_application`), рядом с существующими `CallbackQueryHandler`

## 5. Гейтинг рассылки

- [x] 5.1 В `app/services/scheduler.py` (`_notify_users`) расширить `select(User).where(...)` условием `User.channel_verified.is_(True)` рядом с существующим `User.is_active.is_(True)`

## 6. Проверка

- [x] 6.1 Вручную прогнать сценарий: новый `telegram_id` → `/start` → `channel_verified=False` в БД → пользователь не получает уведомление о новой статье, пока не пройдёт проверку
- [x] 6.2 Вручную прогнать сценарий: нажатие "Проверить подписку" без реальной подписки на канал → сообщение об отсутствии подписки, кнопка остаётся
- [x] 6.3 Вручную прогнать сценарий: подписка на канал есть → нажатие "Проверить подписку" → `channel_verified=True`, пользователь начинает получать уведомления
- [x] 6.4 Убедиться, что 2 существующих пользователя (тестовых) после деплоя сохраняют `channel_verified=True` и рассылка для них не прерывается
- [x] 6.5 Убедиться, что при пустом `REQUIRED_CHANNEL_ID` бот стартует без ошибок и ведёт себя как раньше (гейт неактивен)
