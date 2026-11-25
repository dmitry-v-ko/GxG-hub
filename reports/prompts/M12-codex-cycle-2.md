# M12, цикл 2 — промпт для Codex в `gxg_agents_v2`

Продолжай в той же ветке `codex/m12-signals-gxg-hub`, от `1487186`. Пушь только в неё, в `main` не мержи.

Критик цикла 1 поставил **7.5 при пороге 8**. AC-M12-01…06 пройдены, но есть два major. Сделай ровно два пункта ниже, нового объёма не добавляй.

## 1. Ошибка `effects.status` после propose не должна убивать кандидата

Сейчас (`adapters/gxg-hub/action-handler.ts:56-60`, `client.ts:8-12, 24-31`) после сохранения `intentId` любая неретраибельная `GxgHubError` от `effects.status` становится `ActionExecutionFailure`. Кандидат уходит в failed, Outcome не появляется уже никогда, хотя Approval может исполнить отправку. Webhook это тоже не восстановит. Это нарушает EFF-28: терминальный исход даёт только статус intent'а.

Под это попадают:
- `internal_error` и `timeout_before_start`: их нет в `errorCodes`, поэтому они превращаются в `invalid_hub_response`;
- `unauthenticated` после ротации токена;
- `rate_limited` с `retry_after_s = 0`.

Исправь:
- После сохранения `intentId` любая ошибка `effects.status` даёт `ActionExecutionDeferred`. Исключение — ошибки, которые постоянны для этого intent'а и имеют явный safe-код: `not_found`, а также `permission_denied` при отозванном grant'е.
- В `client.ts` добавь в `errorCodes` коды `internal_error` и `timeout_before_start`. Учитывай серверный флаг `retryable`. Принимай `retry_after_s = 0`.
- Тесты: `internal_error`, `unauthenticated` и `timeout_before_start` на `effects.status` после propose дают `waiting`, затем следующий успешный статус даёт `Outcome`.

## 2. Переключатель не должен уничтожать очередь `request_human_review@1`

Сейчас (`adapters/postgres-evaluation.ts:596-606`, `application/run-action-workflow.ts:307`) при `SIGNAL_ACTION_HUB_BACKEND=gxg` ожидающие кандидаты `request_human_review@1` с `attempt_count = 0` закрепляются за `dispatcher='gxg'` и сразу терминально падают с `action_handler_unavailable`. Откат на internal их уже не вернёт: dispatcher неизменяем.

Исправь:
- Кандидат, для `action_key` которого у текущего dispatcher'а нет handler'а, не закрепляется и не фейлится. Фильтруй в `listActionWork` или в claim, до записи dispatcher.
- Тест: после включения gxg ожидающий `request_human_review` остаётся `pending` с `dispatcher NULL`. После возврата на internal он исполняется.

## Проверка и отчёт

- В `mastra/` должны быть зелёными `npm run typecheck` и `npm test`. Для typecheck нужен `npm ci` и в `frontend/`.
- Проверка `scripts/verify-meeting-voice-contract.test.ts` нестабильна и на `main`. Не чини её, просто отметь в отчёте.
- Допиши в `docs/plans/m12-signals-gxg-hub.md` раздел «Цикл 2». В нём: что сделано по пп. 1–2, тесты, цифры прогона.
- Minor-замечания критика сейчас не трогай, они в backlog у оркестратора.
