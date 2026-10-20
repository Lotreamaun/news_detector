"""
Telegram-обработчики команд бота.
"""

import asyncio
import logging
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import desc, select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Update, WebAppInfo
from telegram.error import BadRequest
from telegram.helpers import escape_markdown
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, ConversationHandler

from app.models import Article, User
from app.services.channel_subscription import is_subscribed
from app.services.events import (
    BUTTON,
    CMD,
    SETTINGS_CHANGED,
    USER_BLOCKED,
    USER_REGISTERED,
    USER_UNBLOCKED,
    USER_VERIFIED,
    record_event,
)
from app.services.force_summary import ForceSummaryResult
from app.services.force_summary import force_summarize as run_force_summarize
from app.services.gigachat import calls_ahead_of_user
from app.services.rss_parser import IMPORTANT_LEVELS, _normalize_text
from app.services.summary_usage import format_reset_date, get_summary_usage, limit_message
from app.services.today import FEDERAL, OTHER, REGIONAL, day_bounds, pick_top, portal_today, scope_of
from app.services.weekly_review import moderation_unavailable_reason, panel_unavailable_reason
from app.webapp.admin import issue_login_token, login_url
from app.services.scheduler import (
    DIGEST_SUMMARIZE_PREFIX,
    FORCE_SUMMARIZE_PREFIX,
    _build_digest,
    _build_notification,
    build_admin_summary_now,
    build_review_ping_now,
    build_weekly_preview,
)

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "Доступные команды:\n"
    "/start — регистрация и настройка фильтров\n"
    "/today — законы, опубликованные сегодня\n"
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


# Команды бота для журнала событий: остальное пишется как ``other``, чтобы произвольный текст
# пользователя не попадал в БД. Добавили команду — дополните список.
BOT_COMMANDS = frozenset({"start", "today", "summary", "settings", "cancel", "help", "admin", "test_digest"})


def _session_maker(context: ContextTypes.DEFAULT_TYPE):
    """Достает фабрику сессий, положенную в bot_data при старте приложения."""
    return context.bot_data["session_maker"]


async def track_activity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Пишет в журнал команду (``cmd``) или нажатие кнопки (``button``); остальные апдейты игнорирует.

    Регистрируется в группе -1 и не бросает ``ApplicationHandlerStop``, поэтому обычные
    обработчики работают как раньше. Действие считается, даже если его отклонит гейт канала.
    """
    user = update.effective_user
    if user is None:
        return
    message = update.message
    if message is not None and message.text and message.text.startswith("/"):
        name = message.text.split(maxsplit=1)[0][1:].split("@", 1)[0].lower()
        record_event(_session_maker(context), CMD, user.id, name if name in BOT_COMMANDS else "other")
    elif update.callback_query is not None:
        # первые два сегмента callback_data: вид кнопки без идентификаторов документов
        kind = ":".join((update.callback_query.data or "").split(":")[:2])
        record_event(_session_maker(context), BUTTON, user.id, kind or None)


async def track_bot_blocked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Блокировка/разблокировка бота пользователем по ``my_chat_member`` (только личные чаты).

    Время берётся из апдейта. ``is_active`` не трогаем: его снятие по этому событию — отдельная
    задача (issue #23).
    """
    change = update.my_chat_member
    if change is None or change.chat.type != "private":
        return
    old_status, new_status = change.old_chat_member.status, change.new_chat_member.status
    if new_status == "kicked":
        kind = USER_BLOCKED
    elif old_status == "kicked" and new_status == "member":
        kind = USER_UNBLOCKED
    else:
        return
    record_event(_session_maker(context), kind, change.chat.id, at=change.date)


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


