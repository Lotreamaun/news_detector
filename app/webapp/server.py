"""HTTP-сервер Mini-App для просмотра полного текста закона.

Запускается вместе с ботом (main.py -> post_init) на WEBAPP_HOST:WEBAPP_PORT
и отдаёт два роута:
  GET /app?external_id=<id>            — HTML-страница фронтенда
  GET /full_text?external_id=<id>      — JSON {title, url, text, is_text_available}
"""

from __future__ import annotations

import logging

from aiohttp import web
from sqlalchemy import select

from app.models import Article

logger = logging.getLogger(__name__)

HTML_PAGE = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Полный текст закона</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  * { box-sizing: border-box; }
  body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: var(--tg-theme-bg-color, #fff); color: var(--tg-theme-text-color, #222); line-height: 1.6; }
  .container { max-width: 800px; margin: 0 auto; padding: 16px; position: relative; }
  .disclaimer { background: #fff3cd; color: #664d03; border: 1px solid #ffecb5; border-radius: 8px; padding: 10px 12px; margin-bottom: 16px; font-size: 14px; }
  .beta { position: absolute; top: 10px; right: 10px; background: #0d6efd; color: #fff; font-size: 11px; font-weight: 700; padding: 3px 8px; border-radius: 12px; letter-spacing: 0.5px; }
  .title { font-size: 20px; font-weight: 700; margin: 0 0 4px 0; }
  .date { font-size: 13px; color: var(--tg-theme-hint-color, #707579); margin: 0 0 16px 0; }
  .text { word-break: break-word; font-size: 15px; line-height: 1.7; }
  .text p { margin: 0 0 12px 0; }
  .text p:last-child { margin-bottom: 0; }
  .text table { width: 100%; border-collapse: collapse; margin: 12px 0; font-size: 14px; display: block; overflow-x: auto; }
  .text th, .text td { border: 1px solid #ddd; padding: 6px 8px; text-align: left; }
  .text th { background: #f2f2f2; font-weight: 600; }
  .link { margin-top: 16px; }
  .link a { color: var(--tg-theme-link-color, #2688d4); text-decoration: none; word-break: break-all; }
  .error { color: #842029; background: #f8d7da; border: 1px solid #f5c2c7; border-radius: 8px; padding: 12px; }
  .loading { color: #555; }
</style>
</head>
<body>
<div class="container">
  <div class="beta">Beta</div>
  <div class="disclaimer">⚠️ Текст распознан с помощью ИИ, возможны ошибки.</div>
  <h1 id="title" class="title loading">Загрузка…</h1>
  <div id="date" class="date"></div>
  <div id="content" class="text loading">Загрузка текста закона…</div>
  <div id="link" class="link"></div>
</div>
<script>
(function() {
  const tg = window.Telegram && window.Telegram.WebApp;
  if (tg) { tg.ready(); tg.expand(); }

  function escapeHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function isTableSeparator(line) {
    const t = line.trim();
    if (!t.includes('|') && !t.includes('-')) return false;
    // separator like |---|---| , ---|--- , | --- | --- |
    return /^[\s|:\-]+$/.test(t) && t.includes('---');
  }
  function stripPlainMarkdown(s) {
    // убираем Markdown-разметку для обычного текста, таблицы обрабатываются отдельно
    return String(s)
      .replace(/\*\*(.*?)\*\*/g, '$1')
      .replace(/__(.*?)__/g, '$1')
      .replace(/\*(.*?)\*/g, '$1')
      .replace(/_(.*?)_/g, '$1')
      .replace(/`{1,3}(.*?)`{1,3}/g, '$1')
      .replace(/^#+\s*/gm, '')
      .replace(/\[([^\]]+)\]\([^\)]+\)/g, '$1');
  }
  function extractSigningDate(text, title) {
    const src = (title || '') + '\n' + (text || '');
    // форматы: от 01.09.2026, 01.09.2026, 1 сентября 2026
    let m = src.match(/от\s+(\d{2}\.\d{2}\.\d{4})/i);
    if (m) return m[1];
    m = src.match(/(\d{2}\.\d{2}\.\d{4})/);
    if (m) return m[1];
    m = src.match(/(\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\s+\d{4})/i);
    if (m) return m[1];
    return null;
  }

  function renderLawText(text) {
    const lines = String(text).split('\n');
    let html = '';
    let i = 0;
    while (i < lines.length) {
      if (i + 1 < lines.length && lines[i].includes('|') && isTableSeparator(lines[i+1])) {
        const tableLines = [lines[i], lines[i+1]];
        i += 2;
        while (i < lines.length && lines[i].includes('|') && lines[i].trim() !== '') {
          tableLines.push(lines[i]);
          i++;
        }
        // build table, skip separator row (index 1)
        let tableHtml = '<table>';
        tableLines.forEach((row, idx) => {
          if (idx === 1) return;
          let cells = row.split('|').map(c => c.trim());
          // remove empty leading/trailing cell caused by leading/trailing |
          if (cells.length && cells[0] === '') cells.shift();
          if (cells.length && cells[cells.length-1] === '') cells.pop();
          const tag = idx === 0 ? 'th' : 'td';
          tableHtml += '<tr>' + cells.map(c => '<' + tag + '>' + escapeHtml(stripPlainMarkdown(c)) + '</' + tag + '>').join('') + '</tr>';
        });
        tableHtml += '</table>';
        html += tableHtml;
      } else {
        let para = [];
        while (i < lines.length && lines[i].trim() !== '' && !(i+1 < lines.length && lines[i].includes('|') && isTableSeparator(lines[i+1]))) {
          para.push(lines[i]);
          i++;
        }
        if (para.length) {
          const paraText = stripPlainMarkdown(para.join('\n'));
          html += '<p>' + escapeHtml(paraText).replace(/\n/g, '<br>') + '</p>';
        }
        while (i < lines.length && lines[i].trim() === '') i++;
      }
    }
    return html || '<p>' + escapeHtml(String(text)) + '</p>';
  }

  const params = new URLSearchParams(window.location.search);
  let externalId = params.get('external_id');
  if (!externalId) {
    const m = window.location.href.match(/[?&]external_id=([^&]+)/);
    if (m) try { externalId = decodeURIComponent(m[1]); } catch(e) { externalId = m[1]; }
  }
  console.log('WebApp href', window.location.href, 'externalId', externalId);
  const titleEl = document.getElementById('title');
  const dateEl = document.getElementById('date');
  const contentEl = document.getElementById('content');
  const linkEl = document.getElementById('link');
  if (!externalId) {
    titleEl.textContent = 'Ошибка';
    titleEl.className = 'title';
    contentEl.textContent = 'Не передан external_id документа. URL: ' + window.location.href;
    contentEl.className = 'error';
    return;
  }
  const fetchUrl = window.location.origin + '/full_text?external_id=' + encodeURIComponent(externalId);
  console.log('fetch', fetchUrl);
  fetch(fetchUrl)
    .then(r => {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(data => {
      titleEl.textContent = data.title || externalId;
      titleEl.className = 'title';
      // дата подписания сверху после заголовка
      try {
        const d = extractSigningDate(data.text || '', data.title || '');
        if (d) {
          dateEl.textContent = 'Дата подписания: ' + d;
          dateEl.style.display = 'block';
        } else {
          dateEl.textContent = '';
          dateEl.style.display = 'none';
        }
      } catch(e) { dateEl.style.display = 'none'; }
      if (data.is_text_available && data.text) {
        try {
          contentEl.innerHTML = renderLawText(data.text);
        } catch(e) {
          console.error('render error', e);
          contentEl.textContent = String(data.text);
        }
        contentEl.className = 'text';
      } else {
        contentEl.textContent = 'Текст этого закона пока недоступен в текстовом формате. Откройте оригинал на портале.';
        contentEl.className = 'error';
      }
      if (data.url) {
        linkEl.innerHTML = '<a href=\"' + data.url.replace(/\"/g, '&quot;') + '\" target=\"_blank\" rel=\"noopener\">Читать на портале</a>';
      }
    })
    .catch(err => {
      console.error('fetch error', err);
      titleEl.textContent = 'Ошибка загрузки';
      contentEl.textContent = 'Не удалось загрузить текст: ' + err.message + ' (URL: ' + window.location.href + ')';
      contentEl.className = 'error';
    });
})();
</script>
</body>
</html>
"""


async def handle_app(request: web.Request) -> web.Response:
    """Отдаёт HTML-страницу Mini-App."""
    return web.Response(
        text=HTML_PAGE,
        content_type="text/html",
        charset="utf-8",
        headers={"Access-Control-Allow-Origin": "*"},
    )


async def handle_full_text(request: web.Request) -> web.Response:
    """Отдаёт JSON с полным текстом закона по external_id."""
    external_id = (request.query.get("external_id") or "").strip()
    if not external_id:
        logger.warning("handle_full_text: missing external_id, query=%s", dict(request.query))
        return web.json_response(
            {"error": "missing external_id"}, status=400,
            headers={"Access-Control-Allow-Origin": "*"},
        )

    try:
        session_maker = request.app["session_maker"]
        async with session_maker() as session:
            article = await session.scalar(
                select(Article).where(Article.external_id == external_id)
            )
            if article is None:
                logger.warning("handle_full_text: not found %s", external_id)
                return web.json_response(
                    {"error": "not found"}, status=404,
                    headers={"Access-Control-Allow-Origin": "*"},
                )

            is_available = bool(article.original_text and article.original_text.strip())
            logger.info("handle_full_text: %s -> is_available=%s", external_id, is_available)
            return web.json_response(
                {
                    "title": article.title,
                    "url": article.url,
                    "text": article.original_text if is_available else None,
                    "is_text_available": is_available,
                },
                headers={"Access-Control-Allow-Origin": "*"},
            )
    except Exception:
        logger.exception("handle_full_text: error for %s", external_id)
        return web.json_response(
            {"error": "internal error"}, status=500,
            headers={"Access-Control-Allow-Origin": "*"},
        )


def create_app(session_maker) -> web.Application:
    """Создаёт aiohttp Application с роутами WebApp."""
    app = web.Application()
    app["session_maker"] = session_maker
    app.router.add_get("/app", handle_app)
    app.router.add_get("/full_text", handle_full_text)
    # health-check
    async def health(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})
    app.router.add_get("/health", health)
    return app


_runner: web.AppRunner | None = None
_site: web.TCPSite | None = None


async def start_webapp(host: str, port: int, session_maker) -> None:
    """Запускает HTTP-сервер WebApp. При ошибке логирует и не роняет бота."""
    global _runner, _site
    try:
        app = create_app(session_maker)
        _runner = web.AppRunner(app)
        await _runner.setup()
        _site = web.TCPSite(_runner, host, port)
        await _site.start()
        logger.info("WebApp запущен на %s:%s", host, port)
    except OSError as exc:
        logger.warning("Не удалось запустить WebApp на %s:%s: %s", host, port, exc)
    except Exception:
        logger.exception("Неожиданная ошибка запуска WebApp")


async def stop_webapp() -> None:
    """Останавливает HTTP-сервер WebApp (если запущен)."""
    global _runner, _site
    try:
        if _site is not None:
            await _site.stop()
            _site = None
        if _runner is not None:
            await _runner.cleanup()
            _runner = None
        logger.info("WebApp остановлен")
    except Exception:
        logger.exception("Ошибка остановки WebApp")
