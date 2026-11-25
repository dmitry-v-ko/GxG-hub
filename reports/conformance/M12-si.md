# SI-01…SI-03: перепрогон тестов M12 (gxg_agents_v2) для stage F

status: passed

- Репозиторий `gxg_agents_v2`, ветка `codex/m12-signals-gxg-hub`, коммит `1dcd8a3`; тесты в `mastra/src/mastra/`.
- Прогон 2026-11-24, vitest 4.1.9, `--no-cache`. Интеграционные тесты шли на изолированных локальных PostgreSQL 16 (127.0.0.1:55432 `hub_isolated_test`, 127.0.0.1:55434 `m12_collection_test`), пропущенных нет.
- Итог: 17 файлов, 147 тестов, все `passed`.

| Файл | passed | Требование |
| --- | --- | --- |
| domains/signal-intelligence/adapters/gxg-hub/collection-ingestion.test.ts | 14/14 | SI-01 (AC-M12-01) |
| domains/signal-intelligence/adapters/gxg-hub/collection-driver.test.ts | 6/6 | SI-01 |
| domains/signal-intelligence/adapters/gxg-hub/collection-config.test.ts | 2/2 | SI-01 |
| domains/signal-intelligence/adapters/gxg-hub/collection-retry.integration.test.ts | 1/1 | SI-01 |
| domains/signal-intelligence/adapters/gxg-hub/action-handler.test.ts | 27/27 | SI-02 (AC-M12-02/03/05) |
| domains/signal-intelligence/adapters/gxg-hub/action-persistence.integration.test.ts | 1/1 | SI-02 |
| domains/signal-intelligence/composition-gxg.test.ts | 3/3 | SI-02 (AC-M12-03/05) |
| domains/signal-intelligence/adapters/gxg-hub/client.test.ts | 25/25 | SI-01, SI-02 |
| domains/signal-intelligence/adapters/gxg-hub/config.test.ts | 29/29 | SI-01…03 |
| workflows/signal-intelligence/signal-workers.test.ts | 11/11 | SI-03, потерянный webhook (AC-M12-04) |
| index-gxg.test.ts | 1/1 | SI-03 (AC-M12-04) |
| domains/signal-intelligence/adapters/gxg-hub/webhook.test.ts | 12/12 | SI-03 (AC-M12-06) |
| domains/signal-intelligence/adapters/gxg-hub/runtime-hints.test.ts | 3/3 | SI-03 |
| domains/signal-intelligence/adapters/gxg-hub/runtime-hints.integration.test.ts | 1/1 | SI-03 |
| domains/signal-intelligence/adapters/gxg-hub/postgres-webhooks.test.ts | 6/6 | SI-03 |
| domains/signal-intelligence/adapters/gxg-hub/postgres-webhooks.integration.test.ts | 2/2 | SI-03 |
| server/signal-hub-webhook-routes.test.ts | 3/3 | SI-03 |

Соответствие AC-M12 и SI — в `docs/plans/m12-signals-gxg-hub.md` той же ветки (таблица приёмки). Ветка M12 в `main` gxg_agents_v2 ещё не влита (мерж делает владелец).

Повторить: в `mastra/` с `HUB_ISOLATED_TEST_DATABASE_URL=postgresql://<user>@127.0.0.1:55432/hub_isolated_test` и `M12_COLLECTION_TEST_DATABASE_URL=postgresql://<user>@127.0.0.1:55434/m12_collection_test` выполнить `npx vitest run --no-cache` по файлам из таблицы.
