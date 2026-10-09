"""Админ-панель модерации недельной подборки (HTTP-слой).

Роуты (регистрируются только если модерация доступна, см. ``setup_admin``):
  GET  /admin                — статичная страница панели (без данных)
  GET  /admin/login?t=<токен> — обмен одноразовой ссылки на сессию (cookie) и редирект на /admin
  GET  /admin/api/state      — подборка целевого периода: сводка и разделы
  POST /admin/api/toggle     — включить/исключить документ
  POST /admin/api/summary    — сделать саммари через GigaChat либо сохранить вручную
  GET  /admin/api/export     — Markdown итогового набора

Доступ — только ``ADMIN_CHAT_IDS``: по подписанной Telegram ``initData`` (заголовок
``X-Telegram-Init-Data``, Mini-App) либо по сессии в cookie (браузер, вход по ссылке из
``/admin``). CORS не включается; изменяющие запросы требуют ``X-Admin-Panel: 1`` и
``Content-Type: application/json`` — кросс-сайтовая форма или ``fetch`` без CORS их не выставят.
Токены и сессии хранятся в памяти процесса: перезапуск сбрасывает их (design.md, п. 9).
"""

from __future__ import annotations

import logging
import secrets
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiohttp import web
from sqlalchemy import update

from app.models import Article, User
from app.services.force_summary import force_summarize
from app.services.rss_parser import _normalize_text
from app.services.scheduler import load_review_target
from app.services.weekly_review import (
    EXCLUDE,
    INCLUDE,
    SUMMARY_MANUAL_MAX_LEN,
    build_export_markdown,
    final_selection,
    moderation_unavailable_reason,
    review_counts,
    short_title,
    split_sections,
)
from app.webapp.server import STATIC_DIR, SUMMARIZE_COOLDOWN_SECONDS, _verify_init_data

logger = logging.getLogger(__name__)

LOGIN_TOKEN_TTL = 10 * 60  # одноразовая ссылка действует 10 минут
SESSION_TTL = 12 * 60 * 60
SESSION_COOKIE = "admin_session"
_WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")

# token / session_id -> (telegram_id, истекает по time.monotonic())
_login_tokens: dict[str, tuple[int, float]] = {}
_sessions: dict[str, tuple[int, float]] = {}


def _purge_expired() -> None:
    now = time.monotonic()
    for store in (_login_tokens, _sessions):
        for key in [k for k, (_, exp) in store.items() if exp <= now]:
            del store[key]


def issue_login_token(telegram_id: int) -> str:
    """Выдаёт одноразовый токен входа в панель (TTL ``LOGIN_TOKEN_TTL``)."""
    _purge_expired()
    token = secrets.token_urlsafe(32)
    _login_tokens[token] = (telegram_id, time.monotonic() + LOGIN_TOKEN_TTL)
    return token


def login_url(webapp_url: str, token: str) -> str:
    """Ссылка для браузера: ``<WEBAPP_URL>/admin/login?t=<токен>``."""
    return f"{webapp_url.rstrip('/')}/admin/login?t={token}"


_NO_STORE = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def _json(data: dict, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, headers=_NO_STORE)


def _deny(exc_class, code: str, message: str) -> web.HTTPException:
    return exc_class(
        text=web.json_response({"error": code, "message": message}).text,
        content_type="application/json",
        headers=_NO_STORE,
    )


def require_admin(request: web.Request) -> int:
    """Возвращает telegram_id администратора или бросает 401/403 (без раскрытия данных).

    Сначала ``initData`` из заголовка, затем сессия из cookie. Для POST дополнительно
    обязательны ``X-Admin-Panel: 1`` и ``Content-Type: application/json``.
    """
    config = request.app["config"]
    telegram_id: int | None = None
    init_data = request.headers.get("X-Telegram-Init-Data")
    if init_data:
        telegram_id = _verify_init_data(init_data, config.TELEGRAM_BOT_TOKEN)
    if telegram_id is None:
        session = _sessions.get(request.cookies.get(SESSION_COOKIE, ""))
        if session is not None and session[1] > time.monotonic():
            telegram_id = session[0]
    if telegram_id is None:
        raise _deny(
            web.HTTPUnauthorized, "session_expired",
            "Сессия закончилась. Отправьте боту команду /admin.",
        )
    if telegram_id not in config.ADMIN_CHAT_IDS:
        raise _deny(web.HTTPForbidden, "forbidden", "Доступ только для администраторов.")
    if request.method == "POST":
        if request.headers.get("X-Admin-Panel") != "1" or request.content_type != "application/json":
            raise _deny(web.HTTPForbidden, "forbidden", "Запрос отклонён.")
    return telegram_id


