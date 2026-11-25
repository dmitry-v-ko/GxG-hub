# GxG Hub — Roadmap v1

**Статус:** план. Ни один этап, кроме части этапа A, не начат. Редакция 2026-11-25: упрощённый маршрут с боевыми провайдерами сразу, см. [План реализации](../implementation-plan-v1.md).

**Упрощения редакции 2026-11-25:** один tenant и один агент на первом пути; первый коннектор — Telegram на реальных чатах из allowlist, без fake provider как цели приёмки; критик только на M7 и M13. Неснимаемые инварианты безопасности эффекта и таблица отложенного — в плане реализации. Тесты изоляции двух tenant'ов обязательны **до подключения второго tenant'а**. Этапы B и D в плане пересекаются: Telegram read-only (M5) идёт сразу после M1 и M4.

Подход — **strangler migration**: работающие сервисы GxG Agents V2 продолжают работать, Hub подключается поверх них. Ничего не отключаем и не переносим, пока новый путь не доказан тестами.

## Этапы

Поэтапный план с вехами M0, M1, M4, M5, M7, M9, M13 (упрощённая редакция) и объективными критериями приёмки: [План реализации](../implementation-plan-v1.md).

| Этап | Результат | Обязательные тесты | Данные |
| --- | --- | --- | --- |
| **A. Спеки и схемы** | Согласованные спеки, JSON Schema, smoke-тесты в CI | Schema smoke | Нет |
| **B. Read-only PoC** | ContextForge (pinned digest, PostgreSQL) с двумя tenant'ами и двумя агентами в каждом; reference FastMCP-коннектор; KB через ContextForge после gap-аудита | SC-01…10, MC-01…04 | Только синтетические |
| **C. Effect sandbox** | Approval Service + Executor + Web UI + notify-бот; `gxg-connector-kit`; тестовый `demo.message.propose` на fake provider | EX-01…24, MC-05 | Синтетические |
| **D. Реальные чтения и Signals** | Telegram read-only (разрешённые чаты); Signals собирает через Hub и вызывает `propose` на fake provider | SI-01, SI-02 и повтор B на реальных коннекторах | Реальные, в пределах выданных прав |
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