async def _usage_line(context: ContextTypes.DEFAULT_TYPE, user: User) -> str | None:
    """Строка со счётчиком попыток саммари для /settings; при сбое чтения — None."""
    try:
        usage = await get_summary_usage(
            context.bot_data["config"], _session_maker(context), user
        )
    except Exception:
        logger.exception("Не удалось получить счётчик саммари для /settings")
        return None
    when = format_reset_date(usage.resets_on)
    if usage.unlimited:
        return "Саммари по запросу: без ограничений."
    if usage.used >= usage.limit:
        return (
            f"Саммари по запросу: {usage.used}/{usage.limit} — попытки закончились. "
            f"Новые появятся {when}."
        )
    return f"Саммари по запросу: {usage.used}/{usage.limit} в этом месяце. Обновится {when}."


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
    usage_line = await _usage_line(context, user)
    if usage_line:
        text += f"\n\n{usage_line}"
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
        record_event(_session_maker(context), SETTINGS_CHANGED, update.effective_user.id)
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
        record_event(_session_maker(context), SETTINGS_CHANGED, update.effective_user.id)
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
    bot,
    user: User,
    status_message: object | None,
    config,
    title_known: bool,
    title: str,
    url: str,
    external_id: str,
    fallback: str,
) -> None:
    """Доставляет новым сообщением итог неудачной принудительной саммаризации.

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
            bot,
            user,
            final,
            status_message=status_message,
            parse_mode="MarkdownV2",
            reply_markup=reply_markup,
        )
    else:
        final = _summary_refused_plain(fallback, external_id, url)
        await _deliver(bot, user, final, status_message=status_message)


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
            record_event(_session_maker(context), USER_REGISTERED, telegram_id)
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
            "/today — законы, опубликованные сегодня\n"
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
        await query.message.reply_text("Пока нет законов для примера. Попробуйте позже")
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
        await query.message.reply_text("Не удалось показать пример. Попробуйте позже")


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


async def _edit_ignoring_not_modified(
    query, text: str, reply_markup: InlineKeyboardMarkup
) -> None:
    """``edit_message_text``, не падающий на «Message is not modified» (повторное нажатие)."""
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
        logger.debug("Сообщение не изменилось, правка пропущена: %s", exc)


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
        await _edit_ignoring_not_modified(
            query,
            "Подписка на канал не найдена. Подпишитесь и нажмите «Проверить подписку» ещё раз.",
            markup,
        )
        return

    await query.answer()
    session_maker = _session_maker(context)
    async with session_maker() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is not None:
            first_time = not user.channel_verified
            user.channel_verified = True
            await session.commit()
            if first_time:
                record_event(session_maker, USER_VERIFIED, telegram_id)

    markup = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Настроить", callback_data="wizard:setup")],
            [InlineKeyboardButton("Пропустить → Все", callback_data="wizard:skip")],
        ]
    )
    await _edit_ignoring_not_modified(query, "Подписка подтверждена! Настроим фильтры:", markup)


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


def _build_today_message(
    articles: list, webapp_url: str | None = None
) -> tuple[str, InlineKeyboardMarkup | None]:
    """MarkdownV2-тизер ``/today``: число актов дня, счётчики групп, до 3 самых важных и кнопка Mini-App.

    ``articles`` — все акты дня (нужны для счётчиков); показываются первые три по
    порядку важности (``pick_top``). Блок акта оформлен как в ``_build_digest``. Кнопка
    «Все за сегодня» открывает страницу дня без даты: её определяет сервер. Без
    ``webapp_url`` нет ни кнопки, ни подсказки.
    """
    counts = {FEDERAL: 0, REGIONAL: 0, OTHER: 0}
    for article in articles:
        counts[scope_of(article.external_id)] += 1
    stats = f"Федеральные — {counts[FEDERAL]} · Региональные — {counts[REGIONAL]}"
    if counts[OTHER]:
        stats += f" · Прочие — {counts[OTHER]}"

    lines: list[str] = [
        f"*{escape_markdown(f'Опубликовано сегодня — {len(articles)}', version=2)}*",
        escape_markdown(stats, version=2),
    ]
    for i, article in enumerate(pick_top(articles), 1):
        title_clean = _normalize_text(article.title) or article.title
        title_esc = escape_markdown(f"{i}. {title_clean}", version=2)
        url_esc = escape_markdown(article.url, version=2)
        if article.summary:
            summary_clean = _normalize_text(article.summary) or article.summary
            summary_esc = escape_markdown(summary_clean, version=2)
            lines.append(f"*{title_esc}*\n{summary_esc}\n[Читать на портале]({url_esc})")
        else:
            lines.append(f"*{title_esc}*\n[Читать на портале]({url_esc})")

    markup = None
    if webapp_url:
        lines.append(escape_markdown("Все акты дня и поиск по заголовку — по кнопке ниже", version=2))
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "📖 Все за сегодня",
                        web_app=WebAppInfo(url=f"{webapp_url.rstrip('/')}/app?view=today"),
                    )
                ]
            ]
        )
    return "\n\n".join(lines), markup


async def today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик /today: все акты, опубликованные сегодня (по Москве), без учёта фильтров юзера."""
    if update.message is None:
        return

    start, end = day_bounds(portal_today())
    stmt = (
        select(
            Article.external_id,
            Article.title,
            Article.summary,
            Article.url,
            Article.level,
            Article.importance,
        )
        .where(Article.published_at >= start, Article.published_at < end)
        .where(Article.is_demo.is_(False))
    )
    async with _session_maker(context)() as session:
        articles = (await session.execute(stmt)).all()

    if not articles:
        await update.message.reply_text("Сегодня пока ничего не опубликовано")
        return

    text, markup = _build_today_message(articles, context.bot_data["config"].WEBAPP_URL)
    await update.message.reply_text(text, reply_markup=markup, parse_mode="MarkdownV2")


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
    bot,
    user: User,
    text: str,
    status_message: object | None = None,
    parse_mode: str | None = None,
    reply_markup=None,
) -> None:
    """Шлёт результат новым сообщением (с push); статус-сообщение перед этим удаляется.

    Ошибки отправки не ловит — их обрабатывает обёртка фоновой задачи (`_run_summary_task`
    в боте, `_deliver_later` в Mini-App).
    """
    if status_message is not None:
        try:
            await status_message.delete()
        except Exception:
            logger.exception("Не удалось удалить индикатор саммаризации")
    await bot.send_message(
        chat_id=user.telegram_id,
        text=text,
        parse_mode=parse_mode,
        reply_markup=reply_markup,
    )


