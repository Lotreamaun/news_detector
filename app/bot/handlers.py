"""
Telegram-обработчики команд бота.
"""

import logging
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import desc, select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.helpers import escape_markdown
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, ConversationHandler

from app.models import Article, User
from app.services.channel_subscription import is_subscribed
from app.services.force_summary import force_summarize as run_force_summarize
from app.services.rss_parser import IMPORTANT_LEVELS, _normalize_text
from app.services.scheduler import (
    DIGEST_SUMMARIZE_PREFIX,
    FORCE_SUMMARIZE_PREFIX,
    _build_digest,
    _build_notification,
)

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "Доступные команды:\n"
    "/start — регистрация и настройка фильтров\n"
    "/latest — последние федеральные законы (ФКЗ/ФЗ) за 30 дней\n"
    "/settings — настройка фильтров\n"
    "/summary <id или ссылка> — принудительно сделать саммари\n"
    "/help — эта справка"
)


LEVELS: list[str] = ["CONSTITUTION", "FKZ", "FZ", "DECREE", "GOV_RESOLUTION", "DEPARTMENTAL", "REGIONAL"]
LEVEL_LABELS: dict[str, str] = {
    "CONSTITUTION": "Конституция",
    "FKZ": "ФКЗ",
    "FZ": "ФЗ",
    "DECREE": "Указы",
    "GOV_RESOLUTION": "Постановления",
    "DEPARTMENTAL": "Ведомственные",
    "REGIONAL": "Региональные",
}
DEFAULT_LEVELS: set[str] = set(IMPORTANT_LEVELS)
LEVEL_STATE: int = 0


def _session_maker(context: ContextTypes.DEFAULT_TYPE):
    """Достает фабрику сессий, положенную в bot_data при старте приложения."""
    return context.bot_data["session_maker"]


def _build_level_keyboard(selected: set[str]) -> InlineKeyboardMarkup:
    """Строит клавиатуру для выбора уровней силы."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for lvl in LEVELS:
        label = LEVEL_LABELS[lvl]
        mark = "☑" if lvl in selected else "☐"
        row.append(InlineKeyboardButton(f"{label} {mark}", callback_data=f"level:{lvl}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    all_label = "Снять всё" if len(selected) == len(LEVELS) else "Выбрать всё"
    rows.append([InlineKeyboardButton(all_label, callback_data="levels:all")])
    rows.append([InlineKeyboardButton("Далее", callback_data="levels:next")])
    return InlineKeyboardMarkup(rows)


async def _get_user_levels(session_maker, user_id: int) -> set[str]:
    """Возвращает выбранные уровни пользователя, пусто = Все."""
    from app.models.user_filter import UserFilter

    async with session_maker() as session:
        result = await session.scalars(select(UserFilter.level).where(UserFilter.user_id == user_id))
        levels = set(result.all())
    return levels


def _window_start(days: int) -> datetime:
    """Начало окна «n дней» — полночь дня (n)-дня назад (календарная граница).

    Иначе ``now - 30d`` (с временем суток) отсекает закон, опубликованный
    утром ровно 30 дней назад (midnight < since).
    """
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days)
    return datetime.combine(start, time.min, tzinfo=timezone.utc)


async def _save_user_levels(session_maker, user_id: int, levels: set[str]) -> None:
    """Сохраняет уровни (пусто = Все). DELETE+INSERT."""
    from app.models.user_filter import UserFilter

    async with session_maker() as session:
        # удалить старые
        old = await session.scalars(select(UserFilter).where(UserFilter.user_id == user_id))
        for obj in old.all():
            await session.delete(obj)
        # вставить новые (пусто = Все, не вставляем ничего)
        for lvl in levels:
            if lvl in LEVELS:
                session.add(UserFilter(user_id=user_id, level=lvl))
        await session.commit()


# ── Визард «Сила» ────────────────────────────────────────────────────────

async def _show_level_step(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> int:
    """Показывает шаг выбора силы, инициализирует context.user_data['levels']."""
    session_maker = _session_maker(context)
    levels = await _get_user_levels(session_maker, user_id)
    # пусто = Все → по умолчанию все включены
    if not levels:
        levels = set(LEVELS)
    context.user_data["levels"] = levels
    context.user_data["wizard_user_id"] = user_id
    text = "Шаг 1/1 — Сила закона:\nВыберите какие акты получать (можно несколько):"
    markup = _build_level_keyboard(levels)
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text=text, reply_markup=markup)
    elif update.message:
        await update.message.reply_text(text=text, reply_markup=markup)
    return LEVEL_STATE


async def level_wizard_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point для /start (новый) и /settings — показывает приветствие визарда."""
    if update.effective_user is None:
        return ConversationHandler.END
    telegram_id = update.effective_user.id
    session_maker = _session_maker(context)
    user = await _get_or_create_user(session_maker, telegram_id, update)
    if user is None:
        return ConversationHandler.END
    # Для /start нового — уже зарегистрировали выше, но здесь универсально
    text = (
        "Настроим подписку (30 сек):\n"
        "Выберите по силе — какие законы получать."
    )
    markup = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Настроить", callback_data="wizard:setup")],
            [InlineKeyboardButton("Пропустить → Все", callback_data="wizard:skip")],
        ]
    )
    if update.message:
        await update.message.reply_text(text=text, reply_markup=markup)
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text=text, reply_markup=markup)
    return LEVEL_STATE


