# M10 — Webhooks и эксплуатация: готова (порог 9)

Ветка `claude/m10-ops`, база `origin/claude/integration` `40cec84`. Мерж в `main` делает человек; PR не создан.

| Что | Commit |
| --- | --- |
| Код, оценённый критиком цикла 3 (9.0) | `3e5541c` |
| Доказательства на нём (`stage-C.json`, `M10.json`, JUnit стенда) | `7da6e46` |
| Отчёт критика цикла 3 | `d75ab91` |

## История оценок

| Оценка | Кто | Отчёт |
| --- | --- | --- |
| 7.5 | критик цикла 1 (major: немонотонный `gxg_executions_total`, повтор подписанного события отклонялся, отзыв passkey и др.) | `reports/critic/M10-cycle-1.json`, `.review.md` |
| 5.0 | критик цикла 2 (кэп 5: AC-M10-05 — `/metrics` отвечал 500 после истечения по 429; major: алерт `GxgExpiredAfterRateLimit` не гас) | `reports/critic/M10-cycle-2.json`, `.review.md` |
| **9.0** | критик цикла 3 (чек-лист цикла 2 выполнен, без кэпов, все AC `passed`) | `reports/critic/M10-cycle-3.json`, `.review.md` |

Облегчённый процесс (решение владельца 2026-11-23): один критик на цикл, оценка ≥ 9 при всех AC `passed` закрывает веху, отдельного финального критика нет.

## AC

AC-M10-01…08 — `passed` (перезапуск критиком цикла 3).

- AC-M10-06 (MC-05): у ContextForge нет minor новее v1.1.1. FastMCP 4.2.4 проходит. Пины не менялись (`reports/conformance/M10-mc05.md`).
- AC-M10-07: `reports/conformance/stage-C.json` — EX-01…EX-25 и MC-05 `passed` (26/26) на `3e5541c`. Стенд на `3e5541c`: fresh 54/0, after-restart 54/0, after-recreate 54/0.

## ci_local

- Цикл 3 на `3e5541c`:
  - pytest approval/kit/demo/ui/notifier/deploy/tests: 1694 passed;
  - ruff и mypy чистые; gitleaks утечек не нашёл;
  - стенд ×3 зелёный.
- Полный `scripts/ci/check-all.sh` в последний раз шёл в цикле 2, на `2a52ca3`. Все шаги прошли, включая стенд после `down -v`. Шаг `mutmut.sh` по облегчённому процессу заменён прогонами только по изменённым файлам модулей безопасности.
- mutmut: `reports/mutmut/M10-*.txt`, `M10-cycle-3.txt`. Ни одного выжившего вне списка эквивалентных.
- Подробно: `reports/conformance/M10.json` (`ci_local`, `self_check`).

## Открыто, вне оценки

- Решения владельца по умолчанию и остаточные риски AS-30 R1, R2: `reports/M10-PENDING-OWNER.md` (`pending_owner_decision`).
- Minor-находки N6–N11 перенесены в `docs/backlog.md`.
- Полный прогон mutmut делается один раз, перед мержем в `main` (облегчённый процесс, п. 1).
- `test_keys_cli_rotate_remove_and_release` один раз упал в полном прогоне цикла 3. Отдельно он проходит, и до изменений цикла, и после. В следующем полном прогоне он тоже прошёл. Похоже на нестабильный тест, причина не найдена.