# Фоновые задачи саммари: сильные ссылки, иначе GC может собрать задачу до завершения
_summary_tasks: set[asyncio.Task] = set()

_STATUS_PENDING = "⏳ Готовлю саммари — пришлю отдельным сообщением."
_STATUS_TEXT = "⏳ Готовлю саммари — обычно до полуминуты. Пришлю отдельным сообщением."
_STATUS_OCR = "⏳ Распознаю текст закона — обычно 1-3 минуты. Пришлю отдельным сообщением."
_STATUS_BUSY = "Сейчас модель занята другими запросами — может занять больше времени."


def _spawn_summary_task(coro) -> asyncio.Task:
    """Запускает фоновую корутину, удерживая ссылку на задачу до её завершения."""
    task = asyncio.create_task(coro)
    _summary_tasks.add(task)
    task.add_done_callback(_summary_tasks.discard)
    return task


def _estimate_text(mode: str, busy: bool) -> str:
    """Текст статуса с оценкой времени по режиму (``text`` / ``ocr``); число запросов не называем."""
    text = _STATUS_OCR if mode == "ocr" else _STATUS_TEXT
    return f"{text}\n\n{_STATUS_BUSY}" if busy else text


async def _edit_status(status_message: object, text: str) -> None:
    """Правит статус-сообщение; сбой (сообщение удалено, бот заблокирован) только в лог."""
    try:
        await status_message.edit_text(text)
    except Exception:
        logger.warning("Не удалось обновить статус саммаризации", exc_info=True)


async def send_summary_crash(
    bot,
    user: User,
    external_id: str,
    status_message: object | None = None,
) -> None:
    """Последняя попытка доставки при сбое: статус удаляется, приходит простой текст о неудаче.

    Общая для бота и Mini-App. Ошибки отправки не ловит — это делает вызывающий.
    """
    await _deliver(
        bot,
        user,
        f"Не удалось сделать саммари. Попробуйте позже: /summary {external_id}",
        status_message=status_message,
    )


async def _run_summary_task(
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
    external_id: str,
    status_message: object | None,
) -> None:
    """Обёртка фоновой задачи: любой сбой — в лог и одна попытка последней доставки.

    Статус «⏳» не должен оставаться единственным ответом: удаляем его и шлём простой
    текст о неудаче. Ошибка этой попытки только логируется.
    """
    try:
        await _summarize_and_reply(context, user, external_id, status_message)
    except Exception:
        logger.exception("Сбой саммаризации %s для user=%s", external_id, user.telegram_id)
        try:
            await send_summary_crash(context.bot, user, external_id, status_message)
        except Exception:
            logger.exception("Не удалось доставить сообщение о сбое саммари %s", external_id)


async def _summarize_and_reply(
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
    external_id: str,
    status_message: object | None = None,
) -> None:
    """
    Выполняет принудительную саммаризацию и доставляет результат пользователю.

    Проверяет месячный лимит, получает текст (при отсутствии — OCR через
    GigaChat), формирует саммари, обновляет Article и инкрементирует счётчик.
    Итог — саммари или понятное сообщение о неудаче — всегда приходит новым
    сообщением; статус-сообщение удаляется.

    Args:
        status_message: временный индикатор «⏳ Готовлю саммари…». Как только ядро
            узнаёт режим (текст / OCR), в нём появляется оценка времени; перед
            результатом оно удаляется.
    """
    config = context.bot_data["config"]

    def on_mode(mode: str) -> None:
        # синхронно: замер очереди должен произойти до того, как запрос встанет в неё
        if status_message is not None:
            busy = calls_ahead_of_user() > 0
            _spawn_summary_task(_edit_status(status_message, _estimate_text(mode, busy)))

    result = await run_force_summarize(
        config, _session_maker(context), user, external_id, on_mode=on_mode
    )
    await send_summary_result(
        context.bot, config, _session_maker(context), user, external_id, result, status_message
    )