async def level_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает toggle уровня, Выбрать всё и Далее."""
    query = update.callback_query
    if query is None or update.effective_user is None:
        return LEVEL_STATE
    data = query.data or ""
    levels: set[str] = context.user_data.get("levels", set(LEVELS))
    user_id: int = context.user_data.get("wizard_user_id") or 0
    # если user_id не в context (рестарт), достаём из БД
    if not user_id:
        session_maker = _session_maker(context)
        user = await _get_or_create_user(session_maker, update.effective_user.id, update)
        user_id = user.id if user else 0
        context.user_data["wizard_user_id"] = user_id

    if data == "wizard:skip":
        await _save_user_levels(_session_maker(context), user_id, set())
        await query.answer()
        await query.edit_message_text("Готово! Фильтр — «Все уровни». Изменить: /settings")
        return ConversationHandler.END
    if data == "wizard:setup":
        # показать шаг Сила
        return await _show_level_step(update, context, user_id)
    if data.startswith("level:"):
        lvl = data.split(":", 1)[1]
        if lvl in LEVELS:
            if lvl in levels:
                levels.remove(lvl)
            else:
                levels.add(lvl)
            context.user_data["levels"] = levels
        await query.answer()
        await query.edit_message_text(
            text="Шаг 1/1 — Сила закона:\nВыберите какие акты получать (можно несколько):",
            reply_markup=_build_level_keyboard(levels),
        )
        return LEVEL_STATE
    if data == "levels:all":
        # toggle все: если все выбраны → снять, иначе выбрать все
        if len(levels) == len(LEVELS):
            levels = set()
        else:
            levels = set(LEVELS)
        context.user_data["levels"] = levels
        await query.answer()
        await query.edit_message_text(
            text="Шаг 1/1 — Сила закона:\nВыберите какие акты получать (можно несколько):",
            reply_markup=_build_level_keyboard(levels),
        )
        return LEVEL_STATE
    if data == "levels:next":
        # пусто = Все (не сохраняем ничего)
        to_save = levels if len(levels) != len(LEVELS) else set()
        # если пусто после toggle (сняли всё) — считаем Все
        if not to_save and len(levels) == 0:
            to_save = set()
        await _save_user_levels(_session_maker(context), user_id, to_save)
        await query.answer()
        if not to_save:
            await query.edit_message_text("Готово! Фильтр — «Все уровни». Изменить: /settings")
        else:
            labels = ", ".join(LEVEL_LABELS[l] for l in sorted(to_save))
            await query.edit_message_text(f"Готово! Выбрано: {labels}. Изменить: /settings")
        return ConversationHandler.END

    await query.answer()
    return LEVEL_STATE


async def level_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отмена визарда."""
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("Настройка отменена. /settings — изменить позже.")
    elif update.message:
        await update.message.reply_text("Настройка отменена. /settings — изменить позже.")
    return ConversationHandler.END


