# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

News Detector / LegalBot — a Telegram bot that monitors `pravo.gov.ru` for newly
published Russian legal acts, summarizes them via the GigaChat API, and pushes
notifications to subscribed users, plus a weekly "most important laws" digest.
Also serves a small Telegram Mini-App (WebApp) for reading the full text and
summaries of laws. Personal/pet project, Russian-language codebase and commit
history. Deployed as a Docker container on Dockhost (see "Deployment").

## Commands

```bash
# setup
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill TELEGRAM_BOT_TOKEN, GIGACHAT_AUTH_KEY, DATABASE_URL
mkdir -p data                # if DATABASE_URL points at ./data

# validate config without starting the bot
python -c "from app.core.config import Config; print(Config.load())"

# run
python -m app.main

# logs (LOG_FILE, default logs/app.log)
tail -f logs/app.log

# one-off tools (spend GigaChat quota, need a filled .env)
python -m scripts.calibrate_importance          # importance rubric vs golden labels
python -m scripts.seed_weekly_window --dry-run  # load the current digest window into the DB
```

There is no test suite, linter, or formatter configured in this repo
(no `pytest`, no `tests/` directory, no `ruff`/`black`/`mypy` config). Don't
assume one exists — verify manually (e.g. via the config-check command above,
or by running the bot) instead of running a test command.

## Architecture

```
Пользователь → Telegram Bot (PTB) → Ядро приложения → SQLite (SQLAlchemy async)
                                          ↓
              Сервисы: rss_parser · summarizer · scheduler · gigachat client
                                          ↓
                  Внешние API: pravo.gov.ru, GigaChat
```

Layering is strict and intentional (see `.cursor/rules/conventions.mdc`):
- `app/models/` — SQLAlchemy models only, no business logic.
- `app/core/` — infra: config loading, DB engine/session setup.
- `app/services/` — business logic: network calls, LLM calls, orchestration.
- `app/bot/` — Telegram-facing handlers only.
- `app/webapp/` — aiohttp server for the Mini-App (full law text view).
- `app/main.py` — wires everything together at startup.

### Startup sequence (`app/main.py`)

1. `Config.load()` reads `.env` into one frozen `Config` dataclass; missing
   `TELEGRAM_BOT_TOKEN`/`DATABASE_URL` raises `ConfigError` and aborts startup.
2. Logging configured (console + `app.log`, daily rotation, `LOG_RETENTION_DAYS`).
3. `init_database()` + `init_db_schema()` create the SQLite engine/session
   factory and tables if missing.
4. `_build_application()` builds the PTB `Application`, registers handlers,
   and stashes `config` and `session_maker` in `application.bot_data` — this
   is the shared "pocket" every handler/job reads from instead of re-parsing
   `.env` or opening ad-hoc connections.
5. `_schedule_jobs()` registers `check_legislation_updates` on PTB's
   `JobQueue` (APScheduler under the hood), every `CHECK_INTERVAL_MINUTES`.
6. `application.run_polling()` — PTB's own blocking call. Its internal hook
   order is `initialize → post_init → start`. Because `run_repeating(first=0)`
   does **not** fire immediately (a known APScheduler/PTB gotcha), the first
   check is manually triggered from `post_init` via `_run_initial_check`,
   which also decides `is_first_run` (suppress notification spam on an empty
   DB) and `needs_backfill` (see below). `post_init` then also starts the
   WebApp server, runs the weekly-digest catch-up (`_run_initial_weekly_digest`),
   inserts the onboarding demo article and makes the first DB backup.
   `_schedule_jobs()` additionally registers `weekly_digest_job` (only if
   `WEEKLY_DIGEST_TIME` is set) and the DB backup job.

### Processing one document (`app/services/scheduler.py`, `rss_parser.py`)

