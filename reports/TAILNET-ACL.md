# Политика Tailscale: готово офлайн

Ветка `claude/m13-tailnet-acl`. Сеть Tailscale/Railway не использовалась, секретов нет, PR не создан.

## Что сделано
- `policies/network/tailscale-acl.hujson` — `tagOwners` (4 тега, `autogroup:admin`), `groups` (пустые, примеры в комментарии), `acls` ровно по таблице ADR-0003, `nodeAttrs: []` (без `funnel`), `tests`: 4 разрешения (6 пар accept) и deny для каждого тега.
- `scripts/network/check_acl.py` — разбор HuJSON, теги из `TS_TAGS` в `services.yaml` есть в `tagOwners`, нет `*`/`autogroup:internet`/funnel/`autoApprovers`, на каждую пару src→dst есть accept, на каждый тег есть deny, тесты согласуются с правилами (мини-вычислитель). Подключён в `scripts/ci/check-all.sh`.
- `tests/network/test_check_acl.py` (11 тестов, в `testpaths`; mypy --strict) — пройдено.
- `docs/runbooks/tailnet-acl.md` — применение, ключи (один ключ — один тег), проверки AC-M14-04/08/10.

## Проверено
`check_acl: OK`, pytest, ruff, mypy --strict. Полный `check-all.sh` не гонял (нужны Docker и др.).

## Что важно владельцу
- `groups` пустые: впишите логины в admin console до сохранения. Пустая группа в `tests` может не пройти валидацию Tailscale — это проверится только при Save; вычислитель в `check_acl.py` — приближение, а не замена Tailscale.
- `tests` на узлах-источниках (`tag:gateway` и др.) проверяют, что Hub-узлы ничего не инициируют.
- AC-M14-09 (поддельный `Tailscale-User-Login`) к ACL не относится, не покрыт.
- Применение через API (SEC-26) не реализовано — вставка вручную по runbook.
