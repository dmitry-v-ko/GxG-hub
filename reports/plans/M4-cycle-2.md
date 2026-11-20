# M4 cycle 2 — plan (ponytail full)

Вход: отчёт критика `reports/critic/M4-cycle-1.json` (8.0, три major) и `.review.md`. Задача — 8 `required_improvements` и дешёвые minor. Новых возможностей нет.

## Что проверено в образе v1.1.1 до кода

- `cpex/framework/settings.py`: `fail_on_plugin_error` читается только из env `PLUGINS_FAIL_ON_PLUGIN_ERROR` (по умолчанию `false`); блок `plugin_settings` в YAML игнорируется.
- `tool_service.py`: upstream вызывается с `tool_name_original` (= `original_name` tool'а), даже если плагин меняет `payload.name`. В плагине тот же `original_name` доступен как `global_context.metadata["tool"].original_name` (`mcpgateway.common.models.Tool`). Его и подписываем: это имя, которое коннектор видит в `params.name`.
- `config.py`: `mcpgateway_direct_proxy_enabled` по умолчанию `false`. `resources/read` и `prompts/get` идут через `RESOURCE_PRE_FETCH` / `PROMPT_PRE_FETCH`, не через `tool_pre_invoke`.

## Scope

| # | Часть | Файлы | Ступень лестницы |
| --- | --- | --- | --- |
| 1 | Fail closed в ContextForge | `compose.variant-c.yaml` (`PLUGINS_FAIL_ON_PLUGIN_ERROR`, `MCPGATEWAY_DIRECT_PROXY_ENABLED=false`, путь к plugins.yaml через переменную), `spike.py plugin_config` (без `plugin_settings`), `run_spike.sh` (этап «нет ключа») | одна строка env; этап — `sed` по готовому plugins.yaml + существующие `wait_up`/`spike.py`; контроль с `PLUGINS_FAIL_ON_PLUGIN_ERROR=false`, чтобы этап доказывал, что ловит |
| 2 | JWS привязан к tool | `plugin.py` (claim `tool`), `verify_jws.py` (сравнение с `params.name`) | одна строка в каждом |
| 3 | Мутации | `spike.py replay` (`replay_other_connector`, `replay_other_tool`), `verify_jws.py` (разделить срок и TTL, kid отдельно от alg), `mutate_verify_jws.py` (aud, tool, ttl, iss, kid, sub-в-tenant) | существующий harness; подписанные ключом адаптера синтетические строки добавляются к копии лога, без нового фреймворка |
| 4 | TEN-01 | `docs/specs/target-platform-v1.md` | одна фраза |
| 5 | direct_proxy, resources/prompts | ADR, SEC-03 | текст |
| 6 | passthrough-misconfig | удалить этап, overlay, 2 файла evidence; ADR критерий 1 | удаление |
| 7 | Вердикт | `M4.json`, ADR | текст |
| 8 | Перепрогон | `run_spike.sh` → `evidence/`, `M4.json` | — |
| minor | Ключ адаптера доступен процессу ContextForge; principal = local part email (переиспользование при пересоздании) | ADR («Минусы» C, «Остаток риска»), SEC-03 | текст |
| — | SEC-02/SEC-03 | после подтягивания `security-v1.md` из ветки оркестратора мои SEC-02/SEC-03 оказались откатаны к VERIFY: переношу их поверх, с правками пунктов 1, 2, 5 | текст |

## Не делаем, сознательно

- Shared store `jti` в спайке: в `verify_jws.py` один процесс. Требование — в SEC-03, реализация — `IdentityVerifier` (M1).
- Хуки `resource_pre_fetch` / `prompt_pre_fetch` в плагине: коннекторы отдают identity-зависимые данные только через tools, остальное коннектор отклоняет без identity (fail closed). Расширять адаптер — когда понадобится resource с правами.
- Claim `method`: хук срабатывает только на `tools/call`, верификатор принимает только `tools/call`.
- Переход на RFC 8785 (JCS): обе стороны на Python, канонический JSON определяется как конкретный `json.dumps(...)`; расхождение с JCS для чисел с плавающей точкой записано в SEC-03.
- Unit-тест фильтра заголовков плагина вместо passthrough-этапа: на v1.1.1 путь недостижим, защищает подпись.
- `chmod 644` ключа на стенде оставляю (нужен non-root пользователю образа); production — secret store, записано.
