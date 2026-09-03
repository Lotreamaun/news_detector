## Context

См. proposal.md — Why. После `law-force-filter` (`UserFilter` по `level`, `Article.level` из `documentTypeId`, визард шага «Сила») нужен второй срез — сфера. Текущее состояние: `User` без сфер, `Article` без `spheres`, классификатор отсутствует, `scheduler._notify_users` фильтрует только по `level`. Требуется добавить сферу без LLM.

## Goals / Non-Goals

**Goals:**
- Отдельный фильтр `sphere` (12 категорий, multi-label) с дефолтом «Все», расширяет `UserFilter`.
- Классификатор `classify_sphere(title, summary)` по ключевым словам + леммам (`pymorphy2`), без LLM, <10мс, детерминированный.
- Визард шаг 1/3 «Сфера» с 12 toggle Inline, комбинируется с фильтром по силе (AND).
- Фильтр применяется в `_notify_users` и `latest`.

**Non-Goals:**
- Не трогаем регион — следующий срез.
- Не меняем `law-force-filter` (сила) — дополняем.
- Не вводим LLM, не меняем `original_text` пайплайн.

## Decisions

### 1. Модель — расширить `user_filters` vs новая таблица
Расширяем `user_filters` до составного `(user_id, dimension, value)` где `dimension` = `level`|`sphere` (или две колонки `level`/`sphere` с nullable). Выбираем `(user_id, dimension, value)` — нормализовано, один индекс, легко добавить `region` позже. Альтернатива отдельная `user_spheres` — лишний JOIN, но тоже ок. Миграция — `ALTER` + добавление строк.

### 2. Article.spheres — CSV TEXT vs JSON vs отдельная таблица
Храним `articles.spheres TEXT` как CSV лемм-категорий (`finance,nature`) — просто, `WHERE spheres LIKE '%finance%'` не нужен, фильтрация в Python `set(spheres.split(',')) ∩ set(user_spheres)`. Альтернатива отдельная `article_spheres` — нормализовано, но JOIN на каждую статью. Для 12 категорий CSV достаточно.

### 3. Классификатор — ключевые слова + леммы vs LLM
Используем `pymorphy2` для лемматизации `title+summary` → токены → подсчёт уникальных лемм из словарей `sphere_keywords.py` (15-30 лемм на сферу, напр. `финансы: [финанс, банк, налог, крипта]`). Score = количество уникальных лемм, порог `>=2` → сфера, иначе `other`. Точка проверки — `title + summary` (fallback 1000 знаков `original_text`). Multi-label до 3 сфер. Альтернатива LLM — точнее на 20%, но +5с и 2₽/закон, отклонена для MVP. Словари уточняются на реализации, не в спеке.

### 4. Визард — порядок и состояние
Порядок: `Сфера` (шаг 1, самый сильный фильтр) → `Сила` (шаг 2) — как в explore. Состояние в `context.user_data['spheres']` (set), кнопки `sphere:finance` тогглят, `[Выбрать всё]`/`[Далее]` пишут в `user_filters`. `/start` для новых → визард, `/settings` → показать текущие ☑.

### 5. Применение — AND между измерениями
`scheduler._notify_users`: `if article.level not in user_levels (и не Все) → skip`; `if not (set(article.spheres) ∩ set(user_spheres) or Все) → skip`. `latest` — аналогично `WHERE` в Python после `SELECT`.

## Risks / Trade-offs

- [Словарь не покрыл «О внесении изменений в ФЗ-123» без ключевых слов] → score 0 → `other` → попадёт только к «Все», для выбранных сфер — false negative. Митигация: fallback на `other` + мониторинг, позже LLM-фолбэк.
- [Омоним `мир` (договор vs вселенная)] → ложное срабатывание. Митигация: порог 2 уникальные леммы, вайтлист `Статья` не считается.
- [12 кнопок — перегруз] → пагинация не нужна, 3×4 сетка влезает, `[Выбрать всё]` снижает трение.
- [pymorphy2 медленный на 30 законах/день] → <10мс/закон, кэш лемм, приемлемо.

## Migration Plan

1. Миграция `user_filters` (добавить `dimension`) или `ALTER TABLE articles ADD COLUMN spheres`, default `other`/`Все`.
2. Деплой классификатора (обратная совместимость: пустые `spheres` = `other` = «Все»).
3. Роллбэк: `DROP COLUMN spheres` / удалить строки `dimension=sphere` — фильтр снова «Все».

## Open Questions

- Точный список 12 сфер и лемм — определим на реализации, не блокирует дизайн.