def _format_summary(summary: str | None, title: str) -> str:
    """
    Оформляет саммари в аккуратную MarkdownV2-разметку.

    Заголовок нормализуется от HTML-артефактов и выделяется жирным,
    саммари экранируется (MarkdownV2) и нормализуется от артефактов.
    Возвращает текст с parse_mode="MarkdownV2".
    """
    title_clean = _normalize_text(title) or title
    title_esc = escape_markdown(title_clean, version=2)
    if not summary:
        body = f"*{title_esc}*"
    else:
        summary_clean = _normalize_text(summary) or summary
        summary_esc = escape_markdown(summary_clean, version=2)
        body = f"*{title_esc}*\n\n{summary_esc}"
    return body


def _full_text_block(article_url: str) -> str:
    """Формирует MarkdownV2-ссылку на оригинал документа на правовом портале.

    Ссылка на оригинал присутствует всегда.
    """
    original = escape_markdown(article_url, version=2)
    return f"[Читать на портале]({original})"


def _full_text_button(config, external_id: str) -> InlineKeyboardMarkup | None:
    """Кнопка «Полный текст», открывающая Mini-App (WebApp) с законом.

    Возвращает None, если WebApp не сконфигурирован (пустой WEBAPP_URL).
    """
    base = getattr(config, "WEBAPP_URL", "")
    if not base or not base.strip():
        return None
    url = f"{base.rstrip('/')}/app?external_id={external_id}"
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📖 Полный текст", web_app=WebAppInfo(url=url))]]
    )