1. `fetch_documents()` pulls the day's document list from pravo.gov.ru's JSON
   API (all pages: `pageSize=200`, at most 20 pages) into `FeedEntry` objects
   (`external_id` = `eoNumber`, the site's publication number).
2. New entries are filtered against `articles.external_id` already in the DB
   — this is the idempotency guarantee: one document is processed exactly once.
3. For each new entry, `_process_entry()` resolves text and level:
   - `get_legal_text()` tries to fetch the ready HTML redaction and convert
     it to plain text; any failure mode (no redaction ready, network error
     after retries, placeholder text) returns `None`, never raises.
   - `classify_level_for_title()` / `classify_level()` tag the act's legal
     force (`FKZ`, `FZ`, `UNKNOWN`, ...) from `document_type_id`/title —
     stored on `Article.level` and used for per-user filtering. `DECREE` /
     `GOV_RESOLUTION` are strictly by title prefix ("Указ Президента Российской
     Федерации…" / "Постановление Правительства Российской Федерации…"); other
     decrees/resolutions (regional) are `REGIONAL`. Old rows are not reclassified.
   - If no text is ready and the act `is_important()` (`IMPORTANT_LEVELS`:
     Конституция/ФКЗ/ФЗ), OCR is attempted via GigaChat on the source PDF
     (`ocr_document_text`).
   - If text was obtained (directly or via OCR), `summarizer.summarize()`
     calls GigaChat for a 140–280 char summary (`SUMMARY_MIN_LEN`/`MAX_LEN`).
     GigaChat refusals are detected (`classify_reply` in `gigachat.py`) and
     handled without crashing.
   - Whatever happens, the article is saved (`_save_article`, protected
     against duplicate-insert races via `IntegrityError`), and users are
     notified: summary + link if available, otherwise a fallback message
     with just the link. **The bot must never go silent** — this fallback
     principle is load-bearing, see `docs/vision.md`.
   - After saving, candidates (`CONSTITUTION`/`FKZ`/`FZ`/`DECREE`/`GOV_RESOLUTION`)
     get an internal importance score 0–3 (`Article.importance`, NULL = not
     scored, never shown to users) in a separate `try/except` so a scoring
     failure never blocks saving/notifying: `prefilter_zero()` (title patterns
     for routine decrees → `0` without LLM), else GigaChat by a rubric
     (`app/services/importance.py`; input = summary, else start of text; no
     text yet → scoring deferred). Score `≥ 2` triggers a background summary
     (text, or OCR if absent) if there is none.
   - Unscored candidates are retried ("backlog") at the end of every
     `check_legislation_updates` cycle (`_rate_backlog`, ≤14 days since
     `created_at`, ≤20 per cycle); no text for >24h → one OCR before scoring.
4. Notifications respect each user's `UserFilter` rows (chosen legal-force
   levels in `user_filters`; no rows = no filter, receive everything) and
   skip users who blocked the bot (`Forbidden`). Only active users who passed
   the channel gate (`channel_verified`) are notified. `IMPORTANT_LEVELS` acts
   go out one message each; the rest: 1 act = one message, 2+ = one digest
   teaser (`_build_digest`: up to 3 acts + a "Дайджест" Mini-App button).
5. `needs_backfill` (set in `_run_initial_check`) drives a one-time 30-day
   backfill of FKZ/FZ acts on first run if the DB has none in that window —
   idempotent, only evaluated at startup.

### Weekly important digest (`app/services/scheduler.py`, `weekly_window.py`)

Every Friday at `WEEKLY_DIGEST_TIME` (`WEEKLY_DIGEST_TZ`; empty time = feature
off, scoring still runs) all acts with importance `≥ 2` accepted in the window
(previous Friday → this Friday, by `Article.created_at`) go to every active,
channel-verified user in one teaser (`_build_digest` with its own header),
ignoring `UserFilter`. `_maybe_send_weekly_digest` is the single entry point
(Friday job, `_post_init` catch-up, end of each check cycle), under an
`asyncio.Lock`; idempotent per period via `weekly_digest_runs`. Sends only
09:00–22:00 local; waits while window candidates are unscored (final pass scores
leftovers, by title if needed); hard deadline Sunday 22:00 (in the last hour
unscored ones are dropped; later the period is skipped). Acts with no summary
after two background attempts are excluded (ids logged) — no placeholder text.
Empty digest (including after that exclusion) = nothing sent. Admin preview:
`/test_digest weekly` (no DB writes, no scoring). Specs: `law-importance-scoring`,
`weekly-important-digest`.

**Moderation (`weekly_review.py`, `webapp/admin.py`, `webapp/static/admin.html`).** The
admin decision lives in `Article.digest_override` (`'include'` / `'exclude'` / NULL) and is
final: `final_selection(window)` (window docs with a summary that are `include`, or NULL with
importance ≥ 2; order = rating, unrated last, then newer first) feeds the send, the panel, the
Markdown export and `/test_digest weekly`. The final pass rates/waits only for unrated docs
with no decision, and the "attempt 2" summary runs only for docs that will actually go (not
`exclude`). On Friday at `WEEKLY_REVIEW_TIME` (default 12:00; empty = off) `_maybe_send_review_ping`
sends a service message **only to `ADMIN_CHAT_IDS`** straight via `bot.send_message` (never
through `users`/`_send_notification`), once per period (`weekly_review_pings`), with a Mini-App
button to `/admin` and a "🔗 Ссылка для браузера" callback. The panel shows the nearest unsent
digest (`target_period`); edits after `weekly_digest_runs.started_at` get 409. Auth: Telegram
`initData` (header `X-Telegram-Init-Data`) or a session cookie from the one-time link of `/admin`
(10 min token, 12 h session, both in process memory); POSTs need `X-Admin-Panel: 1` + JSON; no
CORS. Moderation is available only with `WEEKLY_DIGEST_TIME`, `WEBAPP_URL` and `ADMIN_CHAT_IDS`
(otherwise the digest goes automatically; reason is logged at start). Shared Mini-App styles
live in `app/webapp/static/webapp.css`. Spec: `weekly-digest-moderation`.

Known quirks (not bugs of this feature): a double GigaChat refusal while scoring
gives importance `2` (sensitive topics must not drop out), but if the summary
also fails the act is then excluded from the digest; `classify_level_for_title`
still tags any title containing "конституц" (e.g. a Constitutional Court ruling)
as `CONSTITUTION`; `Summarizer` cuts an over-long summary at `SUMMARY_MAX_LEN`
and appends "…", which can end mid-sentence.

### Bot commands / handlers (`app/bot/handlers.py`, registered in `app/main.py`)

- `/start` — registers the user (`users.telegram_id`, unique).
- `/latest` — last 30 days of ФКЗ/ФЗ.
- `/summary` — force-summarize a law by id/link; rate-limited per user per
  month via `SummarizationUsage` (`FORCE_SUMMARIZE_MONTHLY_LIMIT`); chat IDs
  in `ADMIN_CHAT_IDS` are exempt from the limit.
- `/settings` — a `ConversationHandler` wizard (`level_wizard_handler`) for
  choosing legal-force filter levels, backed by `user_filters`.
- `/help` — command reference.
- `/test_digest real <N>` / `/test_digest weekly` — admin-only debug previews.
- `/admin` (and the reminder's "Ссылка для браузера" button) — admin-only; sends a one-time
  login link to the moderation panel (link preview disabled so Telegram does not burn the token).
- Onboarding: `/start` registers the user and, if `REQUIRED_CHANNEL_ID` is set,
  asks to subscribe to the channel (`check_subscription` callback); a "show
  example" button sends the demo article (`app/services/demo_article.py`,
  `is_demo`, hidden from `/latest` and digests). Other commands/callbacks are
  wrapped in `require_channel_verified` (default-deny, no admin bypass).
- Full-text button opens the Mini-App (`app/webapp/server.py`), which only
  appears when `WEBAPP_URL` is set (Telegram requires HTTPS for WebApps).

### Mini-App (`app/webapp/server.py`)

aiohttp server started from `post_init` on `WEBAPP_HOST:WEBAPP_PORT`. Routes:
`GET /app?external_id=` (one law) and `GET /app?ids=a,b,…` (digest accordion,
capped by `DIGEST_ID_CAP`), `GET /full_text` (JSON with title, url, text,
`is_text_available`, `summary`), `POST /summarize` (force-summarize a digest
card; the user is identified by Telegram `initData` signature) and `GET /health`.
After a temporary summarize failure for a document there is a 60 s cooldown
(`SUMMARIZE_COOLDOWN_SECONDS`, in-memory `app["summarize_failed_at"]`) answered
without calling GigaChat, for any user; the page shows a toast banner and a
muted button with a countdown. Failures and cooldown do not consume the user's
monthly limit. The force-summarize core is `app/services/force_summary.py`,
shared with the bot (`/summary`, "Сделать саммари" buttons).

### Deployment

`Dockerfile` (`python:3.14-slim`, `python -m app.main`) + `docker-compose.yml`
(`.env`, volumes `./data` and `./logs`) — runs on Dockhost; see spec `deployment`.
`openspec/`, `docs/`, `.env`, `data/`, `logs/` are excluded from the image.
SQLite lives in `data/`; `app/services/backup.py` makes periodic consistent
copies (`VACUUM INTO`) into `DB_BACKUP_DIR` and rotates them.

### Key modules

| File | Responsibility |
|------|-----------------|
| `app/core/config.py` | `Config.load()` — the single source of settings from `.env` |
| `app/core/database.py` | Engine + async session factory + table creation |
| `app/models/user.py` | `User` — Telegram subscribers |
| `app/models/article.py` | `Article` — processed laws (`external_id` unique, `level`) |
| `app/models/user_filter.py` | `UserFilter` — per-user legal-force filter rows |
| `app/models/usage.py` | `SummarizationUsage` — monthly force-summarize counter |
| `app/services/rss_parser.py` | pravo.gov.ru list/text/OCR + level classification |
| `app/services/gigachat.py` | GigaChat HTTP client (auth token, files, completions, refusal detection) |
| `app/services/summarizer.py` | Prompt + summarization logic |
| `app/services/importance.py` | Importance 0–3: title prefilter + GigaChat rubric (`score_importance`) |
| `app/services/weekly_window.py` | Pure functions: Friday digest moments, daytime window, Sunday deadline |
| `app/models/weekly_digest_run.py` | `WeeklyDigestRun` — one row per sent weekly period (idempotency) |
| `app/services/weekly_review.py` | Moderation rules without HTTP/DB: `final_selection`, `target_period`, short title, Markdown export |
| `app/webapp/admin.py` | Moderation panel HTTP: login tokens/sessions, `require_admin`, `/admin/api/*` |
| `app/models/weekly_review_ping.py` | `WeeklyReviewPing` — one row per sent admin reminder (idempotency) |
| `app/services/scheduler.py` | Orchestrates fetch → classify → summarize/OCR → save → score → notify; backlog scoring; weekly digest |
| `app/services/force_summary.py` | Force-summarize core shared by bot and Mini-App (limits, cache, refusal handling) |
| `app/services/channel_subscription.py` | `is_subscribed()` — Telegram channel gate check (fail-closed) |
| `app/services/backup.py` | SQLite backup job (`VACUUM INTO`) with rotation |
| `app/services/demo_article.py` | Constants of the onboarding demo article |
| `app/bot/handlers.py` | All Telegram command/callback/conversation handlers |
| `app/webapp/server.py` | aiohttp Mini-App server: full text, digest cards with summaries, force-summarize |

For a much more detailed, code-line-referenced walkthrough of the same flow
(written for a junior dev), see `docs/FLOW.md`. `docs/vision.md` has the
original architectural rationale/principles. Both may lag newer features
(e.g. `docs/FLOW.md` predates the WebApp, filters, backfill, importance scoring
and the weekly digest; `docs/PLAN.md` and `docs/idea.md` are historical) — trust
the code and `openspec/specs/` over the docs when they disagree.

## Project conventions (from `.cursor/rules/`)

These apply to `.cursor`, `.gemini`, `.github`, `.opencode` agent configs
alike (all mirrors of the same OpenSpec rules); the canonical copy is
`.cursor/rules/conventions.mdc` and `.cursor/rules/workflow.mdc`.

- **KISS, hard.** Simple functions over classes/abstractions. No speculative
  generality, no "future-proofing" architecture. Three similar lines beat a
  premature abstraction — this project explicitly rejects heavy abstraction,
  complex config, and full test coverage as goals.
- Type hints on functions; docstrings for public functions.
- `snake_case` functions/files, `PascalCase` classes, `UPPER_SNAKE_CASE`
  constants.
- Only the parser's text-extraction/regex logic is expected to be unit
  tested — the project deliberately does not aim for full test coverage.
- Iterative workflow: the human wants to see a short plan before
  implementation of nontrivial modules and confirm before changes land —
  don't sprawl a task into unrelated refactors.
- Never hardcode secrets; everything sensitive comes from `.env`
  (`TELEGRAM_BOT_TOKEN`, `GIGACHAT_AUTH_KEY`, etc. — see `.env.example`).

## Spec-driven change tracking (OpenSpec)

This repo uses OpenSpec (`openspec/`) to track feature proposals and specs.
`openspec/specs/` holds current capability specs (e.g. `law-force-filter`,
`legislation-ingestion`, `llm-refusal-handling`, `content-viewing`,
`digest-notifications`, `force-summarization`, `law-importance-scoring`,
`weekly-important-digest`, `deployment`); `openspec/changes/` holds
in-progress change proposals (`proposal.md`, `design.md`, `tasks.md` per
change), finished ones moved under `openspec/changes/archive/` (dated). There
are currently no active changes; `law-sphere-filter` and `law-region-filter`
live as GitHub issues #7 and #8. When making a nontrivial feature change, check
whether a relevant spec/change already exists there before starting.

`.claude/commands/opsx/*` (`propose`, `explore`, `apply`, `update`, `archive`,
`sync`) and the matching `.claude/skills/openspec-*` are the tooling for
working with `openspec/` — use the `/opsx-*` commands to create/apply/archive
change proposals rather than hand-editing files under `openspec/`. These are
mirrored 1:1 under `.cursor/`, `.gemini/`, `.github/`, `.opencode/` for other
agents; `.claude/` is just this tool's copy of the same OpenSpec workflow, not
a separate or conflicting rule set.

## Environment (`.env`)

Notable variables beyond the obvious (see `.env.example` for the full list):
`CHECK_INTERVAL_MINUTES` (default 15), `PRAVO_API_URL`, `SUMMARY_MIN_LEN`/
`SUMMARY_MAX_LEN`, `FORCE_SUMMARIZE_MONTHLY_LIMIT`, `ADMIN_CHAT_IDS`
(comma-separated, unlimited force-summarize), `TELEGRAM_PROXY_URL` (needed
from RU, `api.telegram.org` is blocked there), `WEBAPP_HOST`/`WEBAPP_PORT`/
`WEBAPP_URL` (WebApp button hidden if `WEBAPP_URL` is empty), `LOG_FILE`/
`LOG_RETENTION_DAYS`, `BACKFILL_ENABLED` (false skips the 30-day startup
backfill), `REQUIRED_CHANNEL_ID` (empty = channel gate off),
`DB_BACKUP_DIR`/`DB_BACKUP_INTERVAL_HOURS`/`DB_BACKUP_RETENTION_COUNT`, `GIGACHAT_AUTH_KEY`/`GIGACHAT_SCOPE`/`GIGACHAT_MODEL`/
`GIGACHAT_VERIFY_SSL`, `WEEKLY_DIGEST_TIME` (`HH:MM`, empty = weekly digest
off) / `WEEKLY_DIGEST_TZ` (default `Europe/Moscow`; needs `tzdata` on slim images),
`WEEKLY_REVIEW_TIME` (`HH:MM`, default `12:00`, empty = no admin reminder; must be earlier than
`WEEKLY_DIGEST_TIME`).

Importance scripts are one-off tools, not a test suite: `scripts/calibrate_importance.py`
(golden labels in `scripts/importance_golden.csv`, `--fill` adds url/summary columns;
few-shot examples in `importance.py` are excluded from its metrics),
`scripts/seed_weekly_window.py` (loads the current digest window into the DB with
`notified=True`, so nobody is notified) and the title sample for the prefilter in
`scripts/importance_titles_sample.json`.
