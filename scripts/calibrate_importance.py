"""
Калибровка оценки важности: прогон эталонных актов через ``score_importance``.

Разовый инструмент (не тесты репозитория). Эталон — ``scripts/importance_golden.csv``:
колонки ``group`` (``FZ`` или ``ACT`` — указы и постановления), ``external_id``,
``label`` (0–3, пусто = пропустить), ``title``. Вход оценки собирается так же, как в
проде: саммари акта (для ФЗ и актов с текстом), иначе начало текста, иначе название.

Запуск из корня репозитория (нужны ``GIGACHAT_AUTH_KEY`` и доступ к pravo.gov.ru)::

    python -m scripts.calibrate_importance --fill   # дописать в CSV колонки url и summary
    python -m scripts.calibrate_importance          # прогнать размеченные акты

Саммари кэшируются в ``scripts/.calibration_cache.json``, чтобы при подборе рубрики
не пересаммаризировать эталон на каждом прогоне.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
from collections import Counter
from pathlib import Path

from app.core.config import Config
from app.services.gigachat import GigaChatClient, GigaChatConfig, GigaChatError
from app.services.importance import FEW_SHOT_IDS, build_body, prefilter_zero, score_importance
from app.services.rss_parser import classify_level_for_title, get_legal_text
from app.services.summarizer import Summarizer, SummarizerConfig

_DEFAULT_CSV = Path(__file__).parent / "importance_golden.csv"
_CACHE = Path(__file__).parent / ".calibration_cache.json"
_LEVELS = (0, 1, 2, 3)
_URL = "http://publication.pravo.gov.ru/document/{}"
_FIELDS = ["group", "external_id", "label", "url", "summary", "title"]


def _read_rows(path: Path) -> list[dict[str, str]]:
    """Читает CSV эталона; терпим к экспорту из Excel/Numbers (``;``, строка с именем листа, BOM)."""
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    start = next((i for i, line in enumerate(lines) if "external_id" in line), None)
    if start is None:
        raise SystemExit(f"В {path} не найден заголовок с колонкой external_id")
    delimiter = ";" if lines[start].count(";") > lines[start].count(",") else ","
    return list(csv.DictReader(lines[start:], delimiter=delimiter))


def _load_golden(path: Path) -> list[dict[str, str]]:
    """Размеченные строки эталона (пустой ``label`` пропускается)."""
    rows = [r for r in _read_rows(path) if (r["label"] or "").strip()]
    for row in rows:
        if row["label"].strip() not in {"0", "1", "2", "3"}:
            raise SystemExit(f"Недопустимая метка {row['label']!r} у {row['external_id']} (нужно 0–3)")
    return rows


async def _load_entry(
    row: dict[str, str], summarizer: Summarizer, cache: dict[str, dict]
) -> dict:
    """Текст (начало) и саммари акта; результат кэшируется."""
    entry = cache.get(row["external_id"])
    if entry is None:
        text = await get_legal_text(row["external_id"])
        summary = None
        if text:
            try:
                summary = await summarizer.summarize(text)
            except GigaChatError as exc:
                print(f"  саммари {row['external_id']} не получено: {exc}")
        entry = {"text": (text or "")[:3000], "summary": summary}
        cache[row["external_id"]] = entry
    return entry


async def _fill_csv(path: Path, summarizer: Summarizer, cache: dict[str, dict]) -> None:
    """Дописывает в CSV колонки ``url`` и ``summary`` (то, что видит модель); метки сохраняются."""
    rows = _read_rows(path)
    for row in rows:
        entry = await _load_entry(row, summarizer, cache)
        body = build_body(entry["summary"], entry["text"]) or "(текста нет — модель увидит только название)"
        row["url"] = _URL.format(row["external_id"])
        row["summary"] = body.replace("\n", " ")
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_FIELDS)
        writer.writeheader()
        writer.writerows({k: row.get(k, "") for k in _FIELDS} for row in rows)
    print(f"Записано {len(rows)} строк в {path}")


def _print_report(name: str, pairs: list[tuple[dict[str, str], int | None]]) -> None:
    """Матрица ошибок (строки — эталон, столбцы — модель) и сводные числа по группе."""
    scored = [(r, s) for r, s in pairs if s is not None]
    print(f"\n=== {name}: {len(pairs)} актов, оценено {len(scored)} ===")
    if not scored:
        return
    matrix = Counter((int(r["label"]), s) for r, s in scored)
    print("эталон \\ модель   " + "  ".join(f"{lv:>3}" for lv in _LEVELS))
    for gold in _LEVELS:
        print(f"{gold:>16}   " + "  ".join(f"{matrix[(gold, lv)]:>3}" for lv in _LEVELS))
    exact = sum(1 for r, s in scored if int(r["label"]) == s)
    boundary = sum(1 for r, s in scored if (int(r["label"]) >= 2) == (s >= 2))
    print(f"точное совпадение: {exact}/{len(scored)}; граница 1/2 верна: {boundary}/{len(scored)}")
    print(f"модель раздала «3»: {sum(1 for _, s in scored if s == 3)}; эталон «3»: "
          f"{sum(1 for r, _ in scored if r['label'] == '3')}")
    wrong = [(r, s) for r, s in scored if int(r["label"]) != s]
    if wrong:
        print("расхождения (эталон → модель):")
        for r, s in wrong:
            print(f"  {r['label']} → {s}  {r['external_id']}  {r['title'][:110]}")
    missing = [r for r, s in pairs if s is None]
    for r in missing:
        print(f"  не оценено: {r['external_id']}  {r['title'][:110]}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--csv", type=Path, default=_DEFAULT_CSV)
    parser.add_argument("--no-cache", action="store_true", help="не использовать кэш саммари")
    parser.add_argument("--fill", action="store_true", help="дописать в CSV колонки url и summary и выйти")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)

    config = Config.load()
    cache: dict[str, dict] = {}
    if not args.no_cache and _CACHE.exists():
        cache = json.loads(_CACHE.read_text(encoding="utf-8"))
    rows = [] if args.fill else _load_golden(args.csv)
    if not rows and not args.fill:
        raise SystemExit(f"В {args.csv} нет размеченных актов (заполните колонку label)")

    groups: dict[str, list[tuple[dict[str, str], int | None]]] = {"FZ": [], "ACT": []}
    async with Summarizer(
        SummarizerConfig(
            auth_key=config.GIGACHAT_AUTH_KEY,
            model=config.GIGACHAT_MODEL,
            min_len=config.SUMMARY_MIN_LEN,
            max_len=config.SUMMARY_MAX_LEN,
            verify_ssl=config.GIGACHAT_VERIFY_SSL,
        )
    ) as summarizer, GigaChatClient(
        GigaChatConfig(
            auth_key=config.GIGACHAT_AUTH_KEY,
            model=config.GIGACHAT_MODEL,
            verify_ssl=config.GIGACHAT_VERIFY_SSL,
        )
    ) as client:
        if args.fill:
            await _fill_csv(args.csv, summarizer, cache)
            _CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
            return
        for row in rows:
            if row["external_id"] in FEW_SHOT_IDS:
                continue  # пример из промпта: на нём метрики были бы завышены
            level = classify_level_for_title(row["title"])
            if prefilter_zero(level, row["title"]):
                print(f"пропуск (предфильтр отсекает): {row['external_id']}")
                continue
            entry = await _load_entry(row, summarizer, cache)
            body = build_body(entry["summary"], entry["text"])
            score = await score_importance(client, row["title"], body)
            groups.setdefault(row["group"], []).append((row, score))
    _CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")

    _print_report("ФЗ", groups["FZ"])
    _print_report("Указы и постановления", groups["ACT"])


if __name__ == "__main__":
    asyncio.run(main())