async def send_summary_result(
    bot,
    config,
    session_maker,
    user: User,
    external_id: str,
    result: ForceSummaryResult,
    status_message: object | None = None,
) -> None:
    """Форматирует итог принудительной саммаризации и доставляет его новым сообщением.

    Общая для бота и Mini-App: один вид сообщения везде. Статус-сообщение (если есть)
    перед отправкой удаляется. Ошибки отправки не ловит — это делает вызывающий.
    """
    if result.status == "ok":
        final, reply_markup = _summary_final(
            config, result.title, result.url, result.summary, external_id,
            title_known=result.title_known,
        )
        await _deliver(
            bot,
            user,
            final,
            status_message=status_message,
            parse_mode="MarkdownV2",
            reply_markup=reply_markup,
        )
    elif result.status == "limit":
        usage = await get_summary_usage(config, session_maker, user)
        fallback = limit_message(usage)
        await _deliver_summary_failure(
            bot, user, status_message,
            config, result.title_known, result.title, result.url, external_id, fallback,
        )
    elif result.status == "no_text":
        fallback = (
            "😢 Не удалось получить текст закона для саммаризации\n\n"
            f"Попробуйте позже: /summary {external_id}"
        )
        await _deliver_summary_failure(
            bot, user, status_message,
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
            bot,
            user,
            final,
            status_message=status_message,
            parse_mode="MarkdownV2",
            reply_markup=reply_markup,
        )
    else:
        await _deliver_summary_failure(
            bot, user, status_message,
            config, result.title_known, result.title, result.url, external_id,
            "Не удалось сделать саммари. Попробуйте позже.",
        )


async def force_summarize(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback-обработчик кнопки «Сделать саммари» под уведомлением.

    Само уведомление превращается в статус (текст и кнопка убираются) и запускает
    саммаризацию фоновой задачей; по готовности статус удаляется, результат приходит
    новым сообщением — рядом с ним старого уведомления не остаётся.
    """
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

    # статусом становится само уведомление; одним вызовом уходят и текст, и кнопка
    status_message = query.message
    if status_message is not None:
        try:
            await status_message.edit_text(_STATUS_PENDING, reply_markup=None)
        except Exception:
            logger.warning("Не удалось превратить уведомление в статус", exc_info=True)
    _spawn_summary_task(_run_summary_task(context, user, external_id, status_message))


async def force_summarize_digest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback-обработчик кнопки «Сделать саммари №N» внутри тизера дайджеста.

    Кнопка есть только в ранее отправленных дайджестах: новые тизеры её не
    содержат (саммари делается в Mini-App, см. _build_digest); обработчик нужен,
    чтобы кнопки в старых сообщениях продолжали работать.

    Исходное сообщение не трогает (там ещё 1-2 других пункта дайджеста): статус
    приходит ответом на него, результат — новым сообщением, как при /summary.
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
        status_message = await query.message.reply_text(_STATUS_PENDING)
    _spawn_summary_task(_run_summary_task(context, user, external_id, status_message))


async def test_digest_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отладочная команда /test_digest real <N>: строит тизер дайджеста из N реальных
    статей БД (`_build_digest`, тот же код, что и в проде) и присылает его только
    вызвавшему администратору — НЕ запускает `_notify_users_batch` и не рассылает
    остальным подписчикам.

    ``/test_digest weekly`` — предпросмотр недельной подборки за текущее окно
    (``build_weekly_preview``): только вызвавшему администратору, ничего не пишет в
    ``weekly_digest_runs`` и не запускает оценку/саммаризацию.
    """
    if update.message is None or update.effective_user is None:
        return
    config = context.bot_data["config"]
    telegram_id = update.effective_user.id
    if telegram_id not in getattr(config, "ADMIN_CHAT_IDS", ()):
        await update.message.reply_text("Команда доступна только администраторам.")
        return

    args = context.args or []
    if args and args[0] == "weekly":
        text, reply_markup, report = await build_weekly_preview(_session_maker(context), config)
        if text:
            await update.message.reply_text(text, parse_mode="MarkdownV2", reply_markup=reply_markup)
        else:
            await update.message.reply_text("[test_digest weekly] Подборка за текущее окно пуста.")
        await update.message.reply_text(report)
        return
    if args and args[0] == "review":
        reason = moderation_unavailable_reason(config)
        if reason:
            await update.message.reply_text(f"[test_digest review] Модерация недоступна: {reason}.")
            return
        text, reply_markup = await build_review_ping_now(_session_maker(context), config)
        await update.message.reply_text(text, reply_markup=reply_markup)  # только вызвавшему
        return
    if len(args) < 2 or args[0] != "real":
        await update.message.reply_text(
            "Использование: /test_digest real <N>\nНапример: /test_digest real 4\n"
            "Или: /test_digest weekly — предпросмотр недельной подборки\n"
            "Или: /test_digest review — служебное напоминание с текущими числами (только вам)"
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
    """Обработчик /summary <id или ссылка>: принудительная саммаризация закона.

    Сразу отвечает статусом и запускает саммаризацию фоновой задачей — бот не ждёт
    GigaChat; результат придёт новым сообщением.
    """
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
        status_message = await update.message.reply_text(_STATUS_PENDING)
    _spawn_summary_task(_run_summary_task(context, user, external_id, status_message))


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
        # повторный /settings при незавершённом диалоге открывает визард заново, а не молчит
        CommandHandler("settings", require_channel_verified(level_wizard_entry)),
        CommandHandler("cancel", level_cancel),
        CallbackQueryHandler(level_cancel, pattern=r"^level:cancel$"),
    ],
    per_user=True,
    per_chat=True,
    per_message=False,
    name="level_wizard",
    persistent=False,
)


