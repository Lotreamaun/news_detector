## 1. Сервис принудительной саммаризации

- [ ] 1.1 Создать `app/services/force_summary.py`: dataclass `ForceSummaryResult` (`status` ∈ `ok|limit|no_text|refused|error`, `title`, `url`, `title_known`, `summary`, `text`, `cached_article`, `fallback`) и `async force_summarize(config, session_maker, user, external_id)` — перенести из `_summarize_and_reply` без изменения ветвлений: кеш саммари, лечение псевдозаголовка, лимит (`AUTO_LEVELS`, `ADMIN_CHAT_IDS`), текст/OCR, сохранение текста, саммари, сохранение/создание `Article`, `+1` к `SummarizationUsage`, ветки `GigaChatError` и общего исключения; ни одного вызова Telegram; докстринг
- [ ] 1.2 Перенести `AUTO_LEVELS` в сервис (и нужные хелперы `_has_real_title`/`_apply_document_metadata`, если они используются только саммаризацией — иначе импортировать), обновить импорты в `app/bot/handlers.py`
- [ ] 1.3 Переписать `_summarize_and_reply` в адаптер: вызвать `force_summarize` и по `status` отдать в `_deliver` / `_deliver_summary_failure` / `_summary_refused` / `_summary_final` те же тексты и разметку, что сейчас
- [ ] 1.4 Проверить, что бот не изменился: `python -c "import app.bot.handlers"`, затем в боте `/summary <id>` (новое саммари, повтор из кеша), кнопка «Сделать саммари» в уведомлении и «№N» в тизере, сообщение об исчерпанном лимите — тексты как до изменения

## 2. Бэкенд WebApp

- [ ] 2.1 Пробросить `config` в WebApp: `start_webapp(host, port, session_maker, config)` → `create_app(session_maker, config)` → `app["config"]`; обновить вызов в `app/main.py` `_start_webapp`
- [ ] 2.2 `GET /full_text`: добавить в ответ поле `summary` (`article.summary` или `null`)
- [ ] 2.3 Добавить `_verify_init_data(init_data, bot_token) -> int | None` в `app/webapp/server.py`: HMAC-проверка по схеме Telegram (`WebAppData` → secret, отсортированный `data_check_string` без `hash`, `hmac.compare_digest`), `auth_date` не старше 24 ч, возврат `user.id`; `initData` не логировать
- [ ] 2.4 Добавить `POST /summarize`: разбор JSON (`external_id`, `init_data`) → 400 при ошибке; `_verify_init_data` → 401; поиск `User` по `telegram_id` → 403 «Откройте бота и нажмите /start»; при `REQUIRED_CHANNEL_ID` и `not channel_verified` → 403 с текстом про подписку; вызов `force_summarize`; 200 `{status, summary, text, is_text_available, message}`; 500 на непредвиденное; CORS-заголовок как у `/full_text`; зарегистрировать роут в `create_app`
- [ ] 2.5 Проверить вручную: `_verify_init_data` на строке, подписанной тестовым токеном в `python -c` (валидная → id, изменённый байт → `None`, старый `auth_date` → `None`); `curl -X POST /summarize` без `init_data` → 401

## 3. Фронтенд карточек дайджеста (`HTML_PAGE`)

- [ ] 3.1 CSS: перенести эллипсис с `.doc summary` на `.doc-head-title`; стили `.sum` (метка «Саммари», нейтральный фон темы, левая полоса hint-цвета), `.sum-preview` с ограничением ~3 строки, `.doc[open] .sum-preview { display: none; }`, `.sum-action` с кнопкой в цветах `--tg-theme-button-*` и строкой статуса
- [ ] 3.2 JS: выделить `renderCard(details, data, id)`, собирающую `<summary>` (заголовок + превью саммари при наличии) и тело (полный заголовок → дата → блок саммари или `.sum-action` → текст/фолбэк → ссылка); первичная загрузка из `/full_text` идёт через неё
- [ ] 3.3 JS: обработчик «Сделать саммари» — проверка `tg.initData` (пусто → сообщение «доступно при открытии дайджеста из Telegram»), блокировка кнопки и «⏳ Саммари в процессе создания…», `POST /summarize`, обработка `ok` / `refused` / `limit` / `no_text` / `error` / 401 / 403 / сетевой ошибки согласно design.md (решение 5)
- [ ] 3.4 Убедиться, что одиночный режим (`?external_id=`) не изменился и что карточка без саммари в свёрнутом виде выглядит как раньше

## 4. Ручная проверка end-to-end

- [ ] 4.1 `python -c "from app.core.config import Config; print(Config.load())"` и `python -c "import app.webapp.server"`
- [ ] 4.2 В браузере `/app?ids=<с саммари>,<без саммари>`: превью саммари под заголовком в свёрнутом виде, в развёрнутом — после даты и только один раз; у карточки без саммари свёрнутый вид прежний, в развёрнутом есть кнопка; нажатие вне Telegram показывает сообщение и не шлёт запрос
- [ ] 4.3 В Telegram через «📖 Дайджест»: развернуть карточку без саммари → «Сделать саммари» → индикатор → появились саммари (в обоих положениях) и распознанный текст, кнопка исчезла; в чат бот ничего не прислал; счётчик `summarization_usage` +1 (для не-ФЗ/ФКЗ)
- [ ] 4.4 Неудачи: лимит исчерпан (временно `FORCE_SUMMARIZE_MONTHLY_LIMIT=0`) → сообщение, кнопка убрана; документ без доступного текста → сообщение, кнопка доступна; другие карточки не затронуты
- [ ] 4.5 Светлая и тёмная тема Telegram: блок саммари и кнопка читаемы и выглядят нейтрально