def _summary_refused(
    config,
    title: str,
    url: str,
    fallback: str,
    external_id: str,
    with_webapp_button: bool = True,
    *,
    title_known: bool = True,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Собирает уведомление с фолбэком вместо саммари при неудачной саммаризации.

    Фолбэк встраивается в тело уведомления на место саммари: остаются название
    и ссылка на портал. Кнопка «Полный текст» (WebApp) добавляется только если
    with_webapp_button=True — есть смысл показывать её лишь тогда, когда текст
    закона реально доступен (например, отказ LLM, но не отсутствие текста).
    title_known=False — заголовок на самом деле не известен (title == external_id),
    жирным его не показываем (вводит в заблуждение) — вместо этого id отдельной строкой.
    """
    fallback_esc = escape_markdown(fallback, version=2)
    if title_known:
        title_clean = _normalize_text(title) or title
        title_esc = escape_markdown(title_clean, version=2)
        header = f"*{title_esc}*\n\n_{fallback_esc}_"
    else:
        id_esc = escape_markdown(external_id, version=2)
        header = f"_{fallback_esc}_\n\nid: {id_esc}"
    return (
        f"{header}\n\n{_full_text_block(url)}",
        _full_text_button(config, external_id) if with_webapp_button else None,
    )


def _summary_refused_plain(fallback: str, external_id: str, url: str) -> str:
    """Простое служебное сообщение о неудачной саммаризации без псевдозаголовка.

    Используется вместо notification-style _summary_refused, когда настоящего
    названия нет: записи нет в БД или у неё псевдозаголовок (см. force_summary._has_real_title) —
    показывать external_id жирным как будто это заголовок закона было бы
    вводящим в заблуждение. Без MarkdownV2, чтобы
    не экранировать id/ссылку вручную (Telegram сам делает ссылку кликабельной
    в обычном тексте).
    """
    return f"{fallback}\n\nid: {external_id}\nЧитать на портале: {url}"


async def _deliver_summary_failure(
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
    message: object | None,
    status_message: object | None,
    config,
    title_known: bool,
    title: str,
    url: str,
    external_id: str,
    fallback: str,
) -> None:
    """Доставляет сообщение о неудачной принудительной саммаризации.

    У записи настоящее название — notification-style через _summary_refused
    (заголовок + причина + ссылка). Записи нет в БД (реалистично только для
    /summary с произвольным external_id) или у неё псевдозаголовок (title ==
    external_id, портал не отдал метаданные) — простое служебное сообщение:
    показать заголовок в этом случае нечего.
    """
    if title_known:
        final, reply_markup = _summary_refused(
            config, title, url, fallback, external_id, with_webapp_button=False
        )
        await _deliver(
            context,
            user,
            message,
            final,
            status_message=status_message,
            parse_mode="MarkdownV2",
            reply_markup=reply_markup,
        )
    else:
        final = _summary_refused_plain(fallback, external_id, url)
        await _deliver(context, user, message, final, status_message=status_message)


def _summary_final(
    config,
    title: str,
    url: str,
    summary: str | None,
    external_id: str,
    *,
    title_known: bool = True,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Собирает итог саммаризации: MarkdownV2-текст + кнопку Mini-App (если есть).

    Кнопка «Полный текст» показывается только когда есть саммари (важный закон — сразу,
    иначе после принудительной саммаризации). title_known=False — у записи псевдозаголовок
    (закон не из ленты, а портал не отдал метаданные, см. force_summary._has_real_title):
    title в этом случае — не настоящее название, а сам external_id; показывать его
    жирным как заголовок было бы вводящим в заблуждение (тот же принцип, что и в
    _summary_refused_plain для фолбэков) — вместо этого просто саммари + id для сверки.
    """
    if title_known:
        header = _format_summary(summary, title)
    else:
        summary_esc = escape_markdown(_normalize_text(summary) or summary or "", version=2)
        id_esc = escape_markdown(external_id, version=2)
        header = f"{summary_esc}\n\nid: {id_esc}" if summary_esc else f"id: {id_esc}"
    button = _full_text_button(config, external_id) if summary else None
    return (
        f"{header}\n\n{_full_text_block(url)}",
        button,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обработчик /start: регистрирует пользователя в БД и приветствует.

    Для нового пользователя предлагает визард настройки фильтров.
    """
    if update.effective_user is None or update.message is None:
        return
    telegram_id = update.effective_user.id
    username = update.effective_user.username
    config = context.bot_data["config"]

    is_new = False
    async with _session_maker(context)() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            from app.models.user_filter import UserFilter

            user = User(telegram_id=telegram_id, username=username, channel_verified=False)
            session.add(user)
            await session.flush()
            # дефолт для нового юзера — Конституция + ФКЗ + ФЗ
            for lvl in DEFAULT_LEVELS:
                session.add(UserFilter(user_id=user.id, level=lvl))
            await session.commit()
            await session.refresh(user)
            logger.info("Зарегистрирован новый пользователь telegram_id=%s", telegram_id)
            is_new = True
        else:
            logger.info("Пользователь telegram_id=%s уже зарегистрирован", telegram_id)

    if is_new:
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Как это работает?", callback_data="onboarding:example")]]
        )
        await update.message.reply_text(
            "Привет! Я слежу за новыми российскими законами и присылаю короткие саммари, когда выходит что-то важное.",
            reply_markup=markup,
        )
    elif config.REQUIRED_CHANNEL_ID and not user.channel_verified:
        # Уже зарегистрирован, но пропустил подтверждение подписки на онбординге —
        # показываем баннер подписки, а не приветствие со списком команд, которыми
        # он всё равно не сможет воспользоваться (все они защищены гейтом).
        text, markup = _subscribe_prompt()
        await update.message.reply_text(text, reply_markup=markup)
    else:
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔍 Показать пример уведомления", callback_data="show_example")]]
        )
        await update.message.reply_text(
            "Привет! Присылаю саммари новых законов по твоим фильтрам.\n"
            "Команды:\n"
            "/latest — последние ФКЗ/ФЗ за 30 дней\n"
            "/settings — настройка фильтров\n"
            "/summary <id> — принудительно саммари\n"
            "/help — справка",
            reply_markup=markup,
        )


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик /settings: показывает текущие фильтры и вход в визард."""
    if update.message is None or update.effective_user is None:
        return
    telegram_id = update.effective_user.id
    session_maker = _session_maker(context)
    user = await _get_or_create_user(session_maker, telegram_id, update)
    if user is None:
        return
    levels = await _get_user_levels(session_maker, user.id)
    if not levels:
        cur = "Все уровни"
    else:
        cur = ", ".join(LEVEL_LABELS[l] for l in sorted(levels))
    text = f"Текущий фильтр по силе: {cur}\nНажмите чтобы изменить:"
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Изменить фильтр по силе", callback_data="wizard:setup")]]
    )
    await update.message.reply_text(text=text, reply_markup=markup)


async def _send_example_notification(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Шлёт тестовое уведомление — один из последних FZ, как в проде.

    Переиспользуется и обычной кнопкой «Показать пример», и онбордингом
    нового пользователя («Как это работает?»).
    """
    session_maker = _session_maker(context)
    # последний принятый ФЗ (сначала по level, затем по заголовку для старых с UNKNOWN)
    since = _window_start(days=30)
    async with session_maker() as session:
        article = await session.scalar(
            select(Article)
            .where(Article.level == "FZ")
            .where(Article.published_at >= since)
            .order_by(Article.published_at.desc())
            .limit(1)
        )
        if article is None:
            article = await session.scalar(
                select(Article).where(Article.level == "FZ").order_by(Article.id.desc()).limit(1)
            )
        if article is None:
            # fallback для старых записей с level=UNKNOWN но заголовок "Федеральный закон"
            article = await session.scalar(
                select(Article)
                .where(Article.title.ilike("%Федеральный закон%"))
                .where(Article.published_at >= since)
                .order_by(Article.published_at.desc())
                .limit(1)
            )
        if article is None:
            article = await session.scalar(
                select(Article).where(Article.title.ilike("%Федеральный закон%")).order_by(Article.id.desc()).limit(1)
            )
        if article is None:
            # Последний фолбэк: заранее подготовленная демо-статья (реальные
            # законы всегда в приоритете — см. app/services/demo_article.py)
            article = await session.scalar(
                select(Article).where(Article.is_demo == True).limit(1)  # noqa: E712
            )
    if article is None:
        await query.message.reply_text("Пока нет законов для примера. Попробуйте позже: /latest")
        return
    text, reply_markup = _build_notification(article, context.bot_data["config"].WEBAPP_URL)
    # шлём как тестовое уведомление в тот же чат
    try:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode="MarkdownV2",
        )
    except Exception:
        logger.exception("Не удалось отправить пример уведомления")
        await query.message.reply_text("Не удалось показать пример. Попробуйте /latest")


async def show_example(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback-обработчик кнопки «Показать пример уведомления»."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    await _send_example_notification(query, context)


def _subscribe_prompt() -> tuple[str, InlineKeyboardMarkup]:
    """Текст и кнопка «Проверить подписку» — единая точка правды.

    Используется и онбордингом («Как это работает?»), и гейтом подписки
    (см. ``_require_channel_verified``), чтобы формулировка не расходилась.
    """
    text = (
        "Чтобы получать такие уведомления регулярно, подпишись на канал "
        "@hellolawyer_jobs и нажми «Проверить подписку»."
    )
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Проверить подписку", callback_data="check_subscription")]]
    )
    return text, markup


async def onboarding_show_example(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Онбординг нового пользователя: «Как это работает?» → тестовое уведомление → гейт подписки."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    await _send_example_notification(query, context)
    text, markup = _subscribe_prompt()
    await query.message.reply_text(text, reply_markup=markup)


async def check_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback-обработчик кнопки «Проверить подписку» на обязательный канал."""
    query = update.callback_query
    if query is None or update.effective_user is None:
        return

    config = context.bot_data["config"]
    telegram_id = update.effective_user.id

    subscribed = await is_subscribed(context.bot, telegram_id, config.REQUIRED_CHANNEL_ID)

    if not subscribed:
        await query.answer("Подписка не найдена")
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Проверить подписку", callback_data="check_subscription")]]
        )
        await query.edit_message_text(
            "Подписка на канал не найдена. Подпишитесь и нажмите «Проверить подписку» ещё раз.",
            reply_markup=markup,
        )
        return

    await query.answer()
    session_maker = _session_maker(context)
    async with session_maker() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is not None:
            user.channel_verified = True
            await session.commit()

    markup = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Настроить", callback_data="wizard:setup")],
            [InlineKeyboardButton("Пропустить → Все", callback_data="wizard:skip")],
        ]
    )
    await query.edit_message_text("Подписка подтверждена! Настроим фильтры:", reply_markup=markup)


