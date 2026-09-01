"""
Telegram-обработчики команд бота.
"""

import logging
from datetime import datetime

from sqlalchemy import desc, select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.helpers import escape_markdown
from telegram.ext import ContextTypes

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
    "/start — регистрация пользователя\n"
    "/test — проверка связи с GigaChat API\n"
    "/latest — последние обработанные законы\n"
    "/summary <external_id> — принудительно сделать саммари закона\n"
    "/notify_me <external_id> — отправить тестовое уведомление с кнопкой\n"
    "/help — эта справка"
)


def _session_maker(context: ContextTypes.DEFAULT_TYPE):
    """Достает фабрику сессий, положенную в bot_data при старте приложения."""
    return context.bot_data["session_maker"]


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

    Пользователь считается зарегистрированным один раз — при повторном
    /start запись не дублируется (проверка по уникальному telegram_id).
    """
    if update.effective_user is None or update.message is None:
        return
    telegram_id = update.effective_user.id
    username = update.effective_user.username

    async with _session_maker(context)() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            session.add(User(telegram_id=telegram_id, username=username))
            await session.commit()
            logger.info("Зарегистрирован новый пользователь telegram_id=%s", telegram_id)
        else:
            logger.info("Пользователь telegram_id=%s уже зарегистрирован", telegram_id)

    await update.message.reply_text(
        "Привет! Я буду присылать саммари новых законов с pravo.gov.ru.\n"
        "Сейчас сервис в разработке. Доступные команды:\n"
        "/start — регистрация\n"
        "/test — проверка связи с GigaChat API"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик /help: отправляет справку по командам."""
    if update.message is None:
        return
    await update.message.reply_text(HELP_TEXT)


async def myid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик /myid: показывает telegram chat_id для настройки ADMIN_CHAT_IDS."""
    if update.message is None or update.effective_user is None:
        return
    await update.message.reply_text(f"Твой chat_id: {update.effective_user.id}")


async def latest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик /latest: показывает последние обработанные законы."""
    if update.message is None:
        return

    async with _session_maker(context)() as session:
        articles = (
            await session.scalars(
                select(Article).order_by(Article.id.desc()).limit(10)
            )
        ).all()

    if not articles:
        await update.message.reply_text("Пока нет обработанных законов")
        return

    lines: list[str] = []
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


async def test_gigachat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обработчик /test: smoke-тест подключения к GigaChat API.

    Получает access-токен, список моделей и отправляет короткий запрос.
    При ошибке не падает, а отвечает пользователю понятным текстом.
    """
    if update.message is None:
        return
    config = context.bot_data["config"]

    client = GigaChatClient(
        GigaChatConfig(
            auth_key=config.GIGACHAT_AUTH_KEY,
            model=config.GIGACHAT_MODEL,
            verify_ssl=config.GIGACHAT_VERIFY_SSL,
        )
    )

    try:
        async with client:
            token = await client.get_access_token()
            models = await client.get_models()
            reply = await client.complete(
                [{"role": "user", "content": "Ответь одним словом: привет"}]
            )
    except GigaChatAuthError as exc:
        await update.message.reply_text(f"Ошибка авторизации GigaChat:\n{exc}")
        return
    except GigaChatAPIError as exc:
        await update.message.reply_text(f"Ошибка GigaChat API:\n{exc}")
        return
    except Exception as exc:
        # Любая другая ошибка — не молчим, показываем пользователю
        logger.exception("Неожиданная ошибка в /test")
        await update.message.reply_text(f"Неожиданная ошибка:\n{exc}")
        return

    await update.message.reply_text(
        "GigaChat работает.\n"
        f"Токен получен: {token[:12]}...\n"
        f"Доступно моделей: {len(models)}\n"
        f"Тестовый ответ модели: {reply[:200]}"
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

    if not is_admin:
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

            if not is_admin:
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


async def notify_me(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик /notify_me <external_id>: шлёт уведомление с кнопкой по закону из БД."""
    if update.message is None:
        return
    session_maker = _session_maker(context)

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Использование: /notify_me <external_id или ссылка>\n"
            "Например: /notify_me 0001202608060001"
        )
        return
    external_id = _resolve_external_id(args[0])
    if external_id is None:
        await update.message.reply_text(
            "Не удалось определить external_id. Укажите id или ссылку правового портала."
        )
        return

    async with session_maker() as session:
        article = await session.scalar(
            select(Article).where(Article.external_id == external_id)
        )

    if article is None:
        await update.message.reply_text(
            f"Закон с external_id={external_id} не найден в базе."
        )
        return

    text, reply_markup = _build_notification(
        article, context.bot_data["config"].WEBAPP_URL
    )
    if update.effective_user is None:
        return
    await context.bot.send_message(
        chat_id=update.effective_user.id,
        text=text,
        reply_markup=reply_markup,
        parse_mode="MarkdownV2",
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
