# M11 — вопросы владельцу (работу не блокируют)

Веха M11 «Telegram read-only», ветка `claude/m11-telegram-read`. Работа идёт по вариантам по умолчанию; решение владельца, отличное от умолчания, — `new_scope` следующего цикла.

Доступ к `dmitry-v-ko/gxg_agents_v2` получен (только чтение, `f1c4d6d`); в нём ничего не менялось.

| # | Вопрос | Вариант по умолчанию | Статус |
| --- | --- | --- | --- |
| 1 | **Сетевой порт сервиса.** У существующего Telegram-сервиса нет endpoint для чтения архива: поиск (`PostgresTelegramArchiveRepository.search`) вызывается только в процессе Mastra, HTTP есть лишь у admin-маршрутов Capability Hub; `thread.read` и сбор по чату не реализованы. Где открыть порт: HTTP-маршрут в gxg_agents_v2 (внутренняя сеть, служебный ключ) или TS-коннектор на MCP TypeScript SDK внутри сервиса (mcp-toolkit-v1 это допускает)? | HTTP-маршрут `POST /internal/telegram/{search,thread,collect}` в gxg_agents_v2 со служебным ключом; коннектор Hub получает HTTP-адаптер, реализующий `TelegramService` (`connectors/telegram/src/telegram_connector/service.py`). До этого коннектор работает только на fake-порту (`GXG_PROFILE=test`) и вне теста не стартует | `pending_owner_decision` |
| 2 | **AC-M11-01 «с тестовым аккаунтом и тестовыми чатами».** Нужен ли прогон на настоящем тестовом аккаунте Telegram? Без разрешения владельца реальные аккаунты и чаты не используются. | SC-03…SC-05, SC-09, SC-10, MC-02, MC-03 проверены через ContextForge на коннекторе с fake-портом сервиса (fixtures по контракту сервиса). Прогон на тестовом аккаунте — после п. 1 и явного разрешения: отдельный тестовый аккаунт, 2–3 тестовых чата в scope Connection | `pending_owner_decision` |
| 3 | **RID чата.** Пример в resource-grants-v1 — `telegram:tg-main/chat/-1001234567890` (peer ID). Коннектор использует `chat_ref` сервиса: peer ID — внутренний идентификатор провайдера (MCC-04) и не должен попадать в логи (AC-M11-05). Поправить пример в спеке? | `telegram:<connection>/chat/<chat_ref>`; спека не менялась (это пример, формат `<id>` задаёт коннектор) | `pending_owner_decision` |
