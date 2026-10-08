"""HTTP-сервер Mini-App для просмотра полного текста закона.

Запускается вместе с ботом (main.py -> post_init) на WEBAPP_HOST:WEBAPP_PORT
и отдаёт роуты:
  GET /app?external_id=<id>            — HTML-страница фронтенда, один документ
  GET /app?ids=<id1>,<id2>,...         — та же страница, аккордеон из нескольких
                                          документов (кнопка «Полные тексты» дайджеста)
  GET /full_text?external_id=<id>      — JSON {title, url, text, is_text_available, summary}
  POST /summarize                      — принудительная саммаризация из карточки дайджеста
                                          (JSON {external_id, init_data}); пользователь
                                          определяется по подписанному Telegram initData

Режим ``ids`` переиспользует существующий ``/full_text`` (по одному запросу на
документ), а не отдельный batch-эндпоинт — проще и достаточно для размеров
дайджеста, ограниченных ``DIGEST_ID_CAP`` в scheduler.py.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from urllib.parse import parse_qsl

from aiohttp import web
from sqlalchemy import select

from app.models import Article, User
from app.services.force_summary import force_summarize

logger = logging.getLogger(__name__)

CORS = {"Access-Control-Allow-Origin": "*"}
INIT_DATA_MAX_AGE = 24 * 60 * 60  # страница дайджеста может долго висеть открытой
# После временной неудачи саммари документа (нет текста, сбой GigaChat) повтор по нему
# разрешён только через столько секунд — защита GigaChat от 429 при повторных нажатиях,
# перезагрузке страницы и запросах других пользователей к тому же документу
SUMMARIZE_COOLDOWN_SECONDS = 60

HTML_PAGE = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Полный текст закона</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  * { box-sizing: border-box; }
  :root { --sum-accent: var(--tg-theme-button-color, #2688d4); --sum-bg: rgba(38, 136, 212, 0.12); --sum-border: rgba(38, 136, 212, 0.45); }
  @supports (color: color-mix(in srgb, red, blue)) { :root { --sum-bg: color-mix(in srgb, var(--sum-accent) 14%, transparent); --sum-border: color-mix(in srgb, var(--sum-accent) 45%, transparent); } }
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
  /* плашки состояния и ошибки — одна геометрия (как у саммари), разный смысл цвета:
     .unavailable — нормальное состояние «текста пока нет» (нейтральная, в цветах темы),
     .error — что-то пошло не так (красная; ей же оформлен баннер-уведомление .toast) */
  .error, .unavailable { border: 1px solid; border-radius: 8px; padding: 8px 10px; margin: 8px 0 12px 0; font-size: 14px; line-height: 1.5; }
  .error { color: #842029; background: #f8d7da; border-color: #f5c2c7; }
  .unavailable { color: var(--tg-theme-text-color, #222); background: var(--tg-theme-secondary-bg-color, #f1f1f4); border-color: rgba(112, 117, 121, 0.25); }
  .loading { color: #555; }
  .digest-hint { color: var(--tg-theme-hint-color, #707579); font-size: 13px; margin: 0 0 10px 0; }
  .doc { border: 1px solid var(--tg-theme-hint-color, #ddd); border-radius: 8px; margin-bottom: 10px; }
  .doc summary { cursor: pointer; padding: 12px; font-weight: 600; list-style: none; }
  .doc summary::-webkit-details-marker { display: none; }
  .doc-head-title { display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .doc-head-title::before { content: "▸ "; }
  .doc[open] .doc-head-title::before { content: "▾ "; }
  .sum { background: var(--sum-bg); border: 1px solid var(--sum-border); border-radius: 8px; padding: 8px 10px; margin: 8px 0 12px 0; font-size: 14px; font-weight: 400; line-height: 1.5; word-break: break-word; }
  .sum-label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; color: var(--sum-accent); margin-bottom: 2px; }
  .sum-preview { margin: 8px 0 0 0; }
  .doc[open] .sum-preview { display: none; }
  .sum-action { margin: 8px 0 12px 0; }
  .sum-btn { display: inline-flex; align-items: center; gap: 8px; background: var(--tg-theme-button-color, #2688d4); color: var(--tg-theme-button-text-color, #fff); border: 0; border-radius: 8px; padding: 8px 14px; font-size: 14px; line-height: 20px; cursor: pointer; transition: background .15s, color .15s, box-shadow .15s, transform .12s; }
  .sum-btn:active:not(:disabled) { transform: scale(.98); }
  .sum-btn:disabled { cursor: default; }
  /* нажатое состояние (loading): та же плашка, что и у готового саммари (.sum) — плашка «вырастает» из кнопки */
  .sum-btn.busy { background: var(--sum-bg); color: var(--sum-accent); box-shadow: inset 0 0 0 1px var(--sum-border); }
  /* чётный размер и целая высота строки — центр вращения попадает на целый пиксель, спиннер не «гуляет» */
  .sum-btn .spin { width: 14px; height: 14px; flex: none; border: 2px solid currentColor; border-right-color: transparent; border-radius: 50%; will-change: transform; animation: sum-spin .8s linear infinite; }
  @keyframes sum-spin { to { transform: rotate(360deg); } }
  @media (prefers-reduced-motion: reduce) { .sum-btn .spin { animation: none; } }
  .sum-status { font-size: 13px; color: var(--tg-theme-hint-color, #707579); margin-top: 6px; }
  /* кулдаун после неудачи: кнопка видна, но приглушена; нажатие показывает баннер */
  .sum-btn.cooling { opacity: .55; }
  /* баннер о неудаче саммари (заметнее строки статуса под кнопкой): внешний вид — от .error,
     здесь только положение, лёгкая тень (отделяет от контента под ним) и появление */
  .toast { position: fixed; left: 16px; right: 16px; top: calc(16px + env(safe-area-inset-top, 0px)); max-width: 768px; margin: 0 auto; box-shadow: 0 2px 10px rgba(0, 0, 0, .12); opacity: 0; transform: translateY(-20px); pointer-events: none; transition: opacity .2s, transform .2s; z-index: 10; }
  .toast-count { font-variant-numeric: tabular-nums; }
  .toast.show { opacity: 1; transform: none; pointer-events: auto; }
  @media (prefers-reduced-motion: reduce) { .toast { transition: none; } }
  /* тёмная тема Telegram (класс .dark ставит скрипт по tg.colorScheme): приглушённые варианты тех же
     цветов — светлые плашки на тёмном фоне слепят, а серая сливается с фоном без заметной рамки */
  .dark .disclaimer { color: #ffda6a; background: #332701; border-color: #997404; }
  .dark .error { color: #ea868f; background: #2c0b0e; border-color: #842029; }
  .dark .unavailable { border-color: rgba(112, 132, 153, 0.6); }
  .dark .toast { box-shadow: 0 2px 12px rgba(0, 0, 0, .5); }
  .doc-body { padding: 0 12px 12px 12px; }
  .doc-title { font-weight: 700; margin: 0 0 8px 0; }
</style>
</head>
<body>
<div class="container">
  <div class="beta">Beta</div>
  <div class="disclaimer">⚠️ Текст распознан с помощью ИИ, возможны ошибки.</div>
  <div id="single">
    <h1 id="title" class="title loading">Загрузка…</h1>
    <div id="date" class="date"></div>
    <div id="content" class="text loading">Загрузка текста закона…</div>
    <div id="link" class="link"></div>
  </div>
  <div id="digest" style="display:none">
    <div class="digest-hint">👇 Нажмите на закон, чтобы посмотреть текст</div>
    <div id="digest-list"></div>
  </div>
</div>
<div id="toast" class="error toast" role="alert" aria-live="assertive"></div>
<script>
(function() {
  const tg = window.Telegram && window.Telegram.WebApp;
  if (tg) { tg.ready(); tg.expand(); }
  // тёмные варианты плашек — по схеме Telegram (не по настройке ОС: вне Telegram фон всегда светлый)
  function applyColorScheme() {
    document.documentElement.classList.toggle('dark', !!tg && tg.colorScheme === 'dark');
  }
  applyColorScheme();
  if (tg && tg.onEvent) tg.onEvent('themeChanged', applyColorScheme);

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

  function renderResult(data) {
    // общая логика для одиночного документа и одного пункта дайджеста:
    // дата подписания, текст/фолбэк недоступности, ссылка на портал
    let dateText = null;
    try {
      dateText = extractSigningDate(data.text || '', data.title || '');
    } catch (e) { /* дата не критична */ }

    const isAvailable = !!(data.is_text_available && data.text);
    let contentHtml;
    if (isAvailable) {
      try {
        contentHtml = renderLawText(data.text);
      } catch (e) {
        console.error('render error', e);
        contentHtml = '<p>' + escapeHtml(String(data.text)) + '</p>';
      }
    } else {
      contentHtml = 'Содержание этого закона пока недоступно в текстовом формате. Откройте оригинал на портале или запросите саммари: PDF-файл распознаётся с помощью ИИ.';
    }

    let linkHtml = '';
    if (data.url) {
      linkHtml = '<a href="' + String(data.url).replace(/"/g, '&quot;') + '" target="_blank" rel="noopener">Читать на портале</a>';
    }

    return { dateText: dateText, contentHtml: contentHtml, isAvailable: isAvailable, linkHtml: linkHtml };
  }

  const params = new URLSearchParams(window.location.search);
  const idsParam = params.get('ids');
  if (idsParam) {
    document.getElementById('single').style.display = 'none';
    const digestEl = document.getElementById('digest');
    const digestListEl = document.getElementById('digest-list');
    digestEl.style.display = 'block';
    const ids = idsParam.split(',').map(function(s) { return s.trim(); }).filter(Boolean);
    if (!ids.length) {
      digestListEl.innerHTML = '<div class="error">Не переданы документы дайджеста. URL: ' + escapeHtml(window.location.href) + '</div>';
      return;
    }
    const origin = window.location.origin;

    // после временной неудачи (нет текста, сбой GigaChat) кнопку можно нажать снова только через минуту
    const COOLDOWN_MS = 60000;
    const toastEl = document.getElementById('toast');
    let toastTimer = null;
    let toastTick = null;
    function hideToast() {
      toastEl.classList.remove('show');
      clearTimeout(toastTimer);
      clearInterval(toastTick);
    }
    toastEl.addEventListener('click', hideToast);
    // until — конец кулдауна: тогда под сообщением идёт живой обратный отсчёт
    function showToast(message, until) {
      if (!message) return;
      clearTimeout(toastTimer);
      clearInterval(toastTick);
      toastEl.innerHTML = '<div class="toast-text"></div><div class="toast-count"></div>';
      toastEl.querySelector('.toast-text').textContent = message;
      const count = toastEl.querySelector('.toast-count');
      function tick() {
        const left = Math.ceil(((until || 0) - Date.now()) / 1000);
        count.textContent = !until ? '' : left > 0 ? 'Повторить можно через ' + left + ' сек.' : 'Можно повторить.';
        count.style.display = count.textContent ? '' : 'none';
        if (left <= 0) clearInterval(toastTick);
      }
      tick();
      if (until) toastTick = setInterval(tick, 1000);
      toastEl.classList.add('show');
      if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('error');
      toastTimer = setTimeout(hideToast, 6000);
    }
    function startCooldown(details, ms) {
      details._cooldownUntil = Date.now() + ms;
      applyCooldown(details);
    }
    // приглушает кнопку до конца кулдауна (после перерисовки карточки — тоже)
    function applyCooldown(details) {
      const btn = details.querySelector('.sum-btn');
      const left = (details._cooldownUntil || 0) - Date.now();
      if (!btn || left <= 0) return;
      btn.classList.add('cooling');
      setTimeout(function() {
        const current = details.querySelector('.sum-btn');
        if (current) current.classList.remove('cooling');
      }, left);
    }

    function fetchFullText(id) {
      return fetch(origin + '/full_text?external_id=' + encodeURIComponent(id))
        .then(function(r) {
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.json();
        });
    }
    function sumHtml(summary, cls) {
      return '<div class="sum ' + cls + '"><div class="sum-label">Саммари</div>' +
        escapeHtml(summary).replace(/\n/g, '<br>') + '</div>';
    }
    // act: {message, button} — сообщение на месте кнопки «Сделать саммари» и нужна ли кнопка
    function renderCard(details, data, id, act) {
      act = act || {};
      details._id = id;
      details._data = data;
      details._loaded = true;
      const title = data.title || id;
      const r = renderResult(data);
      const head = details.querySelector('summary');
      head.innerHTML = '<span class="doc-head-title">' + escapeHtml(title) + '</span>' +
        (data.summary ? sumHtml(data.summary, 'sum-preview') : '');
      // полный заголовок дублируется в теле секции — заголовок в <summary> обрезается эллипсисом
      let html = '<div class="doc-title">' + escapeHtml(title) + '</div>';
      if (r.dateText) html += '<div class="date">Дата подписания: ' + escapeHtml(r.dateText) + '</div>';
      if (data.summary) {
        html += sumHtml(data.summary, '');
      } else {
        html += '<div class="sum-action">' +
          (act.button === false ? '' : '<button type="button" class="sum-btn">Сделать саммари</button>') +
          '<div class="sum-status">' + escapeHtml(act.message || '') + '</div></div>';
      }
      html += r.isAvailable ? r.contentHtml : '<p class="unavailable">' + escapeHtml(r.contentHtml) + '</p>';
      if (r.linkHtml) html += '<div class="link">' + r.linkHtml + '</div>';
      const body = details.querySelector('.doc-body');
      body.innerHTML = html;
      body.className = 'doc-body';
      const btn = body.querySelector('.sum-btn');
      if (btn) btn.addEventListener('click', function() { startSummarize(details); });
      applyCooldown(details);
    }
    // непустые summary/text из ответа заменяют прежние при любом статусе
    function mergeData(data, res) {
      if (res.summary) data.summary = res.summary;
      if (res.text) { data.text = res.text; data.is_text_available = true; }
    }
    // busy — кнопка показывает спиннер и «Саммари в процессе создания…»; message — текст под кнопкой
    function setActionState(details, message, busy) {
      const status = details.querySelector('.sum-status');
      const btn = details.querySelector('.sum-btn');
      if (status) status.textContent = message || '';
      if (!btn) return;
      btn.disabled = !!busy;
      btn.classList.toggle('busy', !!busy);
      btn.setAttribute('aria-busy', busy ? 'true' : 'false');
      if (busy) btn.innerHTML = '<span class="spin"></span>Саммари в процессе создания…';
      else btn.textContent = 'Сделать саммари';
    }

    function startSummarize(details) {
      const id = details._id;
      const data = details._data;
      const left = (details._cooldownUntil || 0) - Date.now();
      if (left > 0) {
        showToast('Попробуйте позже.', details._cooldownUntil);
        return;
      }
      if (!tg || !tg.initData) {
        const msg = 'Саммари можно сделать, открыв дайджест из Telegram.';
        setActionState(details, msg, false);
        showToast(msg);
        return;
      }
      details._busy = true;
      setActionState(details, '', true);
      fetch(origin + '/summarize', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ external_id: id, init_data: tg.initData })
      })
        .then(function(r) {
          if (r.status === 401) return { stop: 'Сессия устарела — закройте и снова откройте дайджест.' };
          if (r.status === 403) {
            return r.json().then(
              function(j) { return { stop: j.message || 'Нет доступа.' }; },
              function() { return { stop: 'Нет доступа.' }; }
            );
          }
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.json();
        })
        .then(function(res) {
          details._busy = false;
          if (res.stop) { setActionState(details, res.stop, false); showToast(res.stop); return; }
          mergeData(data, res);
          const final = res.status === 'limit' || res.status === 'refused';
          if (res.status === 'no_text' || res.status === 'error' || res.status === 'cooldown') {
            details._cooldownUntil = Date.now() + (res.retry_after ? res.retry_after * 1000 : COOLDOWN_MS);
          }
          // строка под кнопкой — только для окончательных исходов (кнопки больше нет, причина должна остаться);
          // разовые неудачи сообщает баннер, кнопка остаётся для повтора после кулдауна
          renderCard(details, data, id, res.status === 'ok' ? {} : { message: final ? res.message : '', button: !final });
          if (res.status === 'cooldown') showToast('Саммари сейчас не получить.', details._cooldownUntil);
          else if (res.status !== 'ok') showToast(res.message, final ? null : details._cooldownUntil);
        })
        .catch(function() {
          // запрос мог дойти до сервера, а ответ оборвал посредник — перепроверяем документ
          const wait = 'Не удалось дождаться ответа — возможно, саммари ещё создаётся.';
          fetchFullText(id)
            .then(function(fresh) {
              details._busy = false;
              if (fresh.summary) { mergeData(data, fresh); renderCard(details, data, id); return; }
              setActionState(details, '', false);
              startCooldown(details, COOLDOWN_MS);
              showToast(wait, details._cooldownUntil);
            })
            .catch(function() {
              details._busy = false;
              setActionState(details, '', false);
              startCooldown(details, COOLDOWN_MS);
              showToast(wait, details._cooldownUntil);
            });
        });
    }

    ids.forEach(function(rawId) {
      let id = rawId;
      try { id = decodeURIComponent(rawId); } catch (e) { /* оставляем как есть */ }
      const details = document.createElement('details');
      details.className = 'doc';
      const summary = document.createElement('summary');
      summary.textContent = 'Загрузка…';
      const body = document.createElement('div');
      body.className = 'doc-body loading';
      body.textContent = 'Загрузка текста закона…';
      details.appendChild(summary);
      details.appendChild(body);
      digestListEl.appendChild(details);

      // при разворачивании карточки без саммари перепроверяем: его мог сделать другой пользователь
      details.addEventListener('toggle', function() {
        if (!details.open || !details._loaded || details._busy || details._data.summary) return;
        fetchFullText(id)
          .then(function(fresh) {
            if (details._busy || !fresh.summary) return;
            mergeData(details._data, fresh);
            renderCard(details, details._data, id);
          })
          .catch(function() { /* перепроверка не критична */ });
      });

      fetchFullText(id)
        .then(function(data) { renderCard(details, data, id); })
        .catch(function(err) {
          summary.textContent = id;
          body.textContent = 'Не удалось загрузить текст: ' + err.message;
          body.className = 'doc-body error';
        });
    });
    return;
  }

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
      const r = renderResult(data);
      // дата подписания сверху после заголовка
      if (r.dateText) {
        dateEl.textContent = 'Дата подписания: ' + r.dateText;
        dateEl.style.display = 'block';
      } else {
        dateEl.textContent = '';
        dateEl.style.display = 'none';
      }
      if (r.isAvailable) {
        contentEl.innerHTML = r.contentHtml;
        contentEl.className = 'text';
      } else {
        contentEl.textContent = r.contentHtml;
        contentEl.className = 'unavailable';
      }
      if (r.linkHtml) {
        linkEl.innerHTML = r.linkHtml;
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
                    "summary": article.summary or None,
                },
                headers={"Access-Control-Allow-Origin": "*"},
            )
    except Exception:
        logger.exception("handle_full_text: error for %s", external_id)
        return web.json_response(
            {"error": "internal error"}, status=500,
            headers={"Access-Control-Allow-Origin": "*"},
        )


def _verify_init_data(init_data: str, bot_token: str) -> int | None:
    """Проверяет подпись Telegram WebApp initData и возвращает user.id (иначе None).

    Схема Telegram: secret = HMAC_SHA256("WebAppData", bot_token); подписывается
    отсортированный data_check_string из всех полей, кроме hash. Дополнительно
    auth_date не должен быть старше INIT_DATA_MAX_AGE. initData не логируется.
    """
    try:
        fields = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = fields.pop("hash", "")
        if not received_hash:
            return None
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received_hash):
            return None
        if time.time() - int(fields["auth_date"]) > INIT_DATA_MAX_AGE:
            return None
        return int(json.loads(fields["user"])["id"])
    except (KeyError, ValueError, TypeError):
        return None


async def handle_summarize(request: web.Request) -> web.Response:
    """Принудительная саммаризация документа из карточки дайджеста.

    Доменные исходы (ok/limit/no_text/refused/error, а также cooldown — повтор по
    документу после временной неудачи раньше ``SUMMARIZE_COOLDOWN_SECONDS``) — 200 со статусом и готовым
    для карточки message; 400 — плохой запрос, 401 — нет/неверная initData,
    403 — пользователь неизвестен или не подтвердил подписку, 500 — сбой сервера.
    """
    try:
        payload = await request.json()
        external_id = str(payload.get("external_id") or "").strip()
        init_data = str(payload.get("init_data") or "")
    except (ValueError, AttributeError):
        return web.json_response({"error": "bad request"}, status=400, headers=CORS)
    if not external_id:
        return web.json_response({"error": "missing external_id"}, status=400, headers=CORS)

    config = request.app["config"]
    telegram_id = _verify_init_data(init_data, config.TELEGRAM_BOT_TOKEN)
    if telegram_id is None:
        return web.json_response({"error": "unauthorized"}, status=401, headers=CORS)

    try:
        session_maker = request.app["session_maker"]
        async with session_maker() as session:
            user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            return web.json_response(
                {"message": "Откройте бота и нажмите /start."}, status=403, headers=CORS
            )
        if config.REQUIRED_CHANNEL_ID and not user.channel_verified:
            return web.json_response(
                {"message": "Подтвердите подписку на канал в боте."}, status=403, headers=CORS
            )

        failed_at: dict[str, float] = request.app["summarize_failed_at"]
        left = SUMMARIZE_COOLDOWN_SECONDS - (time.monotonic() - failed_at.get(external_id, float("-inf")))
        if left > 0:
            retry_after = int(left) + 1
            logger.info("handle_summarize: user=%s %s -> cooldown %ss", telegram_id, external_id, retry_after)
            return web.json_response(
                {
                    "status": "cooldown",
                    "retry_after": retry_after,
                    "message": f"Саммари сейчас не получить. Попробуйте через {retry_after} сек.",
                },
                headers=CORS,
            )

        result = await force_summarize(config, session_maker, user, external_id)
        if result.status in ("no_text", "error"):
            failed_at[external_id] = time.monotonic()
        else:
            failed_at.pop(external_id, None)
        messages = {
            "limit": (
                f"Месячный лимит саммаризаций ({config.FORCE_SUMMARIZE_MONTHLY_LIMIT}) "
                "исчерпан. Попробуйте в следующем месяце."
            ),
            "no_text": "Не удалось получить текст закона. Попробуйте позже.",
            "refused": "Языковая модель отказалась сформировать саммари для этого документа.",
            "error": "Не удалось сделать саммари. Попробуйте позже.",
        }
        logger.info("handle_summarize: user=%s %s -> %s", telegram_id, external_id, result.status)
        return web.json_response(
            {
                "status": result.status,
                "summary": result.summary,
                "text": result.text,
                "is_text_available": bool(result.text and result.text.strip()),
                "message": messages.get(result.status),
            },
            headers=CORS,
        )
    except Exception:
        logger.exception("handle_summarize: error for %s", external_id)
        return web.json_response({"error": "internal error"}, status=500, headers=CORS)


def create_app(session_maker, config) -> web.Application:
    """Создаёт aiohttp Application с роутами WebApp."""
    app = web.Application()
    app["session_maker"] = session_maker
    app["config"] = config
    app["summarize_failed_at"] = {}  # external_id -> time.monotonic() последней временной неудачи
    app.router.add_get("/app", handle_app)
    app.router.add_get("/full_text", handle_full_text)
    app.router.add_post("/summarize", handle_summarize)
    # health-check
    async def health(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})
    app.router.add_get("/health", health)
    return app


_runner: web.AppRunner | None = None
_site: web.TCPSite | None = None


async def start_webapp(host: str, port: int, session_maker, config) -> None:
    """Запускает HTTP-сервер WebApp. При ошибке логирует и не роняет бота."""
    global _runner, _site
    try:
        app = create_app(session_maker, config)
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