def require_channel_verified(handler):
    """Оборачивает обработчик проверкой ``channel_verified`` перед вызовом.

    Default-deny: применяется при регистрации ко всем обработчикам в
    ``app/main.py``, кроме явного allowlist'а (онбординг). Если
    ``REQUIRED_CHANNEL_ID`` не задан — гейт отключён, проверка пропускается.
    Читает закешированный флаг из БД (как ``scheduler.py``), без live-запроса
    к Bot API на каждую команду. Исключений по ролям (включая
    ``ADMIN_CHAT_IDS``) нет.
    """

    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        config = context.bot_data["config"]
        if not config.REQUIRED_CHANNEL_ID:
            return await handler(update, context, *args, **kwargs)

        telegram_id = update.effective_user.id if update.effective_user else None
        if telegram_id is None:
            return await handler(update, context, *args, **kwargs)

        session_maker = _session_maker(context)
        async with session_maker() as session:
            user = await session.scalar(select(User).where(User.telegram_id == telegram_id))

        if user is not None and user.channel_verified:
            return await handler(update, context, *args, **kwargs)

        text, markup = _subscribe_prompt()
        if update.callback_query is not None:
            await update.callback_query.answer()
            await update.callback_query.message.reply_text(text, reply_markup=markup)
        elif update.message is not None:
            await update.message.reply_text(text, reply_markup=markup)
        return None

    wrapped.__name__ = getattr(handler, "__name__", "wrapped")
    return wrapped


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик /help: отправляет справку по командам."""
    if update.message is None:
        return
    await update.message.reply_text(HELP_TEXT)


async def latest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик /latest: последние ФКЗ/ФЗ за 30 дней (без учёта фильтров юзера)."""
    if update.message is None:
        return

    session_maker = _session_maker(context)

    since = _window_start(days=30)
    stmt = (
        select(Article)
        .where(Article.published_at >= since)
        .where(Article.level.in_(["FKZ", "FZ"]))
        .where(Article.is_demo.is_(False))
        .order_by(Article.published_at.desc())
        .limit(10)
    )

    async with session_maker() as session:
        articles = (await session.scalars(stmt)).all()

    if not articles:
        await update.message.reply_text("Пока нет федеральных законов за последние 30 дней")
        return

    header_title = escape_markdown("Федеральные законы за последние 30 дней", version=2)
    lines: list[str] = [f"*{header_title}*"]
    for i, article in enumerate(articles, 1):
        header = _format_summary(
            (article.summary or "")[:200],
            f"{i}. {article.title}",
        )
        block = f"{header}\n\n{_full_text_block(article.url)}"
        lines.append(block)

    await update.message.reply_text(
        "\n\n".join(lines), parse_mode="MarkdownV2"
    )


