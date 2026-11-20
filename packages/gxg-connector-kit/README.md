# gxg-connector-kit

Тонкая общая библиотека коннекторов GxG Hub ([MCP Toolkit](../../docs/specs/mcp-toolkit-v1.md), веха M1). Здесь только то, что иначе дублировалось бы в каждом коннекторе и где ошибка означает дыру в безопасности. Криптография — `cryptography` (Ed25519) и PyJWT. Своей криптографии нет.

| Модуль | Что делает | Требования |
| --- | --- | --- |
| `canonical` | `normalize`, `dumps` (JCS-байты), `hash` (SHA-256). NFC, запрет float, целые ±(2⁵³−1) | EFF-05 |
| `errors` | `GxgError` → `to_dict()` = `structuredContent` по `error.schema.json` | MCC-05 |
| `svc_jwt` | `sign()`, `SvcJwtVerifier.verify(token, allowed_issuers=…)` → `Service`; `Service.serves(tenant)` | AS-06, AS-25 |
| `exec_token` | `ExecTokenVerifier` + `ExecutorJwks` (кеш ≤ 10 мин, только `kid` из JWKS, `readiness()`) | EFF-18 п.1, AS-09…AS-11, MCC-09 |
| `grants` | `BundleStore`: загрузка и проверка подписи bundle, `GrantResolver` (`check`, `allowed`, `version`), `readiness()`, `registry()` | GR-01…GR-17, GR-11a |
| `identity` | `IdentityVerifier` (Protocol), `VerifiedIdentity`; `StubIssuer`/`StubVerifier` — **только** при `GXG_PROFILE=test` | ADR-0001, SEC-03 |
| `redact` | `redact(record)`: удаляет `text`, `body`, `query`, `args`, `token`, `authorization` на любой глубине, маскирует JWT в строках | SEC-18 |
| `readiness` | `Readiness`, `all_ready()` для `/readyz` | GR-12, GR-13 |

## Конфигурация сервиса

Секретов в конфигурации нет: только публичные ключи и ID.

- **Ключи проверки bundle:** `{kid: Ed25519PublicKey}`. Берутся из конфигурации, а не из bundle (GR-11a). Строку `ed25519:<base64url>` разбирает `gxg_connector_kit._jwt.parse_public_key`.
- **Свой ID** (`aud`) для `ExecTokenVerifier` и `SvcJwtVerifier`.
- **JWKS executor'а:** функция `fetch() -> bytes`, которая читает `GET /.well-known/gxg-exec-jwks.json`. HTTP-клиент выбирает сервис.
- **`tenant`** для `BundleStore`, если экземпляр работает на один tenant (GR-12).

## Поведение при отказе (fail closed)

- Нет валидного bundle или он старше 24 ч по `built_at` → `readiness()` = not ready, `check`/`allowed`/`registry()` бросают `temporarily_unavailable`.
- Неудачная перезагрузка оставляет предыдущий валидный bundle. Bundle с `built_at` старше загруженного отклоняется: это защита от отката к отозванному праву (GR-13a). Bundle с `built_at` позже `now + 5 с` тоже отклоняется: иначе он заблокировал бы все следующие bundle как «откат».
- Обновить JWKS не удалось после 10 минут → ни один `kid` не принимается, `ExecutorJwks.readiness()` = not ready.
- Любой отказ токена → `GxgError("unauthenticated", "token rejected")`. Причина лежит в `TokenError.reason` для внутренних логов, вызывающему она не отдаётся.
- Реального `IdentityVerifier` нет до спайка M4. Вне тестового профиля коннектор identity не получает.

## Тесты

`uv run pytest packages/gxg-connector-kit/tests`. Ключи генерируются во время теста. Покрытие ветвей: `scripts/ci/check_coverage.py` (AC-M1-09). Мутационная проверка (DoD-4): `uv run python scripts/ci/mutate_kit.py --out <file>`, ~12 минут, не в CI.
