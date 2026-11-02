## 1. Бэкенд: режимы счёта (`app/services/stats.py`)

- [x] 1.1 Кортеж `COMPARE_BASES = ("to_date", "rolling", "completed")`; `compare_periods(unit, basis, now)` по таблице design.md, п. 1; неизвестный `basis` → `ValueError`; обновить docstring
- [x] 1.2 Проверить `compare_periods` из консоли: `to_date` даёт прежние отрезки (среда 15:00 МСК; 31 марта 12:00 → весь февраль); `rolling` в пн 02.11 10:00 МСК (неделя — 26.10 10:00 / 19.10 10:00; месяц — 05.10 10:00 / 07.09 10:00); `completed` в пн 02.11 00:05 и 10:00 (неделя 26.10–02.11 против 19.10–26.10; месяц октябрь против сентября), 1 января (декабрь против ноября)
- [x] 1.3 `compare(..., unit, basis="to_date", now=None)`: передать `basis`, добавить `"basis"` в ответ; docstring

## 2. API (`app/webapp/admin.py`)

- [x] 2.1 `handle_stats`: для `compare_week|compare_month` читать `basis` (нет → `to_date`, вне `stats.COMPARE_BASES` → 400 `bad_request`), передавать именованным аргументом; для `pulse`/`trend` не использовать; обновить docstring модуля и функции (`&basis=to_date|rolling|completed`)

## 3. Фронт: состояние и второй переключатель (`app/webapp/static/admin.html`)

- [x] 3.1 `compareBasis` + `localStorage` `adminCompareBasis` (неизвестное → `to_date`), восстановление при старте рядом с `adminTrendMode`
- [x] 3.2 `statsView()` в сравнении → `compare_<unit>:<basis>`; `loadStats` строит URL из ключа (`view` и `basis`); слоты `sv[...]` создаются по мере надобности; проверить, что `statsStatus`, `updateStatsClock`, кнопка «Повторить» и `setStatsKind` работают с новыми ключами
- [x] 3.3 `BASIS_LABELS` и `basisSwitch(unit)` (`.seg.seg-wide`, `role="tablist"`/`tab`, `aria-label="Режим счёта"`, `data-key="basis:…"`) под `modeSwitch()` в `compareView`; `setCompareBasis(basis)`; возврат фокуса после перерисовки — и для `basis:`
- [x] 3.4 Подпись отрезков в `compareView` по `d.basis` (design.md, п. 4): `to_date` — как сейчас, `rolling` — `mskStamp`, `completed` — «Неделя дд.мм–дд.мм · Было: …» / «Месяц дд.мм–дд.мм · Было: …»

## 4. Пояснения (`admin.html`)

- [x] 4.1 Константы `CMP_TAIL_WEEK` / `CMP_TAIL_MONTH` (общая часть нынешних текстов) и шесть ключей `trend.compare_<unit>_<basis>` по design.md, п. 5; в каждом — одна фраза о двух других режимах счёта; удалить `trend.compare_week` / `trend.compare_month`
- [x] 4.2 `compareView` и `openAllHints('trend')` берут ключ `'trend.compare_' + unit + '_' + compareBasis`; обновить последнюю фразу `HINT_TREND_NOTE`; сверить тексты с `compare_periods`

## 5. Проверка и документация

- [x] 5.1 `python -c "from app.core.config import Config; print(Config.load())"` и импорт `app.services.stats`, `app.webapp.admin` проходят
- [x] 5.2 Локальный WebApp, вход по ссылке `/admin`: в обоих режимах сравнения переключаются все три режима счёта, подписи отрезков верные; выбор общий для недели и месяца и сохраняется после перезагрузки; первый запуск (очищенный `localStorage`) — «С начала …»; в «Недели» второго переключателя нет; при смене режима счёта сначала «Загрузка…», а не старые цифры
- [x] 5.3 Цифры: для «Прошлая неделя» значения 2–3 рядов совпадают с двумя последними полными столбиками «Недель»; для «7 дней» один-два ряда сверить ручным SQL; `?view=compare_week` без `basis` = `to_date`; `?view=compare_week&basis=foo` → 400
- [x] 5.4 Пояснения: заголовок таблицы открывает пояснение выбранного режима счёта; «Все пояснения» начинаются с него; при открытой шторке режим счёта не переключается; ширина 320–360 px и десктоп, светлая и тёмная тема
- [x] 5.5 Проверка в Telegram Mini-App на телефоне (после деплоя): два ряда переключателей на узком экране, тёмная тема
- [x] 5.6 CLAUDE.md, абзац «Admin panel and stats»: режимы счёта сравнения (`basis=to_date|rolling|completed`, 7/28 дней, завершённые недели/месяцы, `adminCompareBasis`)
