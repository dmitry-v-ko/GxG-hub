# GxG Hub — Security v1

**Статус:** нормативная целевая модель, не реализована и не проверена. 2026-11-20.
Исполнение эффектов детально описано в [Эффекты и согласование](effects-and-approval-v1.md).

## Что защищаем и от кого

**Цель:** никто, кроме уполномоченного агента нужного tenant, не может прочитать чужие данные. Никто — ни агент, ни посторонний — не может вызвать внешний эффект без прохождения политики и, где требуется, решения уполномоченного человека. Эффект исполняется не более одного раза.

Злоумышленник может:

- контролировать аргументы tool'ов, содержимое MCP-сообщений, HTTP-заголовки;
- внедрять инструкции в данные (prompt injection через Telegram-сообщение, документ KB, веб-страницу);
- иметь украденный токен агента с низкими правами или токен для другого сервиса;
- быть легитимным агентом или пользователем другого tenant;
- иметь сетевой доступ к внутренним адресам (SSRF или скомпрометированный соседний сервис).

Ни одно из этого не даёт полномочий.

**Чего мы не обещаем:** что систему невозможно скомпрометировать. Компрометация оператора, ключа подписи execution token или хоста Approval Service выходит за пределы модели. Компрометация аккаунта Tailscale или координационного сервера даёт только сетевую достижимость точек входа, но не полномочия (SEC-25). Против этого — разделение ролей, аудит, ротация ключей и kill switch.

## Цепочка проверок

| # | Где | Что проверяется | При сбое |
| --- | --- | --- | --- |
| 1 | Ingress (tailnet ACL, TLS, ContextForge auth) | Подлинность токена агента: issuer, audience, срок; срезание клиентских identity- и approval-заголовков; rate limit | 401, вызова нет |
| 2 | ContextForge RBAC + `unified_pdp` | Tool разрешён этому principal в этом tenant; tools `*.execute` агентам запрещены всегда | 403, вызова нет |
| 3 | Коннектор, read и propose | Проверенная identity (tenant, principal) от ContextForge; доступ к конкретному ресурсу; credentials только своего tenant | `permission_denied` / `not_found` (GR-17), обращения к провайдеру нет |
| 4 | Approval Service | Intent создан доверенным коннектором; политика; решение человека нужной роли того же tenant; срок; атомарное резервирование | Intent не исполняется |
| 5 | Коннектор, execute | Подпись и claims execution token; совпадение хеша параметров; одноразовый переход в `dispatching` | Отказ, обращения к провайдеру нет |
| 6 | Provider | Отдельные credentials на tenant; provider-side idempotency, где есть; egress allowlist | `outcome_unknown` → reconciliation |

Шаги 4 и 5 не зависят от шагов 1–2. Даже если ContextForge неверно настроен или обойдён, эффект без токена Executor'а не исполняется.

## Identity

