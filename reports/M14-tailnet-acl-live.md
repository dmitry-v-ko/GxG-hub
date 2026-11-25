# M14: ACL tailnet применён (OPS-06, AC-M14-08/10)

Дата: 2026-11-24. Решение владельца: применить политику с исключением для своих устройств.

## Что применено
`policies/network/tailscale-acl.live.hujson` — это живая политика, сохранена через Tailscale API (`POST /tailnet/-/acl` с `If-Match` на прежний ETag, ответ 200). До сохранения её проверил `/acl/validate` — `{}`. Во время сохранения Tailscale прогнал секцию `tests`.

Она отличается от строгой `policies/network/tailscale-acl.hujson` (по спеке, её проверяет `check_acl.py`) только в части людей и устройств владельца:

| Строгая (repo) | Живая | Почему |
| --- | --- | --- |
| `group:operators`, `group:approvers` (пустые) | `autogroup:owner`, `autogroup:admin` | в Git нет адресов; оператор и approver в пилоте — владелец |
| — | `autogroup:member → autogroup:member:*` | устройства владельца видят друг друга как раньше; узлы Hub помечены тегами, в `autogroup:member` не входят |
| `nodeAttrs: []` | `funnel` только для `autogroup:member` | сохранено из прежней политики, чтобы не сломать устройства владельца; узлы Hub помечены тегами и Funnel не получают (SEC-27 для Hub соблюдён) |
| — | `ssh` check для `autogroup:member → autogroup:self` | сохранено из прежней политики |

Прежняя политика (`*` → `*`) сохранена вне Git, для отката. Откат — Access controls → Revisions.

## Проверено
- Tailscale `tests` прошли при сохранении: `tag:agent` → только `tag:gateway:443`. Узлы `tag:gateway`, `tag:approval-ui` и `tag:approval-admin` не достают друг друга и `tag:agent` (deny).
- Сервисы Hub связаны только через `*.railway.internal` (`pilot_agent_job.py`, `services.yaml`, AC-M14-11 в `railway_deploy.py --audit`). ACL tailnet этот трафик не трогает.
- В tailnet 6 устройств: 3 узла Hub с тегами и 3 устройства владельца без тегов. Узлов `tag:agent` и `tag:mastra` нет.

## Не проверено (нужно для OPS-06 = passed)
- Живые пробы изнутри узла Hub (`curl` с `contextforge` на `gxg-approval-ui`). Для этого нужен SSH-ключ в аккаунте Railway; его не добавляли.
- Положительный путь владельца: UI одобрений и `:8443` открываются с его устройства. Проверится при нажатии «Одобрить» по заявке M13. Если не откроется — откат в Revisions.
