# M11 цикл 4 — план

Закрытый чек-лист: `required_improvements` из [`reports/critic/M11-cycle-3.json`](../critic/M11-cycle-3.json) (оценка 8.0, commit `4ebe3ec`). Последний цикл до эскалации (порог 9).

| # | Пункт критика | Что сделано |
| --- | --- | --- |
| 1 | F1: ложное «чисто» у `--drift` на миграции > 64 KiB (`grep -q` + `pipefail` → SIGPIPE) | `da3e25f`: `grep -E … >/dev/null` читает весь дифф; случай «совпадение + 20000 строк заполнения» в `test_upstream_drift_check_fails_on_a_new_archive_migration` — падает на старом скрипте, проходит на новом. Заодно `search_path` (F4) |
| 2 | F3: `stand test_ac_m11_04[1]` зависит от времени вызовов | Отсрочка выставляется заново перед каждым вызовом: `seconds` перед `collect` (проверка `retry_after_s`), 3600 с перед чтениями (проверяется только флаг) |
| 3 | F2: `M11.json` не из одного полного прогона | Один `scripts/ci/check-all.sh` на финальном commit с клоном gxg_agents_v2 (`../gxg_agents_v2`), без `UPSTREAM_DRIFT=skip`; `M11.json`: `commit`, `ci_local`, счётчики из этого прогона |
| 4 | F4–F6 в backlog | `docs/backlog.md`, строки M11 (F4, F5 сделаны в `da3e25f`) |

Также (самопроверка, не из чек-листа): `test_duration_is_in_milliseconds` ждёт, пока вызов встанет на блокировку, а не 0.3 с (`b5f5b00`): под нагрузкой mutmut два эквивалентных мутанта ложно считались убитыми.

## DoD-6

Новых компонентов нет.