# -- Вход ---------------------------------------------------------------------

async def handle_login(request: web.Request) -> web.Response:
    """Обменивает одноразовый токен на сессию в cookie и перенаправляет на /admin."""
    config = request.app["config"]
    _purge_expired()
    entry = _login_tokens.pop(request.query.get("t", ""), None)  # токен гасится при первом открытии
    headers = {**_NO_STORE, "Referrer-Policy": "no-referrer"}
    if entry is None or entry[1] <= time.monotonic() or entry[0] not in config.ADMIN_CHAT_IDS:
        page = (
            '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">'
            '<title>Вход в панель</title><link rel="stylesheet" href="/static/webapp.css"></head>'
            '<body><div class="container"><h1 class="title">Ссылка недействительна</h1>'
            '<p class="error">Ссылка уже использована или истекла. Отправьте боту команду '
            "<b>/admin</b> и откройте новую ссылку.</p></div></body></html>"
        )
        return web.Response(text=page, status=400, content_type="text/html", charset="utf-8", headers=headers)
    session_id = secrets.token_urlsafe(32)
    _sessions[session_id] = (entry[0], time.monotonic() + SESSION_TTL)
    response = web.HTTPFound("/admin", headers=headers)
    response.set_cookie(
        SESSION_COOKIE, session_id, max_age=SESSION_TTL, path="/admin",
        httponly=True, samesite="Strict",
        secure=config.WEBAPP_URL.lower().startswith("https"),  # на http://localhost cookie с Secure не примется
    )
    return response


async def handle_admin_page(request: web.Request) -> web.Response:
    """Статичная страница панели; данные подгружает ``/admin/api/state`` после проверки роли."""
    return web.Response(
        body=(STATIC_DIR / "admin.html").read_bytes(),
        content_type="text/html", charset="utf-8", headers=_NO_STORE,
    )


# -- Состояние ----------------------------------------------------------------

def _card(a: Article) -> dict:
    return {
        "external_id": a.external_id,
        "title": _normalize_text(a.title) or a.title,
        "short_title": short_title(a.title),
        "importance": a.importance,
        "override": a.digest_override,
        "summary": a.summary or None,
        "url": a.url,
    }


def _deadline_label(period_end: datetime, tz: ZoneInfo) -> str:
    local = period_end.astimezone(tz)
    return f"{_WEEKDAYS[local.weekday()]} {local:%H:%M}"


async def _build_state(request: web.Request) -> dict:
    """Текущее состояние панели: целевой период, сводка и разделы."""
    config = request.app["config"]
    now = datetime.now(timezone.utc)
    target, window = await load_review_target(request.app["session_maker"], config, now)
    included, unrated, rest = split_sections(window)
    counts = review_counts(window)
    seconds_left = int((target.period_end - now).total_seconds())
    return {
        "period_end": target.period_end.isoformat(),
        "deadline_label": _deadline_label(target.period_end, ZoneInfo(config.WEEKLY_DIGEST_TZ)),
        "seconds_left": seconds_left,
        "overdue": seconds_left <= 0,  # отправка за этот период отложена и ещё не началась
        "sending": target.sending,
        "counts": {"included": counts.included, "no_summary": counts.no_summary, "unrated": counts.unrated},
        "sections": {
            "included": [_card(a) for a in included],
            "unrated": [_card(a) for a in unrated],
            "rest": [_card(a) for a in rest],
        },
    }


async def handle_state(request: web.Request) -> web.Response:
    require_admin(request)
    try:
        return _json(await _build_state(request))
    except Exception:
        logger.exception("admin state: error")
        return _json({"error": "internal", "message": "Не удалось загрузить подборку."}, 500)


# -- Правки -------------------------------------------------------------------

async def _editable_article(request: web.Request, external_id: str) -> Article:
    """Документ целевого периода, который можно править; иначе бросает 409/404."""
    target, window = await load_review_target(request.app["session_maker"], request.app["config"])
    if target.sending:
        raise _deny(web.HTTPConflict, "sending", "Отправка подборки уже началась — правки закрыты.")
    article = next((a for a in window if a.external_id == external_id), None)
    if article is None:
        raise _deny(web.HTTPNotFound, "not_in_window", "Документа нет в текущей подборке — обновите страницу.")
    return article


