## Context

См. proposal.md — Why/What Changes для мотивации. Текущее состояние (`app/bot/handlers.py`):

- `start()` для нового пользователя показывает 3 кнопки в одном сообщении: `wizard:setup`, `wizard:skip`, `show_example`.
- `show_example` — самостоятельный `CallbackQueryHandler` (не часть `ConversationHandler`), уже реализует тестовое уведомление, переиспользуется как есть.
- `level_wizard_handler` (`ConversationHandler`) имеет entry_points `CommandHandler("settings", ...)` и `CallbackQueryHandler(level_choice, pattern=r"^wizard:(setup|skip)$")` — эти паттерны запускают визард из любого места, где показана такая кнопка.
- Этот change зависит от `channel-subscription-gate`: кнопка «Проверить подписку» (`callback_data="check_subscription"` по design.md того change) и флаг `channel_verified` определены там.

## Goals / Non-Goals

**Goals:**
- Свести первый экран `/start` к одной кнопке без потери существующей функциональности (`show_example`, визард).
- Встроить гейт подписки между демонстрацией пользы и настройкой фильтров, не создавая новый `ConversationHandler`.

**Non-Goals:**
- Изменение самого визарда фильтров (`level_wizard_handler`, `_build_level_keyboard`) — не трогаем.
- Изменение экрана `/start` для уже существующих пользователей.
- Реализация проверки подписки — она определена в `channel-subscription-gate`.

## Decisions

### Новые экраны — простые `CallbackQueryHandler`, без `ConversationHandler`
Три новых шага (приветствие → тест → гейт) реализуются как последовательность independent `CallbackQueryHandler` по новым `callback_data` (например `onboarding:example` для перехода к тестовому уведомлению, дальше используется `check_subscription` из `channel-subscription-gate`), без сохранения состояния между сообщениями — каждый шаг самодостаточен (запрашивает актуальные данные из БД по `telegram_id` пользователя). Это соответствует стилю проекта: `show_example` уже устроен так же (`persistent=False` у визарда, никакого состояния сессии для не-визардных экранов).
**Альтернатива (отклонена):** обернуть весь онбординг в `ConversationHandler` — усложнение ради трёх последовательных сообщений, которые и так идентифицируются по `telegram_id`/БД, а не по состоянию диалога.

### Точка входа в визард остаётся той же (`wizard:setup`/`wizard:skip`)
Экран после гейта показывает те же кнопки `Настроить`/`Пропустить → Все` с тем же `callback_data`, что и сейчас — `level_wizard_handler.entry_points` не меняется. Меняется только то, какой хендлер отправляет эти кнопки пользователю (раньше — `start()`, теперь — обработчик успешной проверки подписки).
**Альтернатива (отклонена):** заводить новые callback-паттерны для входа в визард после гейта — избыточно, существующий паттерн уже подходит.

## Risks / Trade-offs

- [Риск] Этот change недеплоится без `channel-subscription-gate` — тексты экрана "подпишись на канал" ссылаются на ещё не существующий обработчик `check_subscription`. **Митигация**: деплоить оба change вместе или `channel-subscription-gate` первым (зафиксировано в Impact proposal.md).
- [Trade-off] Путь нового пользователя к настройке фильтров стал на 1 шаг длиннее (подписка на канал) — осознанный компромисс из грилл-сессии (Q1/Q4): обязательность подписки важнее сокращения числа шагов.
