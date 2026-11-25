# M4 cycle 1 — plan (ponytail full)

Задача: протокол спайка из ADR-0001 на закреплённом ContextForge с PostgreSQL. Timebox 3 дня, это первый цикл.

## Что уже известно до стенда

- Docker в контейнере есть (демон запускается вручную), `ghcr.io` доступен: стенд возможен.
- Кандидат релиза: `v1.1.1`, последний релизный тег в `ghcr.io/ibm/mcp-context-forge`; `latest` указывает на тот же index digest. Тега `1.1.1` без `v` нет, значит текущий `.env.example` не работает.
- По исходникам v1.1.1 (`mcpgateway/utils/identity_propagation.py`): HMAC-SHA256 только над `user_id:email`, без teams, времени, nonce, метода, тела; секрет по умолчанию = `JWT_SECRET_KEY`. Ожидание: вариант A проваливает критерии 1 (tenant не подписан) и 3 (повтор). Это гипотеза, её проверяет стенд.

## Scope

| Часть | Файлы | Ступень лестницы |
| --- | --- | --- |
| Стенд | `spikes/m4-contextforge/compose.yaml`, `.env` (git-ignored, генерируется) | готовые образы ContextForge и PostgreSQL по digest; свой код — нет |
| Echo-MCP-сервер | `spikes/m4-contextforge/echo/echo_mcp.py` | stdlib (`http.server`, JSON-RPC streamable HTTP, JSON-ответы): без SDK и без сборки образа, запуск в `python:3.12-slim` по digest |
| Сценарий спайка | `spikes/m4-contextforge/run_spike.sh` + `spike.py` | stdlib `urllib`; teams, агенты, токены, gateway, virtual server, алиас через Admin API; вызовы по 3 путям; атаки 1–4 |
| Проверка HMAC (сторона коннектора) | `spikes/m4-contextforge/verify_hmac.py` | stdlib `hmac`; повторяет формулу из исходника, чтобы проверить подпись захваченных заголовков |
| Вариант C (если A не проходит) | `spikes/m4-contextforge/plugin/` | минимальный плагин `tool_pre_invoke`, добавляет JWS Ed25519; проверка на стенде по тем же 5 критериям, насколько успеет timebox |
| Доказательства | `spikes/m4-contextforge/evidence/` | захваченные заголовки и логи после редактирования секретов (подписи и токены заменяются на `<redacted:len>`) |
| ADR | `docs/adr/0001-identity-propagation.md` | статус, результаты 5 критериев, ссылки на код и advisories |
| VERIFY → конкретика | `docs/specs/security-v1.md` (SEC-02, SEC-03), `target-platform-v1.md` (TEN-01), `resource-grants-v1.md` (GR-пример) | текст, по одному коммиту на пометку |
| Закрепление | `compose.yaml`, `.env.example` | `image@sha256:...` |
| Отчёт | `reports/conformance/M4.json` | формат M0 |

## Не делаем, сознательно

- `IdentityVerifier` в `gxg-connector-kit` — это M1/M5 (протокол ADR, шаг 6, относится к реализации, не к AC-M4).
- Production-качество плагина C: только прототип, чтобы доказать, что хук срабатывает на всех путях.
- Тест MC-05 (обновление ContextForge) не существует до этапа C: критерий 5 оценивается разбором кода и фиксируется как `not_run` для живого прогона, если нет второй версии для апгрейда.
- Автотесты в `tests/` для спайка: AC-M4 — результаты в ADR, а не код в `main`; скрипт спайка сам содержит assert'ы.
