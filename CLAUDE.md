# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

News Detector / LegalBot — a Telegram bot that monitors `pravo.gov.ru` for newly
published Russian legal acts, summarizes them via the GigaChat API, and pushes
notifications to subscribed users. Also serves a small Telegram Mini-App (WebApp)
for reading the full text of a law. Personal/pet project, Russian-language
codebase and commit history.

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

# logs
tail -f app.log
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
   DB) and `needs_backfill` (see below).

### Processing one document (`app/services/scheduler.py`, `rss_parser.py`)

1. `fetch_documents()` pulls the day's document list from pravo.gov.ru's JSON
   API into `FeedEntry` objects (`external_id` = `eoNumber`, the site's
   publication number).
2. New entries are filtered against `articles.external_id` already in the DB
   — this is the idempotency guarantee: one document is processed exactly once.
3. For each new entry, `_process_entry()` resolves text and level:
   - `get_legal_text()` tries to fetch the ready HTML redaction and convert
     it to plain text; any failure mode (no redaction ready, network error
     after retries, placeholder text) returns `None`, never raises.
   - `classify_level_for_title()` / `classify_level()` tag the act's legal
     force (`FKZ`, `FZ`, `UNKNOWN`, ...) from `document_type_id`/title —
     stored on `Article.level` and used for per-user filtering.
   - If no text is ready and the act `is_important()` (currently: ФКЗ), OCR
     is attempted via GigaChat on the source PDF (`ocr_document_text`).
   - If text was obtained (directly or via OCR), `summarizer.summarize()`
     calls GigaChat for a 140–280 char summary (`SUMMARY_MIN_LEN`/`MAX_LEN`).
     GigaChat refusals are detected (`classify_reply` in `gigachat.py`) and
     handled without crashing.
   - Whatever happens, the article is saved (`_save_article`, protected
     against duplicate-insert races via `IntegrityError`), and users are
     notified: summary + link if available, otherwise a fallback message
     with just the link. **The bot must never go silent** — this fallback
     principle is load-bearing, see `docs/vision.md`.
4. Notifications respect each user's `UserFilter` rows (chosen legal-force
   levels in `user_filters`; no rows = no filter, receive everything) and
   skip users who blocked the bot (`Forbidden`).
5. `needs_backfill` (set in `_run_initial_check`) drives a one-time 30-day
   backfill of FKZ/FZ acts on first run if the DB has none in that window —
   idempotent, only evaluated at startup.

### Bot commands / handlers (`app/bot/handlers.py`, registered in `app/main.py`)

- `/start` — registers the user (`users.telegram_id`, unique).
- `/latest` — last 30 days of ФКЗ/ФЗ.
- `/summary` — force-summarize a law by id/link; rate-limited per user per
  month via `SummarizationUsage` (`FORCE_SUMMARIZE_MONTHLY_LIMIT`); chat IDs
  in `ADMIN_CHAT_IDS` are exempt from the limit.
- `/settings` — a `ConversationHandler` wizard (`level_wizard_handler`) for
  choosing legal-force filter levels, backed by `user_filters`.
- `/help` — command reference.
- Full-text button opens the Mini-App (`app/webapp/server.py`), which only
  appears when `WEBAPP_URL` is set (Telegram requires HTTPS for WebApps).

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
| `app/services/scheduler.py` | Orchestrates fetch → classify → summarize/OCR → save → notify |
| `app/bot/handlers.py` | All Telegram command/callback/conversation handlers |
| `app/webapp/server.py` | aiohttp Mini-App server for full law text |

For a much more detailed, code-line-referenced walkthrough of the same flow
(written for a junior dev), see `docs/FLOW.md`. `docs/vision.md` has the
original architectural rationale/principles. Both may lag newer features
(e.g. `docs/FLOW.md` predates the WebApp, filters, and backfill) — trust the
code over the docs when they disagree.

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
`llm-refusal-handling`, `content-viewing`); `openspec/changes/` holds
in-progress or archived change proposals (`proposal.md`, `design.md`,
`tasks.md` per change), archived ones moved under `openspec/changes/archive/`.
When making a nontrivial feature change, check whether a relevant spec/change
already exists there before starting.

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
`LOG_RETENTION_DAYS`, `GIGACHAT_AUTH_KEY`/`GIGACHAT_SCOPE`/`GIGACHAT_MODEL`/
`GIGACHAT_VERIFY_SSL`.
