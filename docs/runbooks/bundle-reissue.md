# Runbook: автоперевыпуск grants bundle (M14, GR-13)

Bundle старше 24 часов процессы отказываются читать (GR-13), алерт `GxgBundleStale` срабатывает на 13 часах. Bundle перевыпускает оркестратор по расписанию, **каждые 10 часов** (решение владельца 2026-11-24). Сервис-cron описан как вариант на будущее, но **в Railway не включён и не включается**.

## Что описано

`deploy/railway/services.yaml`, блок `planned` → `bundle-reissue`:

- cron `0 */6 * * *` (каждые 6 часов; правило проверяется `validate_planned`: не реже чем раз в 12 часов);
- образ `deploy/railway/bundle-reissue.Dockerfile` (не собирался в этой сессии: Docker недоступен), команда `python scripts/deploy/pilot_bundle.py --deploy`;
- единственный секрет — `RAILWAY_TOKEN` (владелец вносит в Railway, строка в таблице AS-28 кода: `railway_token` → `bundle-reissue`);
- **ключ подписи остаётся в `bundle-signer`** (сервис-хранилище, не запускается). Cron читает его через Railway API в памяти, передаёт `gxg-policy build` через окружение дочернего процесса и не печатает и не пишет. Копии ключа в `bundle-reissue` нет, проверка `validate_planned` это запрещает.
- версия bundle — `RAILWAY_GIT_COMMIT_SHA` (в сборке Railway нет `.git`).

Блок `planned` не входит в `services`: `railway_deploy.py` (применение, `--verify`, `--audit`) его не создаёт и не проверяет.

## Проверка без Railway

```bash
uv run python scripts/deploy/railway_deploy.py --dry-run   # строка «bundle-reissue [cron, PLANNED, not deployed] cron 0 */6 * * *»
uv run pytest tests/deploy/test_bundle_reissue.py          # описание валидно; один запуск команды на mock Railway выпускает свежий, подписанный ключом bundle-signer bundle для всех читателей
```

## Решение владельца (2026-11-24) и включение на будущее

Railway-токен, достаточный для чтения ключа из `bundle-signer`, записи переменных пяти сервисов и redeploy, — токен проекта: он видит переменные **всех** сервисов, то есть ломает разделение секретов AS-28 на уровне платформы. Поэтому владелец решил: cron-сервис не включаем, перевыпуск по расписанию (каждые 10 ч, при пороге GR-13 в 24 ч и алерте на 13 ч) делает оркестратор той же командой `pilot_bundle.py --deploy`. Описание в `planned` остаётся как вариант на будущее.

Если когда-нибудь включать сервис:
1. Перенести блок `bundle-reissue` из `planned` в `services` (`railway_deploy.py` создаст сервис, выставит `cronSchedule` и политику `NEVER`; поле `cronSchedule` на живом API не проверялось).
2. Добавить в таблицу AS-28 спеки строку «Railway token для перевыпуска bundle — `bundle-reissue`» (сейчас она только в коде `railway_deploy.py`) и принять риск токена проекта заново.
3. Внести `RAILWAY_TOKEN` в Railway и запустить `railway_deploy.py --audit`: имена секретов должны совпасть, публичных доменов нет.
4. Первый запуск проверить вручную: `uv run python scripts/deploy/pilot_bundle.py --check`, затем дождаться срабатывания cron и убедиться по `GxgBundleStale`/`readyz` у `api`, `ui`, `executor`, что bundle свежий.

## Если перевыпуск не идёт

1. `pilot_bundle.py --check`: ключи реестра `policies/pilot/platform/registry.yaml` равны живым `GXG_SVC_PUBLIC_KEY`? Если нет, скрипт отказывается подписывать («the registry in policies/pilot does not match the live keys»).
2. Токен жив и не отозван; `bundle-signer.GXG_BUNDLE_SIGNING_KEY` задан.
3. Пока не починено: выпустить вручную, `RAILWAY_TOKEN=... python scripts/deploy/pilot_bundle.py --deploy` с хоста оператора. До 24 часов с последнего bundle процессы работают.