async def _read_body(request: web.Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise _deny(web.HTTPBadRequest, "bad_request", "Некорректный запрос.")
    return body


async def handle_toggle(request: web.Request) -> web.Response:
    """Включает/исключает документ; решение записывается явно (``include`` / ``exclude``)."""
    admin_id = require_admin(request)
    body = await _read_body(request)
    external_id = str(body.get("external_id") or "")
    include = body.get("include")
    if not external_id or not isinstance(include, bool):
        raise _deny(web.HTTPBadRequest, "bad_request", "Некорректный запрос.")
    article = await _editable_article(request, external_id)
    if include and not article.summary:
        raise _deny(web.HTTPBadRequest, "no_summary", "Сначала нужно саммари.")
    async with request.app["session_maker"]() as session:
        await session.execute(
            update(Article).where(Article.id == article.id).values(digest_override=INCLUDE if include else EXCLUDE)
        )
        await session.commit()
    logger.info("admin %s: %s -> %s", admin_id, external_id, INCLUDE if include else EXCLUDE)
    return _json({"status": "ok", "state": await _build_state(request)})


_SUMMARY_MESSAGES = {
    "no_text": "Не удалось получить текст закона. Попробуйте позже.",
    "refused": "Языковая модель отказалась сформировать саммари для этого документа.",
    "error": "Не удалось сделать саммари. Попробуйте позже.",
}


async def handle_summary(request: web.Request) -> web.Response:
    """Саммари документа: ``action=generate`` (GigaChat, с кулдауном) или ``action=save`` (вручную)."""
    admin_id = require_admin(request)
    body = await _read_body(request)
    external_id = str(body.get("external_id") or "")
    action = body.get("action")
    if not external_id or action not in ("generate", "save"):
        raise _deny(web.HTTPBadRequest, "bad_request", "Некорректный запрос.")
    article = await _editable_article(request, external_id)
    session_maker = request.app["session_maker"]

    if action == "save":
        text = str(body.get("text") or "").strip()
        if not text:
            raise _deny(web.HTTPBadRequest, "empty", "Саммари не может быть пустым.")
        if len(text) > SUMMARY_MANUAL_MAX_LEN:
            raise _deny(
                web.HTTPBadRequest, "too_long",
                f"Саммари слишком длинное: {len(text)} из {SUMMARY_MANUAL_MAX_LEN} символов.",
            )
        async with session_maker() as session:
            await session.execute(update(Article).where(Article.id == article.id).values(summary=text))
            await session.commit()
        logger.info("admin %s: ручное саммари %s (%d симв.)", admin_id, external_id, len(text))
        return _json({"status": "ok", "state": await _build_state(request)})

    failed_at: dict[str, float] = request.app["summarize_failed_at"]
    left = SUMMARIZE_COOLDOWN_SECONDS - (time.monotonic() - failed_at.get(external_id, float("-inf")))
    if left > 0:
        retry_after = int(left) + 1
        return _json({
            "status": "cooldown", "retry_after": retry_after,
            "message": f"Саммари сейчас не получить. Попробуйте через {retry_after} сек.",
            "state": await _build_state(request),
        })
    # не персистентный User: force_summarize нужен только telegram_id (админы без лимита)
    result = await force_summarize(request.app["config"], session_maker, User(telegram_id=admin_id), external_id)
    if result.status in ("no_text", "error"):
        failed_at[external_id] = time.monotonic()
    else:
        failed_at.pop(external_id, None)
    logger.info("admin %s: саммари через GigaChat %s -> %s", admin_id, external_id, result.status)
    return _json({
        "status": result.status,
        "message": _SUMMARY_MESSAGES.get(result.status),
        "retry_after": SUMMARIZE_COOLDOWN_SECONDS if result.status in ("no_text", "error") else None,
        "state": await _build_state(request),
    })


async def handle_export(request: web.Request) -> web.Response:
    """Markdown текущего итогового набора (тот же ``final_selection``, что у рассылки)."""
    require_admin(request)
    _, window = await load_review_target(request.app["session_maker"], request.app["config"])
    selected = final_selection(window)
    return _json({"markdown": build_export_markdown(selected), "count": len(selected)})


def setup_admin(app: web.Application) -> None:
    """Регистрирует роуты панели, если модерация доступна; иначе пишет причину в лог."""
    reason = moderation_unavailable_reason(app["config"])
    if reason:
        logger.info("Админ-панель не подключена: %s", reason)
        return
    app.router.add_get("/admin", handle_admin_page)
    # allow_head=False: HEAD-запрос предпросмотра ссылки не должен сжигать одноразовый токен
    app.router.add_get("/admin/login", handle_login, allow_head=False)
    app.router.add_get("/admin/api/state", handle_state)
    app.router.add_post("/admin/api/toggle", handle_toggle)
    app.router.add_post("/admin/api/summary", handle_summary)
    app.router.add_get("/admin/api/export", handle_export)
