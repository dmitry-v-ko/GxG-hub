# Учебная тревога 3: перевыпуск bundle (AC-M14-05)

Сценарий: bundle просрочен (`GxgBundleStale`, старше 13 часов) или нужен немедленный перевыпуск после изменения политик/грантов. Процессы отказывают в работе при bundle старше 24 часов (GR-13). Автоматический перевыпуск описан, но не включён: [bundle-reissue](bundle-reissue.md). Ведущий и дежурный — как в [первом учении](drill-effects-block.md).

**Подготовка.** `RAILWAY_TOKEN` оператора; ключ подписи остаётся в `bundle-signer` (в учении его не читают и не показывают); репозиторий на нужной ветке с политиками `policies/pilot/`.

## Шаги

1. **Проверить исходное состояние:** `uv run python scripts/deploy/pilot_bundle.py --check` — ключи реестра равны живым ключам сервисов (иначе перевыпуск откажется, чинить реестр).
2. **Собрать и проверить без записи:** `uv run python scripts/deploy/pilot_bundle.py --build-only` — `gxg-policy build` и `verify` без ошибок.
3. **Перевыпустить и перезапустить читателей:**
   ```bash
   RAILWAY_TOKEN=... uv run python scripts/deploy/pilot_bundle.py --deploy
   ```
   Ожидаемо: `set GXG_BUNDLE_KEYS, GXG_BUNDLE_B64 on api|executor|ui|notifier|telegram`, затем `deploy …` для каждого.
4. **Подтвердить свежесть:** `readyz` у `api`, `ui`, `executor` — 200; алерт `GxgBundleStale` гаснет; в `built_at` bundle сегодняшнее время.
5. **Расширение прав** (если bundle меняет гранты): проверка `scripts/ci/grants_widening.sh` и подтверждение владельца — [widening-approval](../setup/widening-approval.md).

## Критерий и что записывает ведущий

От шага 1 до `readyz` 200 у всех читателей — не больше 15 минут (в зачёт AC-M14-05 по времени идёт только блокировка эффектов, ≤ 5 мин; здесь время фиксируется для справки). Процессы работали на старом bundle до redeploy. Протокол заполняет человек.
