# GxG Hub — Roadmap v1

**Статус:** план. Ни один этап, кроме части этапа A, не начат. 2026-11-20.

Подход — **strangler migration**: работающие сервисы GxG Agents V2 продолжают работать, Hub подключается поверх них. Ничего не отключаем и не переносим, пока новый путь не доказан тестами.

## Этапы

Поэтапный план с вехами M0–M14 и объективными критериями приёмки: [План реализации](../implementation-plan-v1.md).

| Этап | Результат | Обязательные тесты | Данные |
| --- | --- | --- | --- |
| **A. Спеки и схемы** | Согласованные спеки, JSON Schema, smoke-тесты в CI | Schema smoke | Нет |
| **B. Read-only PoC** | ContextForge (pinned digest, PostgreSQL) с двумя tenant'ами и двумя агентами в каждом; reference FastMCP-коннектор; KB через ContextForge после gap-аудита | SC-01…10, MC-01…04 | Только синтетические |
| **C. Effect sandbox** | Approval Service + Executor + Web UI + notify-бот; `gxg-connector-kit`; тестовый `demo.message.propose` на fake provider | EX-01…24, MC-05 | Синтетические |
| **D. Реальные чтения и Signals** | Telegram read-only (разрешённые чаты), затем расширения `ext.*` только `read` ([MCC-11](mcp-connector-contract-v1.md#расширения)); Signals собирает через Hub и вызывает `propose` на fake provider | SI-01, SI-02, MC-06…MC-10 и повтор B на реальных коннекторах | Реальные, в пределах выданных прав |
| **E. Первый реальный эффект** | `telegram.message.propose` включён для одного tenant'а и узкого allowlist'а чатов, только с решением человека | OPS-01, OPS-02, повтор EX на реальном коннекторе | Реальные |
| **F. Production-кандидат** | Мониторинг, backup/restore, incident response, ротация секретов, политика хранения аудита, процедура изменений; клиенты-tenant'ы | OPS-03…05, полный прогон | Реальные |

После F: CRM, публикации, A2A-исполнители.

## Этап A — что осталось

- [x] Переписанные спеки (этот набор).
- [x] [Модель прав на ресурсы](resource-grants-v1.md) + `resource-grants.schema.json` + пример.
- [x] [Спека reference-коннектора `demo`](demo-connector-v1.md).
- [ ] Спайк ContextForge по [ADR-0001](../adr/0001-identity-propagation.md): механизм identity, формат principal ID, teams, `unified_pdp`. Это работа на стенде, а не документ: итог записывается в ADR и [Security](security-v1.md). Блокирует подключение коннекторов через ContextForge, но не их локальную разработку.
- [x] [Approval Service](approval-service-v1.md): модель данных, переходы T1–T18, [OpenAPI](../../schemas/approval-service.openapi.yaml), execute/reconcile, service JWT, UI, бот, webhooks, хранение, администрирование, метрики.
- [x] [Политика согласования](approval-policy-v1.md), квоты `propose`, формат `preview` (EFF-30…33).
- [x] Схемы `execution-token`, `intent-status`, `intent-create`, `approval-decision`, `execute-request`, `reconcile-result`, `intent-event`, `preview`, `error`, `gxg-manifest`, `approval-policy`, `platform-registry`; тестовые векторы канонизации.
- [ ] CI (GitHub Actions): schema smoke-тесты, lint и diff-отчёт grants (GR-09).

## Этап B — критерии приёмки PoC

- [ ] Списки tools у двух агентов и двух tenant'ов различаются согласно политике.
- [ ] Прямой вызов запрещённого или скрытого tool'а отклоняется сервером.
- [ ] Доступ к чужому tenant'у, документу или чату отклоняется до обращения к backend.
- [ ] Коннектор получает проверяемую identity от ContextForge и отклоняет вызов без неё.
- [ ] Поля из аргументов и заголовков не влияют на права.
- [ ] Аудит связывает вызов с principal, tenant, tool и результатом без содержимого сообщений.
- [ ] После рестарта политики и connections восстанавливаются.
- [ ] Существующие KB, Signals и Voice работают без изменений.

## Позже, не сейчас

- Общий `hub.search_tools` и code mode на уровне Hub: возвращаемся, когда у одного агента становится ~30–50 tools. До порога хватает ядра и расширений на уровне коннектора.
- Эффекты через расширения: не раньше этапа E, отдельными `propose`-tools; `ext.call` остаётся только `read`.

## GO / NO-GO

Решение принимается в конце этапа B.

**GO**, если на закреплённой версии ContextForge подтверждены:
1. изоляция tools и servers по team (tenant);
2. проверяемая передача identity (tenant + principal) коннектору;
3. запрет вызова скрытых tools;
4. правила `unified_pdp` deny-by-default работают на всех путях вызова.

**Если не подтверждено 2:** обходной путь — отдельный экземпляр коннектора на tenant с собственным service credential. Если это неприемлемо по стоимости — сравниваем с [agentgateway](https://agentgateway.dev/) и минимальным собственным access-адаптером.

**Если не подтверждено 1 или 3:** NO-GO для ContextForge как многоарендного шлюза; пересматриваем выбор шлюза до этапа C.

Путь эффектов (этап C) от ContextForge почти не зависит, поэтому его можно строить параллельно с решением GO/NO-GO.

### Решение: GO (2026-11-21, M5)

**GO для ContextForge `v1.1.1@sha256:16c7a898…56a5` как многоарендного шлюза этапа B.** Все четыре условия подтверждены на dev-стенде M5 ([stand/README.md](../../stand/README.md)) тестами SC-01…SC-10 и MC-01…MC-04 через ContextForge. Тесты прошли трижды: на свежем стенде, после рестарта всех контейнеров и после их пересоздания. Отчёт — [reports/conformance/M5.json](../../reports/conformance/M5.json).

| Условие | Результат | Тест |
| --- | --- | --- |
| 1. Изоляция tools и servers по team | Агент видит в `tools/list` (`/rpc`, `/sse`, virtual server по MCP и SSE) только tools своей team. Tool чужой team по имени отклоняется на `/rpc`, `/mcp`, `/sse`, своём и чужом virtual server (MCP и SSE), до коннектора | `test_sc01_*` |
| 2. Проверяемая identity коннектору | Вариант C: `X-GxG-Identity` (EdDSA), `ContextForgeVerifier` кита. Подделка, повтор, чужой ключ, `aud`, `iss`, tool, аргументы, истёкший — `unauthenticated` до backend'а. Identity из аргументов, заголовков и `_meta` агента не действует | `test_sc03_*`, `test_sc04_*`, `test_mc04_*` |
| 3. Запрет вызова скрытых tools | Скрытый tool (чужой team, исходное имя после алиаса, `*execute*`) по имени не вызывается | `test_sc01_*`, `test_sc08_*` |
| 4. `unified_pdp` deny by default на всех путях | Встроенный `unified_pdp` (движок `native`, `default_decision: deny`). Вызов без правила allow → `PDP_DENY` на `/rpc`, virtual server, алиасе и SSE (`/sse`, `/servers/{id}/sse`; там отказ приходит пустым `result: {}` без причины, вызов до коннектора не доходит). Глобальный `/mcp` отказывает team-scoped токену всегда. Правило запрета `*execute*` бьёт ошибочный allow | `test_sc01_hidden_tool_and_default_deny_on_every_path` |

Запасной вариант ADR-0002 (ToolHive) не нужен. Известные ограничения v1.1.1, не влияющие на GO: ContextForge сам проверяет аргументы по `inputSchema` и отвечает `-32603` вместо `invalid_input`. Отзыв токена агента действует через `AUTH_CACHE_REVOCATION_TTL` (≥ 5 с). Вопросы владельцу — [reports/M5-ESCALATION.md](../../reports/M5-ESCALATION.md). Для production версия одобряется отдельно (SEC-22): нужны secret store для ключа адаптера, общий store `jti`, правила `unified_pdp` из реестра платформы.

## Порядок миграции сервисов

1. **Инвентаризация:** владелец данных, хранилище секретов, runtime и потребители KB, Telegram, Signals, Voice.
2. **KB:** gap-аудит существующего MCP → подключение через ContextForge → wrapper, если нужен.
3. **Telegram:** фасад к существующему сервису, только чтение.
4. **Signals:** сначала сбор через Hub, затем действия через `propose`. Старый путь работает, пока новый не протестирован; один dispatcher на кандидата.
5. **Voice:** подключает нужные tools как обычный агент.
6. **Эффекты:** по одному tool'у и tenant'у, начиная с узкого allowlist'а.
7. **Вывод старого Capability Hub:** только после параллельной проверки, backout-плана и подтверждения, что все потребители переведены.

Production-миграции и доступ к секретам согласуются отдельно.

## Связь со старым проектом

[Agent Capability Hub — Our Proposal](https://github.com/dmitry-v-ko/gxg_agents_v2/blob/main/docs/specs/platform/capability-hub/agent-capability-hub-our-proposal.md) и [Capability Hub v3.0](https://github.com/dmitry-v-ko/gxg_agents_v2/blob/main/docs/specs/platform/capability-hub/README.md) — история требований и источник для анализа миграции. Старую реализацию не копируем.
