# GxG Hub — модель прав на ресурсы v1

**Статус:** нормативная целевая модель, не реализована. 2026-11-20.

Отвечает на вопрос: **может ли этот principal выполнить это действие над этим конкретным ресурсом** (чат, коллекция KB, CRM-аккаунт). Права на сам tool проверяет ContextForge, права на ресурс — коннектор по этой модели ([MCC-03](mcp-connector-contract-v1.md#требования)), а Approval Service использует её для approver'ов.

## Решение v1

- Права хранятся **в Git** как YAML-файл на каждый tenant: `policies/tenants/<tenant_id>.yaml`.
- Изменения вносятся через PR с ревью. CI проверяет схему и lint.
- Из файлов собирается **подписанный bundle**, который читают коннекторы и Approval Service.
- Код обращается к правам только через интерфейс `GrantResolver`. Позже хранилище можно заменить на базу с админкой, не меняя коннекторы.

Модель — **только разрешения** (allow-only). Всё, что не разрешено явно, запрещено. Правил deny в v1 нет, поэтому нет и вопросов приоритета.

## Идентификатор ресурса (RID)

```text
<domain>:<connection_id>/<type>/<id>
```

Примеры: `telegram:tg-main/chat/-1001234567890`, `kb:kb-main/collection/sales`, `crm:crm-main/account/42`, `demo:demo-main/board/b1`.

- **GR-01.** RID строит коннектор из проверенных данных backend'а. Аргумент модели — только ключ для поиска ресурса. Например, для документа KB коннектор сам узнаёт у backend'а его коллекцию, а не верит полю `collection` в аргументах.
- **GR-02.** `connection_id` в RID MUST принадлежать tenant'у вызывающего. Если connection чужой, ответ `not_found`.

## Иерархия

Права выдаются на контейнер и действуют на его содержимое: чат → сообщения, коллекция → документы, аккаунт → записи.

- **GR-03.** Для дочернего объекта коннектор определяет контейнер по данным backend'а и проверяет право на контейнер. Типы контейнеров и правило определения родителя задаются в manifest tool'а (`resource.type`, `resource.resolve`).

## Действия

| Действие | Что разрешает | Tools |
| --- | --- | --- |
| `read` | Чтение и поиск | `search`, `get`, `list`, `read` |
| `collect` | Потоковый сбор по cursor | `collect` |
| `write` | Обратимая внутренняя запись | Tools класса `write_reversible` |
| `propose` | Создание intent'а эффекта над ресурсом | `propose` |

Действие tool'а задаётся в manifest. `propose` не даёт права исполнить: исполнение всегда идёт через согласование ([Эффекты](effects-and-approval-v1.md)).

## Формат файла tenant'а

Схема: [resource-grants.schema.json](../../schemas/resource-grants.schema.json). Пример: [policies/tenants/example.yaml](../../policies/tenants/example.yaml).

```yaml
tenant: t-alpha
schema_version: 1

connections:                       # что есть у tenant'а; секретов нет, только ссылки
  - id: tg-main
    domain: telegram
    credential_ref: secret://t-alpha/telegram/main
  - id: kb-main
    domain: kb
    credential_ref: secret://t-alpha/kb/service

principals:
  agents:
    - id: agent:t-alpha:hermes      # agent:<tenant>:<name>: <tenant> = slug team, <name> = local-part email агента в ContextForge (<name>@<домен агентов tenant'а>); это `sub` из X-GxG-Identity (ADR-0001)
    - id: agent:t-alpha:analyst
  humans:
    - id: oidc:https://accounts.google.com:1234567890
      roles: [approver]

groups:
  researchers: [agent:t-alpha:hermes, agent:t-alpha:analyst]

grants:
  - id: kb-research
    subjects: [group:researchers]
    actions: [read]
    resources:
      - kb:kb-main/collection/public
      - kb:kb-main/collection/sales
  - id: tg-hermes-chats
    subjects: [agent:t-alpha:hermes]
    actions: [read]
    resources:
      - telegram:tg-main/chat/-1001234567890
    expires_at: 2027-03-01T00:00:00Z
    reason: "Пилот мониторинга чата партнёров"
```

- **GR-04.** `resources` — точные RID или маска `…/<type>/*` (все ресурсы типа в одном connection). Другие маски запрещены. Маска на уровне connection или домена запрещена.
- **GR-05.** `subjects` — principals и группы этого же файла. Ссылка на principal другого tenant'а — ошибка валидации.
- **GR-06.** Grant с `expires_at` в прошлом не действует. CI предупреждает о grant'ах, истекающих в ближайшие 14 дней.
- **GR-07.** У каждого grant'а есть стабильный `id`. Он пишется в аудит при каждом разрешённом доступе.
- **GR-08.** Роль `approver` у человека действует только в его tenant'е. Один человек может быть approver'ом в нескольких tenant'ах, но тогда он описан в каждом файле.

## Проверки в CI

- **GR-09.** Каждый PR, меняющий `policies/tenants/`, проходит:
  1. валидацию по JSON Schema;
  2. lint: уникальные `id`, нет висячих ссылок на principals, группы и connections, нет запрещённых масок, RID соответствует `connection.domain`;
  3. отчёт diff прав: какие пары (subject, action, resource) появились или исчезли. Отчёт публикуется в PR.
- **GR-10.** PR, **расширяющий** права (новые пары в отчёте diff), требует ревью человека, который не является автором. Сужение прав можно мержить без второго ревью.

## Bundle и распространение

- **GR-11.** После мержа CI собирает `grants-bundle.json`: grants всех tenant'ов с развёрнутыми группами, политики согласования ([approval-policy-v1](approval-policy-v1.md)), реестр платформы ([AS-25](approval-service-v1.md#реестр-платформы)), `bundle_version` = git commit SHA, время сборки. Bundle подписывается Ed25519-ключом CI, отдельным от ключа Executor'а.
- **GR-11a. Формат bundle** (общий для сборщика M2 и загрузчика M1):
  - `grants-bundle.json` — UTF-8, сериализация JCS (RFC 8785), поля: `schema_version` (= 1), `bundle_version` (git SHA), `built_at` (RFC 3339, UTC), `tenants` — объект `{tenant_id: {"grants": <файл grants с развёрнутыми группами>, "approval_policy": <файл политики или null>}}`, `platform` — реестр платформы, где у каждого коннектора есть `tools`: `[{name, version, effect_class, action, executes_via?, dispatch_timeout_s?, preview?}]`, собранные `gxg-policy build` из `connectors/*/gxg-manifest.yaml` (источник истины для AS-26). Машиночитаемо: [grants-bundle.schema.json](../../schemas/grants-bundle.schema.json), [grants-bundle-sig.schema.json](../../schemas/grants-bundle-sig.schema.json), тестовый вектор подписи — [bundle-signature.json](../../schemas/test-vectors/bundle-signature.json). Ключ `approval_policy` присутствует всегда (`null`, если файла нет).
  - `grants-bundle.json.sig` — JSON `{"alg": "EdDSA", "kid": "<id ключа>", "sig": "<base64url без паддинга подписи Ed25519 над точными байтами grants-bundle.json>"}`.
  - Публичные ключи проверки (по `kid`) задаются конфигурацией сервиса, а не берутся из bundle.
  - Детерминизм (AC-M2-05): при одинаковом commit всё содержимое, кроме `built_at`, побайтно одинаково.
- **GR-12.** Коннектор и Approval Service загружают bundle при старте и при обновлении, проверяют подпись и берут только свой tenant-срез, если работают на один tenant. **Без валидного bundle сервис не готов** (`/readyz` = fail) и отклоняет вызовы.
- **GR-13.** Новый bundle должен доехать до всех сервисов не позже чем за 5 минут после мержа. Устаревший bundle (старше 24 ч без проверки обновлений) — сервис не готов.
- **GR-13a.** Чтобы bundle не устаревал без изменений политик, CI пересобирает и переподписывает его по расписанию не реже раза в 12 часов (тот же commit → то же содержимое, новый `built_at`). Загрузчик отклоняет bundle с `built_at` старше уже загруженного **или старше максимального `built_at`, сохранённого на локальном диске** (переживает рестарт), а также bundle с `built_at` позже `now + 5 с`.
- **GR-14.** `bundle_version` записывается в аудит и в intent как часть `policy_version`.
- **GR-15.** Экстренный отзыв не ждёт bundle: отзыв токена агента в ContextForge и kill switch ([SEC-20](security-v1.md#аварийные-выключатели)) действуют сразу.

## Интерфейс `GrantResolver`

Входит в `gxg-connector-kit` ([Toolkit](mcp-toolkit-v1.md)).

```python
class GrantResolver(Protocol):
    def check(self, tenant: str, principal: str, action: Action, rid: str) -> Decision:
        """allow + grant_id или deny. Проверяет tenant, connection, иерархию уже
        разрешённого RID, срок grant'а."""

    def allowed(self, tenant: str, principal: str, action: Action,
                connection_id: str, rtype: str) -> AllowedSet:
        """Набор разрешённых RID (или «все типа rtype» по маске) для предфильтрации поиска."""

    @property
    def version(self) -> str: ...
```

## Поиск по многим ресурсам

- **GR-16.** Поиск MUST сначала ограничить запрос к backend'у набором `allowed(...)` (фильтр по разрешённым чатам или коллекциям). Затем каждый элемент результата проверяется повторно через `check`. Фильтрация только после поиска недопустима: результат раскрыл бы число скрытых совпадений и стоил бы ресурсов.
- **GR-17.** Если агент явно указал ресурс, на который нет права, ответ `not_found`, а не пустой результат. Иначе агент не отличит «нет данных» от «нет доступа к источнику» и может начать бесполезный повтор.

## Связь с ContextForge

Права на tools (какой агент видит какой tool) настраиваются в ContextForge: teams и правила `unified_pdp`. В v1 их не дублируем в этих файлах.

- **GR-18.** (SHOULD) CI проверяет дрейф: агент из файла tenant'а должен существовать в соответствующей team ContextForge, и наоборот.

## Будущий переход на базу

Когда клиентов станет больше или им понадобится управлять правами самим, `GrantResolver` получит реализацию поверх БД с админкой. Формат RID, действия, иерархия и инварианты GR-01…GR-17 сохраняются.
