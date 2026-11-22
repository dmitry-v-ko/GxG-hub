# M9 — Web UI и notify-бот: готова (порог 9)

Ветка `claude/m9-ui-bot`, база `origin/claude/integration` `3c2f0b0`.

| Что | Commit |
| --- | --- |
| Код, оценённый финальным независимым критиком (9.0) | `40bc8a5` |
| После него: только `timeout_constant` в `[tool.mutmut]` connectors/demo и отчёты/бэклог | `53a1631` и далее |

## История оценок

| Оценка | Кто | Отчёт |
| --- | --- | --- |
| 6.0 | критик цикла 1 (critical: токен бота в логах httpx; DoD-4: два неэквивалентных «эквивалентных» мутанта; major: непротестированный `ui cli.main`, одна битая preview-строка роняла весь список) | `reports/critic/M9-cycle-1.json`, `.review.md` |
| 9.0 | критик цикла 2 (чек-лист цикла 1 выполнен; условие — зелёный `mutmut.sh`) | `reports/critic/M9-cycle-2.json`, `.review.md` |
| **9.0** | финальный независимый критик (оркестратор), `check-all` EXIT=0 на `40bc8a5` | `reports/critic/M9-final.json`, `.review.md` |

## ci_local (DoD-1, решение владельца 2026-11-21)

- Финальный критик: `scripts/ci/check-all.sh` на `40bc8a5` — **OK, EXIT=0** (129 мин), `mutmut.sh` без выживших.
- Прогон владельца вехи на `40bc8a5`: все шаги зелёные, `mutmut.sh` — 0 выживших, но exit 1 из-за 3 мутантов «timeout»: два в `approval` `transitions.x_rules` при отдельном прогоне убиты; третий — `connectors/demo` `server.x_build__mutmut_9` (M3, M9 его не трогала): при ручном применении мутации тесты demo падают за 1 с, а под mutmut прогон зависает; в прогоне финального критика и в прогоне на `aaa770c` он убит. `timeout_constant = 60` в `connectors/demo` (`53a1631`) этого **не** исправил (повторный прогон завис) — причина не найдена, записано в backlog. На `aaa770c` тот же прогон нашёл 5 выживших в `intents.py` — закрыты тестами в `40bc8a5`.
- Подробно: `reports/conformance/M9.json` (`ci_local`, `self_check`).

## AC

AC-M9-01…12 — `passed` (перезапуск критиками). AC-M9-01 «затем `succeeded`» — через функции ролей executor'а (M8 не на базе; решение владельца 5).

## Открыто, вне оценки

- Компрометация `ui` (решение за любого approver'а из замороженного набора, подделка step-up) — принято владельцем до M10 (AS-30, AC-M10-08); `reports/M9-ESCALATION.md` (вопросы решены).
- Backlog: `docs/backlog.md`, строки M9 (cycle-1 F8, F10–F12, outbox notifier; cycle-2 N1–N4; final F-1…F-7).
- GitHub CI не проверялся (нет минут Actions); `grants-widening` красный на push без PR по дизайну M2.
