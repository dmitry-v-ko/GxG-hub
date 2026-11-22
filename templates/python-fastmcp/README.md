# Шаблон `python-fastmcp`

Шаблон MCP-коннектора GxG ([MCP Toolkit](../../docs/specs/mcp-toolkit-v1.md), веха M1.1), собранный из эталона [`demo`](../../connectors/demo). Движок — [copier](https://copier.readthedocs.io/): `copier update` по `.copier-answers.yml` доносит изменения шаблона до сервисов в других репозиториях.

```bash
scripts/new-connector --name <domain> --lang python [--with-effects]            # connectors/<domain>, кит по path
scripts/new-connector --name <domain> --lang python --dest ../gxg-<domain>-service  # отдельный репозиторий, кит по git
```

Вне репозитория кит (`gxg-connector-kit`) и набор (`gxg-connector-check`) подключаются git-зависимостями на закреплённый commit GxG-hub (`--kit-rev`, по умолчанию `HEAD` — его нужно запушить). Генератор не создаёт credentials, не выдаёт права и не деплоит.

| Что генерируется | Чей файл после генерации |
| --- | --- |
| `src/<pkg>/server.py`, `__main__.py`, `Dockerfile`, `.dockerignore`; вне репозитория ещё `scripts/check-all.sh`, `.github/workflows/ci.yml`, `.gitignore` | шаблона (`copier update`) |
| `gxg-manifest.yaml`, `schemas/`, `src/<pkg>/tools.py` (пример: `<domain>.items.search`, `<domain>.item.get`, с `--with-effects` — `<domain>.message.propose` + внутренний `execute`/`reconcile`), `backend.py` (fake provider: счётчик, 429, недоступность, таймаут, «ответ потерян»), `testctl.py`, `conformance.py` (harness), `fixtures/`, `tests/`, `config.example.env`, `contextforge/registration.example.yaml` (`enabled: false`), `pyproject.toml` (`pytest-timeout`) | сервиса (`_skip_if_exists`) |

Обязательные hooks identity и ресурса — в ките (`gxg_connector_kit.mcp`): handler вызывается только с `Access`, который выдаёт hook. Проверки: `tests/toolkit/test_template.py` (сервис из шаблона проходит свой `check-all` и `check-connectors`; файлы шаблона в `demo` совпадают с рендером).
