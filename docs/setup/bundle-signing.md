# Ключ подписи bundle (GR-11, GR-11a)

**Зачем.** После мержа в `main` job `bundle` в `.github/workflows/ci.yml` собирает `grants-bundle.json` и подписывает его Ed25519-ключом CI. Коннекторы и Approval Service принимают bundle только с валидной подписью (GR-12). Ключ CI отдельный от ключа Executor'а (GR-11).

Пока ключ не настроен, job `bundle` на `main` красный. На `ci-ok` и мерж PR это не влияет: в PR bundle собирается с одноразовым ключом (`scripts/ci/bundle_smoke.sh`).

## Формат

- Приватный ключ — 32-байтный seed Ed25519 в base64url без паддинга. PEM не используется: сканер секретов запрещает заголовки ключей в репозитории, а Ed25519 хватает seed'а.
- Публичный ключ — `ed25519:<base64url 32 байт>`, как в реестре платформы.
- `kid` — произвольная метка ключа, например `ci-2026-11`. Она попадает в `grants-bundle.json.sig`, а сервисы ищут по ней публичный ключ в своей конфигурации (GR-11a).

## Настройка (владелец, один раз)

1. Сгенерировать seed на доверенной машине. Не сохранять его в файл:
   ```bash
   python3 -c "import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).rstrip(b'=').decode())"
   ```
2. Получить публичный ключ из клона репозитория:
   ```bash
   read -rs GXG_BUNDLE_SIGNING_KEY && export GXG_BUNDLE_SIGNING_KEY   # вставить seed, ввод не отображается
   uv run python -c "import os; from gxg_policy.bundle import public_key, unb64url; print(public_key(unb64url(os.environ['GXG_BUNDLE_SIGNING_KEY'])))"
   unset GXG_BUNDLE_SIGNING_KEY
   ```
3. **Settings → Secrets and variables → Actions:**
   - вкладка **Secrets**: `GXG_BUNDLE_SIGNING_KEY` = seed;
   - вкладка **Variables**: `GXG_BUNDLE_KID` = `kid`, `GXG_BUNDLE_PUBLIC_KEY` = публичный ключ из шага 2.
4. Тот же `kid` и публичный ключ прописать в конфигурацию сервисов, которые загружают bundle (M1 и далее).

Job `bundle` после сборки проверяет подпись по `GXG_BUNDLE_PUBLIC_KEY`. Если secret и variable не совпадают, job красный, и неверно подписанный bundle не публикуется. Результат — артефакт `grants-bundle` с файлами `grants-bundle.json` и `grants-bundle.json.sig`. Доставка до сервисов (GR-12, GR-13) в M2 не входит.

## Ротация и компрометация

Новый seed получает новый `kid`. Сначала новый публичный ключ добавить в конфигурацию сервисов, затем заменить secret и variables. Старый ключ убрать из сервисов, когда все работают на bundle с новым `kid`. При компрометации старый `kid` удаляется из сервисов сразу.

## Проверка вручную

```bash
uv run gxg-policy verify <каталог с bundle> --kid "$GXG_BUNDLE_KID" --public-key "$GXG_BUNDLE_PUBLIC_KEY"
```