async def _get_or_create_user(
    session_maker, telegram_id: int, update: Update
) -> User | None:
    """Возвращает пользователя по telegram_id, регистрируя его при отсутствии."""
    async with session_maker() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            username = update.effective_user.username if update.effective_user else None
            user = User(telegram_id=telegram_id, username=username)
            session.add(user)
            await session.commit()
            await session.refresh(user)
    return user


async def _deliver(
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
    message: object | None,
    text: str,
    status_message: object | None = None,
    parse_mode: str | None = None,
    reply_markup=None,
) -> None:
    """Шлёт текст результата: для кнопки — правит исходное сообщение, иначе — новое.

    Если задан status_message (временный индикатор «Саммари в процессе создания…»),
    он удаляется перед отправкой результата.
    """
    if message is not None:
        await context.bot.edit_message_text(
            chat_id=message.chat_id,
            message_id=message.message_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )
    else:
        if status_message is not None:
            try:
                await status_message.delete()
            except Exception:
                logger.exception("Не удалось удалить индикатор саммаризации")
        await context.bot.send_message(
            chat_id=user.telegram_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )


async def _summarize_and_reply(
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
    external_id: str,
    message: object | None = None,
    status_message: object | None = None,
) -> None:
    """
    Выполняет принудительную саммаризацию и доставляет результат пользователю.

    Проверяет месячный лимит, получает текст (при отсутствии — OCR через
    GigaChat), формирует саммари, обновляет Article и инкрементирует счётчик.
    При лимите/сбое отправляет пользователю понятное сообщение.

    Args:
        message: если задано (кнопка — исходное сообщение с кнопкой), результат
            правится в нём (кнопка исчезает); иначе шлётся новое сообщение.
        status_message: временный индикатор «Саммари в процессе создания…»,
            удаляется после доставки результата (используется для команды).
    """
    config = context.bot_data["config"]
    result = await run_force_summarize(config, _session_maker(context), user, external_id)

    if result.status == "ok":
        final, reply_markup = _summary_final(
            config, result.title, result.url, result.summary, external_id,
            title_known=result.title_known,
        )
        await _deliver(
            context,
            user,
            message,
            final,
            status_message=status_message,
            parse_mode="MarkdownV2",
            reply_markup=reply_markup,
        )
    elif result.status == "limit":
        limit = config.FORCE_SUMMARIZE_MONTHLY_LIMIT
        fallback = (
            f"Месячный лимит принудительных саммаризаций ({limit}) исчерпан. "
            "Попробуйте в следующем месяце."
        )
        await _deliver_summary_failure(
            context, user, message, status_message,
            config, result.title_known, result.title, result.url, external_id, fallback,
        )
    elif result.status == "no_text":
        fallback = (
            "Не удалось получить текст закона для саммаризации 😢\n"
            f"Попробуйте позже: /summary {external_id}"
        )
        await _deliver_summary_failure(
            context, user, message, status_message,
            config, result.title_known, result.title, result.url, external_id, fallback,
        )
    elif result.text:
        # текст получен, но саммари нет (отказ модели или её сбой) — фолбэк с кнопкой «Полный текст»
        if result.status == "refused":
            fallback = (
                "Языковая модель отказалась сформировать саммари для этого документа. "
                "Распознанный текст доступен по кнопке «Полный текст»."
            )
        else:
            fallback = "Не удалось сделать саммари. Попробуйте позже."
        final, reply_markup = _summary_refused(
            config, result.title, result.url, fallback, external_id,
            title_known=result.title_known,
        )
        await _deliver(
            context,
            user,
            message,
            final,
            status_message=status_message,
            parse_mode="MarkdownV2",
            reply_markup=reply_markup,
        )
    else:
        await _deliver_summary_failure(
            context, user, message, status_message,
            config, result.title_known, result.title, result.url, external_id,
            "Не удалось сделать саммари. Попробуйте позже.",
        )


