# M5 — готово к независимому финальному критику

Веха: M5 «Read-only PoC через ContextForge», ветка `claude/m5-readonly-poc`.

- **Проверенный commit:** `d39a1ff` (код — `9add475`, сверху только отчёт), отчёт критика цикла 2 — `e4ba7bd`.
- **Оценка критика:** 9.5 из 10 (порог 9), critical и major нет. Отчёты: [`reports/critic/M5-cycle-2.json`](critic/M5-cycle-2.json), [`reports/critic/M5-cycle-2.review.md`](critic/M5-cycle-2.review.md).
- **Отчёт прогона:** [`reports/conformance/M5.json`](conformance/M5.json) — SC-01…SC-10, MC-01…MC-04 через ContextForge, 29 тестов, зелёные на свежем стенде, после рестарта и после пересоздания всех контейнеров.
- **GO/NO-GO:** GO, [Roadmap](../docs/specs/roadmap-v1.md#решение-go-2026-11-21-m5). Условие 4 ADR-0002 выполнено, переход на ToolHive не нужен.

## История оценок

| Цикл | Commit | Оценка | Итог |
| --- | --- | --- | --- |
| 1 | `c01c5c3` | 8.0 | 5 major: общий пароль агентов, bundle стенда истекает через 24 ч, SC-10 проверял rollback вместо 24 ч, пути SSE без тестов, неизвестный `kid` не виден в аудите |
| 2 | `d39a1ff` | **9.5** | чек-лист цикла 1 выполнен; 3 minor (F11–F13) → `docs/backlog.md` |

## Открыто

- **CI на голове.** Прогон 44932350759 на `d39a1ff` не получил раннеров: все job упали за 2–3 с без шагов, в ту же минуту так же упал прогон ветки M7. Критик воспроизвёл все job локально — зелёные. Перед PR в main нужен зелёный CI на голове. Job `grants-widening` на push без PR красный по дизайну M2.
- **Вопросы владельцу** (работу не блокируют, `pending_owner_decision`): [`reports/M5-ESCALATION.md`](M5-ESCALATION.md) #1–#4.
