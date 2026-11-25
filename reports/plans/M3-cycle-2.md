# M3 cycle 2 — plan (ponytail full)

Основа: [critic cycle 1](../critic/M3-cycle-1.review.md), оценка 8.0, два major. Спеки и AC не меняются.

| # | Замечание | Что делаем | Файлы |
| --- | --- | --- | --- |
| 1 | GR-12 / SC-10 / SC-05: без bundle часть вызовов отвечает | в `DemoTool.run` после `refresh()` и до handler читаем `store.current` (кит сам бросает `temporarily_unavailable`). Тест: без bundle три tool × nobody/reader/неизвестный tenant × существующий/отсутствующий id → одно и то же тело, backend = 0 | `tools.py`, `test_demo.py` |
| 2 | GR-16 / MCC-06: пустой набор досок доходит до backend | `notes_search`: пустой набор → пустой результат без `backend.search`. `test_nobody_does_not_reach_the_backend` проверяет `sum(calls) == 0`, плюс `board_ids: []` и `index_failure` | `tools.py`, `test_demo.py` |
| 3 | исключение не `GxgError` уходит агенту текстом | `except Exception` → `internal_error` без текста исключения, `outcome` в аудите; тест с `RuntimeError("…secret…")` | `tools.py`, `test_demo.py` |
| 4 | импорт `gxg_connector_kit._jwt` | `TokenError`, `parse_public_key` в публичном `gxg_connector_kit/__init__`; тесты кодируют ключ через `cryptography` | kit `__init__.py`, `server.py`, `test_demo.py` |
| 5 | схемы и таймауты в коде и в manifest | один источник — `gxg-manifest.yaml` (PyYAML уже стоит через FastMCP; объявляем прямой зависимостью); в коде только `name → handler` | `tools.py`, `pyproject.toml`, `uv.lock` |
| 6 | mutmut не покрывает `server.py`, `DEMO_STATE_FILE` не проверен | `server.py` в `only_mutate`; тест на `DEMO_STATE_FILE`; прочие выжившие — в `mutmut-equivalent.txt` с причиной | `pyproject.toml`, `test_demo.py`, `scripts/ci/mutmut-equivalent.txt` |
| 7 | `testctl.FAILURES` копирует `IndexFailure` | `typing.get_args(IndexFailure)` | `testctl.py` |

Проверка: `uv run pytest` (корень и `connectors/demo`), `scripts/ci/mutmut.sh`, CI на push, обновить `reports/conformance/M3.json` и `reports/mutmut/M3.txt`.
Не делаем: `mask_error_details` FastMCP — свой `except Exception` уже даёт конверт MCC-05 и аудит.
