"""
Telegram-обработчики команд бота.
"""

import logging
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import desc, select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.helpers import escape_markdown
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, ConversationHandler

from app.models import Article, SummarizationUsage, User
from app.services.gigachat import (
    GigaChatAPIError,
    GigaChatAuthError,
    GigaChatClient,
    GigaChatConfig,
)
from app.services.rss_parser import _normalize_text, get_legal_text, ocr_document_text
from app.services.scheduler import FORCE_SUMMARIZE_PREFIX, _build_notification
from app.services.summarizer import Summarizer, SummarizerConfig

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
DEFAULT_LEVELS: set[str] = {"CONSTITUTION", "FKZ", "FZ"}
AUTO_LEVELS: set[str] = {"CONSTITUTION", "FKZ", "FZ"}
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
    rows.append([InlineKeyboardButton("Выбрать всё", callback_data="levels:all")])
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


def _summary_final(
    config, title: str, url: str, summary: str | None, external_id: str
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Собирает итог саммаризации: MarkdownV2-текст + кнопку Mini-App (если есть).

    Кнопка «Полный текст» показывается только когда есть саммари (важный закон — сразу,
    иначе после принудительной саммаризации).
    """
    header = _format_summary(summary, title)
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

    is_new = False
    async with _session_maker(context)() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            from app.models.user_filter import UserFilter

            user = User(telegram_id=telegram_id, username=username)
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
            [
                [InlineKeyboardButton("Настроить", callback_data="wizard:setup")],
                [InlineKeyboardButton("Пропустить → Все", callback_data="wizard:skip")],
                [InlineKeyboardButton("🔍 Показать пример уведомления", callback_data="show_example")],
            ]
        )
        await update.message.reply_text(
            "Привет! Присылаю саммари новых законов по твоим фильтрам.\n"
            "Например, юристу по крипте — оставь только ФЗ, остальное сними.\n"
            "Начни с /settings — выбери силу, или жми /latest — последние ФКЗ/ФЗ за 30 дней.",
            reply_markup=markup,
        )
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


async def show_example(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает тестовое уведомление — один из последних FZ, как в проде."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()
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
    session_maker = _session_maker(context)

    is_admin = user.telegram_id in getattr(config, "ADMIN_CHAT_IDS", ())
    month = datetime.now().strftime("%Y-%m")
    limit = config.FORCE_SUMMARIZE_MONTHLY_LIMIT

    # авто-уровни без лимита (Конституция/ФКЗ/ФЗ) — проверяем по уже сохранённой статье
    auto_bypass = False
    try:
        async with session_maker() as session:
            lvl = await session.scalar(select(Article.level).where(Article.external_id == external_id))
            if lvl in AUTO_LEVELS:
                auto_bypass = True
    except Exception:
        pass

    if not is_admin and not auto_bypass:
        async with session_maker() as session:
            usage = await session.scalar(
                select(SummarizationUsage).where(
                    SummarizationUsage.user_id == user.id,
                    SummarizationUsage.month == month,
                )
            )
            if usage is not None and usage.count >= limit:
                await _deliver(
                    context,
                    user,
                    message,
                    (
                        f"Месячный лимит принудительных саммаризаций ({limit}) исчерпан. "
                        "Попробуйте в следующем месяце."
                    ),
                    status_message=status_message,
                )
                return

    # Переиспользуем уже сохранённое саммари, если оно есть (без повторного LLM).
    async with session_maker() as session:
        cached = await session.scalar(
            select(Article).where(Article.external_id == external_id)
        )
    if cached is not None and cached.summary:
        final, reply_markup = _summary_final(
            config, cached.title or external_id, cached.url, cached.summary, external_id
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
        return

    # TODO: Рассмотреть платный тариф — бесконечная принудительная саммаризация

    try:
        text = await get_legal_text(external_id)
        if not text:
            logger.info(
                "Текст для %s отсутствует — используем OCR через GigaChat",
                external_id,
            )
            async with GigaChatClient(
                GigaChatConfig(
                    auth_key=config.GIGACHAT_AUTH_KEY,
                    verify_ssl=config.GIGACHAT_VERIFY_SSL,
                )
            ) as ocr_client:
                text = await ocr_document_text(external_id, client=ocr_client)

        if not text:
            await _deliver(
                context,
                user,
                message,
                "Не удалось получить текст закона для саммаризации.",
                status_message=status_message,
            )
            return

        async with Summarizer(
            SummarizerConfig(
                auth_key=config.GIGACHAT_AUTH_KEY,
                model=config.GIGACHAT_MODEL,
                min_len=config.SUMMARY_MIN_LEN,
                max_len=config.SUMMARY_MAX_LEN,
                verify_ssl=config.GIGACHAT_VERIFY_SSL,
            )
        ) as summarizer:
            summary = await summarizer.summarize(text)

        async with session_maker() as session:
            article = await session.scalar(
                select(Article).where(Article.external_id == external_id)
            )
            if article is not None:
                article.original_text = text
                article.summary = summary
                await session.commit()
                await session.refresh(article)
                title = article.title
                url = article.url
            else:
                title = external_id
                url = f"http://publication.pravo.gov.ru/document/{external_id}"

            if not is_admin and not auto_bypass:
                usage = await session.scalar(
                    select(SummarizationUsage).where(
                        SummarizationUsage.user_id == user.id,
                        SummarizationUsage.month == month,
                    )
                )
                if usage is None:
                    usage = SummarizationUsage(user_id=user.id, month=month, count=0)
                    session.add(usage)
                usage.count += 1
                await session.commit()
            else:
                await session.commit()

        final, reply_markup = _summary_final(config, title, url, summary, external_id)
        await _deliver(
            context,
            user,
            message,
            final,
            status_message=status_message,
            parse_mode="MarkdownV2",
            reply_markup=reply_markup,
        )
    except Exception:
        logger.exception("Ошибка принудительной саммаризации %s", external_id)
        await _deliver(
            context,
            user,
            message,
            "Не удалось сделать саммари. Попробуйте позже.",
            status_message=status_message,
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
level_wizard_handler = ConversationHandler(
    entry_points=[
        CommandHandler("settings", level_wizard_entry),
        CallbackQueryHandler(level_choice, pattern=r"^wizard:(setup|skip)$"),
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
