# GxG Hub — Conformance & Security Test Matrix v1

**Статус:** обязательный план приёмки. Сейчас существуют только JSON Schema smoke-тесты (`tests/contract/test_envelopes.py`). Все остальные тесты — **not_run**.

## Локальные schema smoke-тесты

```bash
python -m pip install -r tests/contract/requirements.txt
python -m unittest discover -s tests/contract -p 'test_*.py' -v
```

Они проверяют только примеры доменных envelopes, но не MCP-сервер, аутентификацию, права или runtime-безопасность.

## Матрица

Колонка «Этап» — с какого этапа [Roadmap](roadmap-v1.md) тест обязателен. «Требования» — что он доказывает. Во всех негативных сценариях ожидается **ноль обращений к провайдеру** (проверяется счётчиком fake provider) и безопасная запись в аудит.

### Identity, tenant, ресурсы

| ID | Этап | Сценарий | Ожидаемо | Требования |
| --- | --- | --- | --- | --- |
| SC-01 | B | Два tenant'а, по два агента; список tools и прямые вызовы | Каждый видит и вызывает только своё; скрытый tool отклоняется и при прямом вызове | TEN-01, TEN-04 |
| SC-02 | B | Анонимный, истёкший токен, токен с чужим audience | 401 или MCP-эквивалент | SEC-01, SEC-02 |
| SC-03 | B | Подмена `tenant_id`/`agent_id` в аргументах, forwarded-заголовках, `_meta` | Игнорируется или отклоняется; действует проверенная identity | SEC-01, MCC-02 |
| SC-04 | B | Identity к коннектору: подделанная подпись, истёкшая, повтор, чужой issuer/audience | Коннектор отказывает до получения credentials | SEC-03 |
| SC-05 | B | Агент tenant A запрашивает чат, документ или аккаунт tenant B | `not_found` / `resource_forbidden` | TEN-02, MCC-03 |
| SC-06 | B | Прямой вызов коннектора в обход ContextForge | Сеть не пускает; при доступе — `unauthenticated` | SEC-04, SEC-13 |
| SC-07 | B | SSRF: URL регистрации, OAuth discovery, редирект на metadata/RFC1918 | Отказ, в том числе после DNS-редиректа | SEC-15 |
| SC-08 | B | Инструкции на вызов tool'а в тексте Telegram или KB | Не дают прав; эффекта нет; максимум — intent от агента в пределах его прав | SEC-11 |
| SC-09 | B | Утечки: секреты, токены, тексты сообщений в ответах и логах | Отсутствуют | SEC-17, SEC-18 |

### Эффекты и согласование

| ID | Этап | Сценарий | Ожидаемо | Требования |
| --- | --- | --- | --- | --- |
| EX-01 | C | Агент ищет или вызывает `*.execute` через ContextForge | Tool не найден / 403 | EFF-02 |
| EX-02 | C | Агент передаёт `approved: true` или чужой `intent_id` в `propose` | Создаётся обычный `pending_approval`; полномочий нет | MCC-02, EFF-09 |
| EX-03 | C | `execute` без токена, с чужим `aud`, неизвестным `kid`, истёкшим `exp` | Отказ до провайдера | EFF-18, SEC-10 |
| EX-04 | C | Повтор того же токена (`jti`) и повтор `dispatch` | Ровно один успешный `dispatch` | EFF-18, EFF-20 |
| EX-05 | C | Executor передаёт args, отличные от хеша в токене (подмена получателя или текста) | Отказ | EFF-07, EFF-18 |
| EX-06 | C | Повтор `propose`: тот же ключ и хеш / тот же ключ и другой хеш | Тот же intent / `conflict` | EFF-08 |
| EX-07 | C | Approve от агента, от requester'а, от approver'а другого tenant'а, без CSRF, со stale-экрана | Отклонено | SEC-06, SEC-07, EFF-10, EFF-11, EFF-15 |
| EX-08 | C | `high_risk` без step-up; кворум с одним человеком дважды | Отклонено | SEC-08, EFF-12 |
| EX-09 | C | Отклонён, истёк, отменён до резервирования | Исполнения нет | EFF-26 |
| EX-10 | C | Два Executor'а одновременно резервируют один intent | Ровно одно резервирование | EFF-16 |
| EX-11 | C | Провайдер исполнил, ответ потерян; падение после `dispatch` | `outcome_unknown` → reconcile; второго вызова нет | EFF-24, EFF-25 |
| EX-12 | C | Ошибка до отправки | `failed_before_effect`, повтор ≤ `max_attempts`, тот же `idempotency_key` | EFF-23 |
| EX-13 | C | Kill switch: глобальный, по tenant, по tool, во время ожидания и после резервирования | Новые исполнения блокируются за ≤ 30 с | SEC-20 |
| EX-14 | C | Права requester'а на ресурс отозваны между approve и execute | Отказ на execute | EFF-18 |
| EX-15 | C | Подделанный callback или webhook бота; кнопка в боте | Бот не может одобрить | EFF-13 |
| EX-16 | C | Approval Service недоступен во время propose / execute | Propose возвращает ошибку, intent не создан; execute не идёт без `dispatch` | EFF-18 |

### Контракт

| ID | Этап | Сценарий | Ожидаемо | Требования |
| --- | --- | --- | --- | --- |
| MC-01 | B | MCP negotiate/discover/invoke через закреплённый ContextForge | Корректные схемы и версии | MCC-01 |
| MC-02 | B | Поиск: схема, пагинация, валидный пустой результат, лимит размера | Валидные данные; пустой результат не вызывает fallback | MCC-04, MCC-06 |
| MC-03 | B | Transport failure / 429 / таймаут до и после отправки | Правильные коды и `retryable` | MCC-05 |
| MC-04 | B | Изоляция provider credentials, ротация и отзыв | Старые credentials не работают | SEC-16 |
| MC-05 | C | Обновление ContextForge и FastMCP, virtual servers, алиасы tools | Все тесты SC и EX проходят снова | SEC-22 |

### Интеграции и эксплуатация

| ID | Этап | Сценарий | Ожидаемо |
| --- | --- | --- | --- |
| SI-01 | D | Сбор через Hub → SourceInbox, оценка, кандидат в Signals | Без потерь и дублей; состояние вне Hub |
| SI-02 | D | ActionCandidate → propose → approve → fake provider → Outcome | Один dispatcher; повторный propose не дублирует |
| OPS-01 | E | Рестарт всех сервисов с intents в `pending_approval`, `reserved`, `dispatching` | Восстановление или reconcile без повторной отправки |
| OPS-02 | E | Изменение политики, отзыв агента, ротация ключа подписи | Действует немедленно; изменение в аудите; alert на ослабление |
| OPS-03 | F | Backup/restore Approval Service | Состояния intents восстановлены; повторных исполнений нет |

## Фиксация результатов

Для каждого прогона записываются: commit, digest образа ContextForge, версии MCP SDK и FastMCP, версия политики, fixture identity, ожидаемый и фактический результат, очищенные доказательства, автор и время. Статус теста — `not_run` / `passed` / `failed`. Чек-лист без прогонов ничего не доказывает.

## Как строить тесты

- Fake provider со счётчиком вызовов и режимами: успех, 429, timeout до отправки, «исполнил и потерял ответ».
- Гонки — реальным параллелизмом против одной БД PostgreSQL, а не моками.
- Обход — вызовом коннектора и с ContextForge, и напрямую.
- Секретов в fixtures нет. Все identity синтетические.