async def force_summarize(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback-обработчик кнопки «Сделать саммари»."""
    query = update.callback_query
    if query is None or update.effective_user is None:
        return
    await query.answer()

    session_maker = _session_maker(context)
    telegram_id = update.effective_user.id

    data = query.data or ""
    if not data.startswith(FORCE_SUMMARIZE_PREFIX):
        await query.message.reply_text("Неизвестная команда кнопки")
        return
    external_id = data[len(FORCE_SUMMARIZE_PREFIX):].strip()
    if not external_id:
        await query.message.reply_text("Не удалось определить документ")
        return

    user = await _get_or_create_user(session_maker, telegram_id, update)
    if user is None:
        return

    if query.message is not None:
        await query.message.edit_text(
            "⏳ Саммари в процессе создания…", reply_markup=None
        )
    await _summarize_and_reply(context, user, external_id, message=query.message)


async def force_summarize_digest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback-обработчик кнопки «Сделать саммари №N» внутри тизера дайджеста.

    Кнопка есть только в ранее отправленных дайджестах: новые тизеры её не
    содержат (саммари делается в Mini-App, см. _build_digest); обработчик нужен,
    чтобы кнопки в старых сообщениях продолжали работать.

    В отличие от force_summarize, НЕ правит исходное сообщение (там ещё 1-2
    других пункта дайджеста) — результат уходит отдельным новым сообщением,
    как при команде /summary.
    """
    query = update.callback_query
    if query is None or update.effective_user is None:
        return
    await query.answer()

    session_maker = _session_maker(context)
    telegram_id = update.effective_user.id

    data = query.data or ""
    if not data.startswith(DIGEST_SUMMARIZE_PREFIX):
        return
    external_id = data[len(DIGEST_SUMMARIZE_PREFIX):].strip()
    if not external_id:
        return

    user = await _get_or_create_user(session_maker, telegram_id, update)
    if user is None:
        return

    status_message = None
    if query.message is not None:
        status_message = await query.message.reply_text("⏳ Саммари в процессе создания…")
    await _summarize_and_reply(context, user, external_id, status_message=status_message)


async def test_digest_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отладочная команда /test_digest real <N>: строит тизер дайджеста из N реальных
    статей БД (`_build_digest`, тот же код, что и в проде) и присылает его только
    вызвавшему администратору — НЕ запускает `_notify_users_batch` и не рассылает
    остальным подписчикам.
    """
    if update.message is None or update.effective_user is None:
        return
    config = context.bot_data["config"]
    telegram_id = update.effective_user.id
    if telegram_id not in getattr(config, "ADMIN_CHAT_IDS", ()):
        await update.message.reply_text("Команда доступна только администраторам.")
        return

    args = context.args or []
    if len(args) < 2 or args[0] != "real":
        await update.message.reply_text(
            "Использование: /test_digest real <N>\nНапример: /test_digest real 4"
        )
        return
    try:
        count = int(args[1])
    except ValueError:
        await update.message.reply_text("N должно быть числом. Например: /test_digest real 4")
        return
    if count < 1:
        await update.message.reply_text("N должно быть не меньше 1.")
        return

    session_maker = _session_maker(context)
    async with session_maker() as session:
        # предпочитаем не важные уровни — именно они уходят в дайджест (2+) в проде
        primary = list(
            (
                await session.scalars(
                    select(Article)
                    .where(Article.level.notin_(IMPORTANT_LEVELS))
                    .order_by(Article.id.desc())
                    .limit(count)
                )
            ).all()
        )
        if len(primary) < count:
            exclude_ids = [a.id for a in primary]
            extra = list(
                (
                    await session.scalars(
                        select(Article)
                        .where(Article.id.notin_(exclude_ids))
                        .order_by(Article.id.desc())
                        .limit(count - len(primary))
                    )
                ).all()
            )
            primary += extra
        articles = primary

    if not articles:
        await update.message.reply_text("В БД нет ни одной статьи для теста.")
        return

    text, reply_markup = _build_digest(articles, config.WEBAPP_URL)
    await update.message.reply_text(text, parse_mode="MarkdownV2", reply_markup=reply_markup)
    await update.message.reply_text(
        "[test_digest] Использовано "
        f"{len(articles)} реальных статей: " + ", ".join(a.external_id for a in articles)
    )


def _resolve_external_id(value: str) -> str | None:
    """Извлекает external_id из голого id или из ссылки правового портала."""
    value = value.strip()
    if not value:
        return None
    if value.startswith("http://") or value.startswith("https://"):
        path = value.split("?")[0].split("#")[0].rstrip("/")
        last = path.rsplit("/", 1)[-1]
        return last if last else None
    if "/" in value or value.lower().startswith("http"):
        return None
    return value


async def summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик /summary <id или ссылка>: принудительная саммаризация закона."""
    if update.message is None or update.effective_user is None:
        return
    session_maker = _session_maker(context)
    telegram_id = update.effective_user.id

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Использование: /summary <external_id или ссылка>\n"
            "Например: /summary 0001202608060001\n"
            "или /summary http://publication.pravo.gov.ru/document/0001202608060001"
        )
        return
    external_id = _resolve_external_id(args[0])
    if external_id is None:
        await update.message.reply_text(
            "Не удалось определить external_id. Укажите id или ссылку правового портала."
        )
        return

    user = await _get_or_create_user(session_maker, telegram_id, update)
    if user is None:
        return

    status_message = None
    if update.message is not None:
        status_message = await update.message.reply_text("⏳ Саммари в процессе создания…")
    await _summarize_and_reply(
        context, user, external_id, status_message=status_message
    )


# ── ConversationHandler для визарда «Сила» ───────────────────────────────
# Гейт подписки применяется только на entry_points (сам /settings и запуск
# визарда сразу после онбординга) — состояния диалога (level_choice в states)
# остаются без обёртки, иначе пользователь, уже прошедший гейт и начавший
# диалог, будет неожиданно заблокирован посреди шага (design.md — Risks).
level_wizard_handler = ConversationHandler(
    entry_points=[
        CommandHandler("settings", require_channel_verified(level_wizard_entry)),
        CallbackQueryHandler(require_channel_verified(level_choice), pattern=r"^wizard:(setup|skip)$"),
    ],
    states={
        LEVEL_STATE: [
            CallbackQueryHandler(level_choice, pattern=r"^(level:|levels:|wizard:)"),
        ],
    },
    fallbacks=[
        CommandHandler("cancel", level_cancel),
        CallbackQueryHandler(level_cancel, pattern=r"^level:cancel$"),
    ],
    per_user=True,
    per_chat=True,
    per_message=False,
    name="level_wizard",
    persistent=False,
)
