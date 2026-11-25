# AC-M14-04: внешняя доступность production (2026-11-24)

Проверка из контейнера вне tailnet, без доступа к сети Railway.

## Railway, проект `gxg-hub-pilot`, окружение production

GraphQL API Railway, все 16 сервисов (executor, ui, contextforge, telegram, bundle-signer, notifier, api, migrate, mcp, telegram-db, approval-db, sweeper, admin, pilot-agent, contextforge-db, decider):

- `serviceDomains` пусто, `customDomains` пусто у каждого;
- `tcpProxies(environmentId, serviceId)` пусто у каждого.

Публичных адресов у production нет, поэтому сканировать снаружи нечего: ни один порт не открыт в интернет (ADR-0003, SEC-13). То же подтверждает `railway_deploy.py --audit` (AS-28).

## Tailscale funnel

Funnel публикует узел в интернете, и тогда его имя `*.ts.net` резолвится в публичном DNS. Запрос к публичному DoH (cloudflare-dns.com), типы A и AAAA:

| Узел Hub | Тег | A | AAAA |
| --- | --- | --- | --- |
| gxg-approval-ui | tag:approval-ui | NXDOMAIN | NXDOMAIN |
| gxg-approval-admin | tag:approval-admin | NXDOMAIN | NXDOMAIN |
| gxg-gateway | tag:gateway | NXDOMAIN | NXDOMAIN |

Funnel не включён ни на одном узле Hub. `tailscale funnel status` на самих узлах не запускался: у этой сессии нет shell на узлах. Внешний признак funnel — публичная DNS-запись — отсутствует.

Итог AC-M14-04: **passed** по внешним признакам.
