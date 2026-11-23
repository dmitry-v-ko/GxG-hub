# gxg-connector-check — `check-connectors`

Общий conformance-набор MCP-коннекторов ([MCP Toolkit](../../docs/specs/mcp-toolkit-v1.md), веха M1.1). Запуск в каталоге коннектора:

```bash
uv run check-connectors --connector . [--report checks.json]
```

Код выхода 0 — только если прошли все применимые проверки. Каждая проверка — тест pytest с ID из [Conformance Matrix](../../docs/specs/conformance-matrix-v1.md); у read-only коннектора проверки EX — `not_applicable` (не запускаются, не `skip`).

| ID | Что проверяется |
| --- | --- |
| MC-01 | Manifest по схеме и lint кита; опубликованы ровно публичные tools manifest, со своими файлами схем; глагол соответствует классу |
| MC-02 | Результаты по output schema и в пределах `max_bytes`; у списка есть `max_results`, `limit` ограничен им, больше — `invalid_input`; пустой результат — успех; `limit=1` даёт `next_cursor`, следующая страница — следующий элемент, курсор другого principal — `invalid_input` |
| MC-03 | 429 → `rate_limited` с `retry_after_s`; недоступность и таймаут до старта → `retryable`; harness обязан уметь `rate_limited`, `temporarily_unavailable`, `crash` |
| MCC-05 | Любой отказ — tool error с envelope; неожиданное исключение — `internal_error` без своего текста |
| MCC-09 | `/healthz`; `/readyz` 200 с bundle, 503 без него и без причин, 503 при недоступном backend; без bundle нет вызова провайдера. С эффектами — ещё JWKS |
| SC-02 | Без identity, истёкшая, чужой audience → `unauthenticated` |
| SC-03 | tenant/principal в аргументах, заголовках и `_meta` отклоняются или игнорируются |
| SC-04 | Чужая подпись, повтор → `unauthenticated`; ни одного вызова провайдера до проверки identity |
| SC-05 | Скрытый, чужой и несуществующий ресурс — один и тот же `not_found` побайтно; и в list-tool с фильтром `*_ids` |
| SC-09 | Credential провайдера, токены identity и свободный текст вызывающего (запрос, сообщение) не попадают ни в ответы, ни в логи уровня INFO |
| SC-10 | Без grants — пусто или `not_found` и ноль вызовов; успешный вызов виден в счётчике (положительный контроль); читатель видит только разрешённое; другой tenant — ничего |
| EX-01…EX-16 | `execute` не в MCP; `approved` не даёт полномочий; execute без/с плохим токеном и на цель, которую коннектор не обслуживает; повтор отклоняется без второго `dispatch`; подмена args, потерянный ответ, отозванный grant, недоступный Approval Service |

Не здесь (нужен стенд с ContextForge, `stand/`): SC-01, SC-06, SC-07, MC-01 через ContextForge, MC-04, MC-05.

## Как коннектор подключается

В `pyproject.toml` коннектора — `[tool.gxg-connector-check] harness = "<pkg>.conformance:harness"`. `harness()` возвращает `gxg_connector_check.Harness`: каталог, класс `Settings`, функцию запуска (`start(settings, wiring) -> Running`: ASGI-приложения, счётчик вызовов провайдера, режимы отказа), principals и по одному вызову на tool. Tool, который по праву отвечает только оператору (маска на весь тип ресурса), помечается `Call(..., caller="operator")`, а principal оператора — `Harness.operator`: набор вызывает такой tool от оператора и проверяет, что reader и principal без grants получают отказ (SC-10). Остальное набор строит сам: подписанный тестовый bundle из `fixtures/grants/`, тестовый issuer identity, фейковый Approval Service с ключом executor'а.

Сервис в отдельном репозитории подключает набор git-зависимостью на тот же закреплённый commit GxG-hub, что и кит (так делает `scripts/new-connector --dest <вне репозитория>`).

## Доказательство, что набор ловит ошибки (AC-M1.1-02)

`tests/broken/demo/*.patch` — 18 испорченных копий `demo`: первые 10 краснеют каждая на своей проверке, остальные — положительные контроли тех же проверок; точный набор красных проверок каждой копии зафиксирован в `tests/test_broken.py`. `tests/broken/sample/*.patch` — 5 копий сгенерированного `sample --with-effects` для EX (`tests/toolkit/test_template.py`). Все патчи пересоздаёт `tests/broken/make_patches.py`.

`bundle.py` повторяет `gxg-policy build` для grants без групп (`gxg_policy` не импортируется из git-wheel вне репозитория); `tests/test_units.py` сверяет байты с `gxg-policy build`.