_ADMIN_HEADER = "🛠 Служебное сообщение для администратора"


def _admin_link_text(config, telegram_id: int) -> str:
    """Абзац со свежей одноразовой ссылкой входа в панель (для браузера)."""
    url = login_url(config.WEBAPP_URL, issue_login_token(telegram_id))
    return (
        "Ссылка для входа в панель в браузере. "
        "Одноразовая, действует 10 минут — не пересылайте её:\n\n"
        f"{url}"
    )


async def _send_admin_link(context: ContextTypes.DEFAULT_TYPE, chat_id: int, telegram_id: int) -> None:
    """Присылает администратору только свежую одноразовую ссылку входа в панель (для браузера).

    Предпросмотр ссылки отключён: иначе Telegram сам откроет её и сожжёт одноразовый токен.
    """
    config = context.bot_data["config"]
    reason = panel_unavailable_reason(config)
    if reason:
        await context.bot.send_message(chat_id=chat_id, text=f"Панель недоступна: {reason}.")
        return
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"{_ADMIN_HEADER}\n\n{_admin_link_text(config, telegram_id)}",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def _send_admin_panel(context: ContextTypes.DEFAULT_TYPE, chat, telegram_id: int) -> None:
    """Ответ на ``/admin``: пометка, сводка подборки (если модерация доступна), ссылка и кнопка Mini-App.

    Кнопка ``web_app`` только в личном чате: в группах Telegram такие inline-кнопки не принимает.
    Предпросмотр ссылки отключён, иначе Telegram сожжёт одноразовый токен.
    """
    config = context.bot_data["config"]
    reason = panel_unavailable_reason(config)
    if reason:
        await context.bot.send_message(chat_id=chat.id, text=f"Панель недоступна: {reason}.")
        return
    parts = [_ADMIN_HEADER]
    if moderation_unavailable_reason(config) is None:
        try:
            parts.append(await build_admin_summary_now(_session_maker(context), config))
        except Exception:
            logger.exception("Не удалось собрать сводку подборки для /admin")
    parts.append(_admin_link_text(config, telegram_id))
    markup = None
    if chat.type == "private":
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("📋 Открыть панель", web_app=WebAppInfo(url=f"{config.WEBAPP_URL.rstrip('/')}/admin"))]]
        )
    await context.bot.send_message(
        chat_id=chat.id,
        text="\n\n".join(parts),
        reply_markup=markup,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик /admin: администратору — вход в панель (сводка подборки, ссылка для браузера, кнопка Mini-App)."""
    if update.message is None or update.effective_user is None:
        return
    config = context.bot_data["config"]
    if update.effective_user.id not in config.ADMIN_CHAT_IDS:
        await update.message.reply_text("Команда доступна только администраторам.")
        return
    await _send_admin_panel(context, update.message.chat, update.effective_user.id)


async def admin_link_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка «🔗 Ссылка для браузера» в напоминании: то же, что /admin."""
    query = update.callback_query
    if query is None or query.message is None:
        return
    config = context.bot_data["config"]
    if query.from_user.id not in config.ADMIN_CHAT_IDS:
        await query.answer("Доступно только администраторам.", show_alert=True)
        return
    await query.answer()
    await _send_admin_link(context, query.message.chat_id, query.from_user.id)
