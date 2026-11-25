# Runbook: ACL tailnet и ключи Tailscale

Политика: `policies/network/tailscale-acl.hujson`. Решения — [ADR-0003](../adr/0003-network-tailscale.md), SEC-13, SEC-25…27. Сеть из Claude-сессии не доступна: всё ниже владелец делает сам.

## 1. Применить политику

1. Локально: `uv run python scripts/network/check_acl.py` → `check_acl: OK`.
2. В `groups` впишите свои логины (в Git остаются пустые списки, пример — в комментарии файла):
   `"group:operators": ["<ваш-логин>"]`, `"group:approvers": ["<логины approver'ов>"]`. Не коммитьте реальные адреса.
3. Admin console → **Access controls** → вставить файл целиком → **Save**. Tailscale сам прогоняет `tests`: при провале политика не сохранится, ошибка укажет тест.
4. Изменение правил — через PR, затем повторить п. 3. Историю смотрите в Access controls → Revisions.

## 2. Auth-ключи узлов

**Один ключ на сервис, ровно с одним его тегом.** Узел с tagged-ключом получает ровно теги ключа. Если `--advertise-tags` (`TS_TAGS`) просит тег, которого в ключе нет, `tailscale up` падает с «requested tags invalid or not permitted». Общий ключ с тремя тегами не делайте.

| Сервис Railway | `TS_TAGS` | Ключ |
| --- | --- | --- |
| `contextforge` | `tag:gateway` | tagged `tag:gateway` |
| `ui` | `tag:approval-ui` | tagged `tag:approval-ui` |
| `admin` | `tag:approval-admin` | tagged `tag:approval-admin` |

Создание: admin console → Settings → Keys → **Generate auth key**: *Reusable* — по необходимости, **Pre-approved** — вкл., **Ephemeral** — вкл., **Tags** — один тег из таблицы, срок короткий (≤ 90 дней). Значение — в переменную `TS_AUTHKEY` нужного сервиса в Railway; ни в Git, ни в чат.
Теги `tag:agent` получают узлы агентов (отдельный ключ с этим тегом, когда появятся).

Отзыв: узлы с тегом не имеют срока ключа, поэтому отзывайте в Machines → узел → **Delete**, ключ — Settings → Keys → **Revoke**.

## 3. Проверки

**AC-M14-04** (нет публичной поверхности, Funnel выключен)
- С машины вне tailnet: порт-скан адресов production — открытых портов нет; у сервисов Railway нет публичных доменов и TCP Proxy.
- На каждом узле Hub (`railway ssh` в `contextforge`, `ui`, `admin`): `tailscale funnel status` пуст (`entrypoint.sh` и так не стартует при включённом Funnel).

**AC-M14-08** (ACL)
- `check_acl.py` и Access controls → Save зелёные.
- С устройства вне tailnet и с узла `tag:agent` к `tag:approval-ui:443`, `tag:approval-admin:443`: `curl -m 5 https://gxg-approval-ui.<tailnet>.ts.net/` — таймаут, не ответ.
- С устройства approver'а к `tag:gateway`: таймаут на 443 и 8443.

**AC-M14-10** (админка ContextForge только у операторов)
- С узла `tag:agent`: `curl -si https://gxg-gateway.<tailnet>.ts.net/admin` и admin API → `404`; `curl -m 5 https://gxg-gateway.<tailnet>.ts.net:8443/` → соединение не устанавливается.
- С устройства из `group:operators`: `https://gxg-gateway.<tailnet>.ts.net:8443/` открывается.

Полный список приёмки M14 — [implementation-plan-v1](../implementation-plan-v1.md).