- **SEC-01.** Principal — неизменяемый ID от identity-провайдера: токен агента в ContextForge, OIDC-пользователь для людей. Display name, email из аргументов, `agent_id`/`tenant_id` в аргументах tool не являются identity.
- **SEC-02.** У каждого агента свой credential. Общий токен на несколько агентов запрещён. Токен агента — API-токен ContextForge, выпущенный с `team_id` его team (`POST /v1/tokens`). На v1.1.1 такой JWT (HS256, ключ `JWT_SECRET_KEY` ContextForge) несёт `iss` = `mcpgateway`, `aud` = `mcpgateway-api`, `token_use` = `api`, `teams` = [`<team_id>`], `exp` ([проверено в M4](../adr/0001-identity-propagation.md#результаты-по-критериям), [claims](../../spikes/m4-contextforge/evidence/teams-claims-and-visibility.json)). Агенту выдаётся только токен ровно с одной team. Токен без team или с несколькими teams адаптер identity отклоняет.
- **SEC-03.** Коннектор получает identity вызывающего только доверенным каналом от ContextForge. Канал — наш плагин-адаптер ContextForge `gxg_identity` (хук `tool_pre_invoke`, [ADR-0001](../adr/0001-identity-propagation.md), вариант C). Он добавляет заголовок `X-GxG-Identity`: JWS `alg` = `EdDSA` (Ed25519) с `kid`. Ключ подписи есть только у адаптера, коннектор знает только публичный. Адаптер работает в процессе ContextForge, поэтому выполнение кода в ContextForge даёт ключ: ключ — из secret store, ротация через `kid`, алерт на неизвестный `kid`. Встроенное identity propagation ContextForge (`IDENTITY_PROPAGATION_ENABLED`, `IDENTITY_SIGN_CLAIMS`, на v1.1.1 оба по умолчанию `false`) полномочий не даёт и остаётся выключенным: его HMAC покрывает только `user_id:email`, без tenant, времени и nonce, и перехваченный заголовок можно повторять бесконечно.
  - Claims: `iss`, `aud` = коннектор, `sub` = principal, `tenant_id`, `tool` = имя tool у коннектора (upstream name, не алиас), `iat`, `exp` (`exp − iat ≤ 60 с`), `jti`, `ahs` = sha256 канонического JSON аргументов вызова. Канонический JSON: UTF-8, ключи отсортированы, без пробелов, не-ASCII как есть (`json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`). Это не RFC 8785: числа с плавающей точкой пишутся иначе, поэтому обе стороны используют одну функцию.
  - Коннектор проверяет подпись и известный `kid`, `iss`, `aud`, `tool` = `params.name` вызова, срок с допуском 5 с и `exp − iat ≤ 60 с`, хеш аргументов, `sub` внутри `tenant_id` и одноразовость `jti`. Store `jti` общий для всех реплик коннектора и хранит запись до `exp` + допуск. Без валидных claims коннектор отказывает (fail closed).
  - Fail closed в ContextForge: `PLUGINS_FAIL_ON_PLUGIN_ERROR=true` в env (блок `plugin_settings` в YAML плагинов v1.1.1 игнорирует), тогда плагин, который не загрузился, останавливает старт. Если плагины выключены (`PLUGINS_ENABLED=false`) или флаг не задан, ContextForge отправляет вызовы без identity, и защищает только проверка коннектора.
  - Пути без `tool_pre_invoke`: gateway в режиме `direct_proxy` обходит все плагины, поэтому `MCPGATEWAY_DIRECT_PROXY_ENABLED=false` (по умолчанию). `resources/read` и `prompts/get` хук не вызывают: они приходят без `X-GxG-Identity`, и коннектор на них отказывает: закрыт по умолчанию. Данные, зависящие от прав, коннектор отдаёт только через tools.
- **SEC-04.** Коннектор принимает MCP-вызовы только от ContextForge: сеть плюс аутентификация сервиса. Неподписанные заголовки и `_meta` не дают полномочий.
- **SEC-05.** Нет token passthrough. Токен агента не уходит к провайдеру, у провайдера отдельные credentials tenant'а.
- **SEC-06.** Человек-approver аутентифицируется в Approval Web UI отдельно от агентов: OIDC и/или passkey. Machine credentials не могут вызвать endpoint решения.

Механизм выбирается спайком: [ADR-0001](../adr/0001-identity-propagation.md) (варианты, критерии приёмки, протокол проверки). Коннекторы получают результат как `VerifiedIdentity` из `gxg-connector-kit` независимо от выбранного механизма.

- **SEC-23.** Approval Service независимо проверяет identity requester'а при создании intent'а, если механизм даёт проверяемую assertion. Иначе он опирается на service JWT коннектора и реестр платформы ([AS-08](approval-service-v1.md#аутентификация-сервисов)).
- **SEC-24.** Внутренние вызовы между сервисами Hub аутентифицируются service JWT (Ed25519, `aud`, `exp ≤ 60 с`) по реестру платформы ([AS-06](approval-service-v1.md#аутентификация-сервисов)). Сетевое расположение само по себе не даёт доверия.

Если ContextForge на закреплённой версии не может надёжно передать подписанную identity коннектору, это **NO-GO** для чтения персональных данных. Варианты обхода: отдельный экземпляр коннектора на tenant с собственным service credential либо внешний identity-адаптер. Решение принимается в PoC ([Roadmap](roadmap-v1.md#go--no-go)).

## Разделение ролей людей

- **SEC-07.** Агент не может согласовать свой intent. Человек не может согласовать intent, инициированный от его имени (requester ≠ approver).
- **SEC-08.** Для `high_risk` политика может требовать step-up (повторная аутентификация passkey/TOTP) и двух разных approver'ов. Тот, кто изменил политику, не согласует в одиночку intents по ней в течение суток после изменения (SHOULD).
- **SEC-09.** Изменения tools, connections, политик, ролей и ключей логируются с указанием автора, времени и хеша до/после. Ослабление политики вызывает alert.

## Execution token

Формат и проверки описаны в [Эффекты и согласование](effects-and-approval-v1.md#execution-token).

- **SEC-10.** Токен подписывается асимметричным ключом (Ed25519). Приватный ключ есть только у Executor'а, коннекторы знают только публичный (JWKS с `kid`, AS-09). Допуск расхождения часов — 5 с (AS-11). Ключ хранится в secret store, процедура ротации документирована, ротация проводится не реже раза в 90 дней и немедленно при подозрении на компрометацию.

## Prompt injection

- **SEC-11.** Содержимое, полученное из источников (сообщения, документы, страницы, результаты поиска), — данные, а не инструкции. Оно не может выдать права, создать решение или изменить политику. Максимум, что может сделать обманутый агент, — создать intent в пределах своих прав, и человек увидит его в UI.
- **SEC-12.** Approval UI показывает человеку точные параметры эффекта (получатель, полный текст, аккаунт, изменяемые поля) и того, кто его запросил, а не пересказ агента.

## Сеть

- **SEC-13.** В интернет не открыто ничего (решение владельца 2026-11-21, [ADR-0003](../adr/0003-network-tailscale.md)). Точки входа доступны только из tailnet Tailscale. Агенты ходят в ContextForge (MCP, HTTPS), люди — в Approval Web UI (HTTPS), оператор — в админку ContextForge и `admin` Approval Service. Коннекторы, Approval Service API, `mcp`, Executor и базы в tailnet не входят. Они работают только во внутренней сети хоста или кластера, и узлы tailnet их не видят.
- **SEC-25.** Tailscale — дополнительный сетевой слой, а не identity. Доступ через tailnet не даёт полномочий: на каждом входе по-прежнему проверяются токен агента ContextForge (SEC-02), OIDC approver'а (SEC-06) и service JWT (SEC-24). Заголовки Tailscale (`Tailscale-User-Login` и подобные от `tailscale serve`) и IP в tailnet identity не являются (SEC-01).
- **SEC-26.** Доступ в tailnet задаёт ACL-политика Tailscale (`policies/network/tailscale-acl.hujson`, вводится в M14). Она хранится в Git, проходит review, её изменения логируются (SEC-09). Запрещено всё, что не разрешено явно, правила пишутся по тегам:
  - `tag:agent` → `tag:gateway:443` (ContextForge MCP);
  - `group:approvers` → `tag:approval-ui:443`;
  - `group:operators` → `tag:gateway:443`, `tag:approval-ui:443`, `tag:approval-admin:443`, SSH к узлам Hub (Tailscale SSH с проверкой `check`);
  - узлы Hub (`tag:gateway`, `tag:approval-ui`, `tag:approval-admin`) не инициируют соединения к устройствам людей и агентов.
  Проверки ACL (`tests` в политике) покрывают каждое разрешение и минимум один запрет на каждый тег. Политика применяется через GitOps (`tailscale/gitops-acl-action`) после зелёного CI.
- **SEC-27.** Узлы Hub входят в tailnet по тегированным pre-authorized ключам с ограниченным сроком из secret store (SEC-16). У тегированных узлов нет срока действия ключа, поэтому их отзывают из админки Tailscale (runbook). Устройства людей подтверждаются администратором tailnet (device approval), Tailnet Lock — SHOULD. HTTPS — сертификаты Tailscale для имён `*.ts.net` (`tailscale serve`/`tailscale cert`): cookie `__Host-` и `Secure` (AC-M9-06) требуют HTTPS и внутри tailnet. **Funnel запрещён** на всех узлах (`nodeAttrs` без `funnel`). Exit node и subnet router на узлах Hub не включаются. Egress к провайдерам, IdP и Telegram Bot API идёт обычным интернетом по allowlist (SEC-15), не через tailnet.
- **SEC-14.** Endpoint'ы `execute` коннекторов достижимы сетью только из Executor'а. Даже при сетевом доступе без валидного токена исполнения нет.
- **SEC-15.** Egress ограничен allowlist'ом. Для любых URL, задаваемых конфигурацией или пользователем, запрещены localhost, link-local и metadata-адреса, RFC1918 и редиректы на них (защита от SSRF). Регистрация произвольных MCP-серверов доступна только оператору.

## Секреты

- **SEC-16.** Provider credentials и ключи хранятся в secret store или зашифрованы, разделены по tenant, имеют процедуру ротации и отзыва.
- **SEC-17.** Секреты никогда не попадают в промпты, ответы tools, approval UI, Git, логи и трейсы.

## Аудит

- **SEC-18.** Каждый вызов, intent, решение и исполнение связаны через `request_id`, `intent_id`, `business_task_id`. Payload в логах редактируется: тексты сообщений и персональные данные не пишутся в общий аудит, полный payload хранится только в защищённом хранилище intents.
- **SEC-19.** Аудит не является источником истины для статуса эффекта. Им является Approval Service.

## Аварийные выключатели

- **SEC-20.** Глобальный kill switch эффектов, отключение эффектов по tenant и по tool, немедленный отзыв токенов агента. Kill switch проверяется внутри транзакций создания intent'а, резервирования и `dispatch` ([AS-05](approval-service-v1.md#статусы-intentа-и-переходы)), поэтому действует на следующий же `dispatch`, без кешей.
- **SEC-21.** Новый effect tool регистрируется выключенным. Он включается только после прохождения тестов этапа C ([Conformance Matrix](conformance-matrix-v1.md)).

## Версия ContextForge

- **SEC-22.** Используется только релиз, закреплённый по digest, без известных незакрытых уязвимостей. Перед закреплением и каждым обновлением проверяются security advisories IBM (в частности, [CVE-2026-110279](https://www.ibm.com/support/pages/node/7296490)) и прогоняются тесты этапа B. Prerelease-образы запрещены.
- Закреплённая версия (спайк M4, 2026-11-20): `ghcr.io/ibm/mcp-context-forge:v1.1.1@sha256:16c7a8985c1a3528ba49fcab32951e06c9a41a171fc5c41d24b085283c4556a5`. Advisories на эту дату — в [ADR-0001](../adr/0001-identity-propagation.md). Для production версия одобряется после этапа B (M5).

## Ссылки

- [ContextForge plugins](https://ibm.github.io/mcp-context-forge/latest/using/plugins/)
- [ContextForge RBAC и teams](https://ibm.github.io/mcp-context-forge/latest/manage/rbac/)
- [ContextForge identity propagation](https://ibm.github.io/mcp-context-forge/1.1.x/manage/identity-propagation/)
- [Tailscale ACL](https://tailscale.com/kb/1018/acls), [ACL tests](https://tailscale.com/kb/1337/acl-syntax#tests), [GitOps для ACL](https://tailscale.com/kb/1204/gitops-acls), [Tailscale Serve](https://tailscale.com/kb/1312/serve), [Tailnet Lock](https://tailscale.com/kb/1226/tailnet-lock)
- [MCP authorization](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)
