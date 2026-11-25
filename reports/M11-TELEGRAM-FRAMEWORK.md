# Telegram-клиент существующего сервиса (gxg_agents_v2)

Источник: клон `dmitry-v-ko/gxg_agents_v2` @ `f1c4d6d9e7bd5202fc703dabf3b0fba3fc732f0f`, только чтение.

| Вопрос | Ответ |
| --- | --- |
| Библиотека | **Pyrofork** — форк Pyrogram (MTProto, user session), импортируется как `pyrogram`. Не GramJS, mtcute, Telethon или TDLib |
| Версия | `pyrofork>=2.3.69` в `pyproject.toml:8`; в `uv.lock:40-41` — `pyrofork` `2.3.69`. Ускоритель шифрования `tgcrypto` `1.2.5` (`pyproject.toml:10`, `uv.lock:108-109`) |
| Язык | Python ≥ 3.13 (`pyproject.toml:5`). Оркестрация — TypeScript (Mastra), Telegram-библиотек в Node-части нет (`package.json` без `telegram`/`@mtcute`/`grammy`/`telegraf`) |
| Код сессии | `telegram_ingest/main.py` — команды `archive-sync`, `account-check`, `account-lookup`, папки; `from pyrogram import Client` и `Client(session_name, api_id, api_hash, workdir)` (`main.py:330-333`, `422-425`, `450-454`); чтение файла сессии только на чтение (`read_session_string`, `main.py:466-470`). `telegram_ingest/config.py` — `resolve_session` (`TG_SESSION_NAME`, `TG_SESSION_WORKDIR`, абсолютный путь вне репозитория, `config.py:61-69`), `primary_session_lock`, `SessionBusyError` |
| Как вызывается | Mastra запускает Python-процесс: `spawn('./scripts/run_telegram_ingest.sh', [...])` в `mastra/src/mastra/connectors/telegram/archive-sync.ts:188`, `account-check.ts:29`, `account-lookup.ts:46`; скрипт — `.venv/bin/python -m telegram_ingest.main` |
| MCP-сервер | **Нет.** В `telegram_ingest/` нет MCP; единственный MCP-сервер Mastra `mastra/src/mastra/mcp/operator-server.ts` Telegram не упоминает. Чтение архива — `PostgresTelegramArchiveRepository` (`mastra/src/mastra/connectors/telegram/archive-repository.ts`) in-process, без сетевого API |

Для коннектора Hub (ADR-0004) это ничего не меняет: он читает только `telegram_archive` ролью на `SELECT`, Telegram-библиотеки и сессии у него нет.
