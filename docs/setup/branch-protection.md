# Защита ветки `main` (AC-M0-03)

**Статус:** настраивает человек с правами администратора репозитория. Claude этого сделать не может. Пока шаги не выполнены и не проверены попыткой, AC-M0-03 — `manual_pending`.

**Решение владельца (2026-11-20):** у репозитория один мейнтейнер, PR создаются от его имени. Поэтому защита — обязательная проверка `ci-ok` и CODEOWNERS. **Обязательного одобрения нет.** Прямой push и force-push в `main` запрещены. См. AC-M0-03 в [плане](../implementation-plan-v1.md).

## Что должно получиться

- В `main` нельзя пушить напрямую. Изменения попадают только через PR.
- PR нельзя смержить, пока `ci-ok` красный или не завершён на актуальном коммите. Это касается и администратора: bypass list пуст, `gh pr merge --admin` правило не обходит.
- Пути из [`.github/CODEOWNERS`](../../.github/CODEOWNERS) (`schemas/`, `tests/contract/`, `scripts/ci/` и др.) закреплены за владельцем. Одобрение не требуется: GitHub не засчитывает одобрение автора PR, и обязательное ревью заблокировало бы все мержи.
- GitHub не показывает ошибок в `.github/CODEOWNERS`, а файлы из `schemas/`, `tests/contract/`, `scripts/ci/` в PR помечены как принадлежащие владельцу. Ревью при этом **не запрашивается**: единственный code owner (`@dmitry-v-ko`) сам открывает PR, а GitHub никогда не запрашивает ревью у автора PR. Список Reviewers пуст, это ожидаемо. Запрос появится только в PR от другого аккаунта.
- Force-push и удаление `main` запрещены.

## Предусловия

`dmitry-v-ko/GxG-hub` — **private-репозиторий на личном аккаунте**. На **GitHub Free** rulesets и branch protection для private-репозиториев **не применяются**. Правило можно создать, но GitHub показывает баннер «Upgrade to GitHub Pro or make this repository public to enable this feature», и правило не действует.

Нужно одно из трёх:

- **GitHub Pro** на аккаунте;
- **публичный** репозиторий;
- репозиторий в **организации** на плане Team или Enterprise.

Как проверить: **Settings → Rules → Rulesets** открывается без баннера про апгрейд, а созданный ruleset показан как `Active`.

Ещё одно условие: workflow `ci` хотя бы раз отработал в репозитории. Иначе проверку `ci-ok` нельзя выбрать из списка.

## Шаги (Ruleset, GitHub UI)

1. **Settings → Rules → Rulesets → New ruleset → New branch ruleset.**
2. **Ruleset name:** `main-protection`. **Enforcement status:** `Active`.
3. **Bypass list:** оставьте пустым. Не добавляйте ни роли (включая Repository admin), ни приложения (включая Claude GitHub App).
4. **Target branches → Add target → Include default branch.** Проверьте, что default branch — `main`.
5. **Branch rules** — включите:
   - **Restrict deletions**;
   - **Block force pushes**;
   - **Require a pull request before merging** — запрещает прямой push:
     - *Required approvals:* `0`;
     - *Require review from Code Owners:* **выкл.** Code owner здесь — сам автор PR, его одобрение не засчитывается, и мерж стал бы невозможен;
     - *Require approval of the most recent reviewable push:* **выкл.**;
   - **Require status checks to pass**:
     - *Require branches to be up to date before merging:* вкл.;
     - **Add checks →** `ci-ok` (источник — GitHub Actions).
6. **Create.**

Если используется классическая защита (**Settings → Branches → Add branch protection rule**, pattern `main`), включите те же пункты: *Require a pull request before merging* (0 approvals, без Code Owners), *Require status checks to pass* (`ci-ok`, *Require branches to be up to date*), *Do not allow bypassing the above settings*. *Allow force pushes* и *Allow deletions* должны быть выключены.

## Проверка попыткой (критерий AC-M0-03)

Запишите результат в `reports/conformance/M0.json`: `AC-M0-03` — `passed`/`failed`, кто проверял, дата, ссылки на PR. Тексты ошибок GitHub могут слегка отличаться, ниже приведены ключевые фразы.

1. **Прямой push запрещён:**
   ```bash
   git switch main && git pull
   git commit --allow-empty -m "probe: direct push must be rejected"
   git push origin main
   git push --force origin HEAD~1:main   # force-push тоже должен быть отклонён
   git reset --hard origin/main
   ```

   | Защита | Ожидаемый вывод |
   | --- | --- |
   | Ruleset | `remote: error: GH013: Repository rule violations found for refs/heads/main.`, `- Changes must be made through a pull request.` (для force-push — `- Cannot force-push to this branch`), затем `! [remote rejected] main -> main (push declined due to repository rule violations)` |
   | Классическая | `remote: error: GH006: Protected branch update failed for refs/heads/main.`, затем `! [remote rejected] main -> main (protected branch hook declined)` |

2. **Красный `ci-ok` не мержится (AC-M0-02):** откройте тестовый PR, где из `required` в `schemas/effect-intent.schema.json` удалён `idempotency_key`, и выполните `uv run python scripts/ci/schema_lock.py --write`. `ci-ok` красный: падает `tests/contract/test_security_required_fields.py`. Обе команды должны отказать:
   ```bash
   gh pr merge <n> --merge
   gh pr merge <n> --merge --admin
   ```

   | Команда | Ожидаемый результат |
   | --- | --- |
   | `gh pr merge <n> --merge` | `X Pull request dmitry-v-ko/GxG-hub#<n> is not mergeable: the base branch policy prohibits the merge.` |
   | `gh pr merge <n> --merge --admin` | отказ, а не мерж: `GraphQL: Repository rule violations found` … `Required status check "ci-ok" is failing` (классическая защита: `Required status check "ci-ok" is failing. (mergePullRequest)`) |

   Если `--admin` смержил PR, значит bypass list не пуст или *Do not allow bypassing* выключен. Тогда AC-M0-03 — `failed`, исправьте настройку.
3. **Незавершённый `ci-ok` не мержится:** сразу после push в тестовый PR, пока `ci-ok` в статусе pending, `gh pr merge <n> --merge --admin` отказывает: `Required status check "ci-ok" is expected` / `is in progress`.
4. **Секрет не мержится (AC-M0-04):** в тестовом PR добавьте пустой файл `probe.pem` (`git add -f probe.pem`). Job `secrets` красный, merge заблокирован.
5. **CODEOWNERS действует:** в PR, открытом владельцем, ревью у него **не запрашивается** (GitHub не запрашивает ревью у автора PR), поэтому пустой список Reviewers — не ошибка. Проверьте:
   - на странице файла `.github/CODEOWNERS` в `main` GitHub не показывает баннер с ошибками (неизвестный пользователь, неверный синтаксис);
   - в тестовом PR, меняющем по одному файлу в `schemas/`, `tests/contract/` и `scripts/ci/`, на вкладке **Files changed** у каждого из них есть значок щита с подсказкой «Owned by you».

   Merge при этом не заблокирован — так и задумано. Если PR открыт другим аккаунтом, `dmitry-v-ko` появляется в Reviewers автоматически.
6. **Зелёный PR мержится без одобрения:** PR с безобидным изменением и зелёным `ci-ok` мержится кнопкой **Merge**. Это проверка того, что мержи не заблокированы скрыто.
7. Закройте тестовые PR без мержа и удалите их ветки.

## Альтернативы

Они не выбраны владельцем. Понадобятся, если условия изменятся.

- **Появился второй ревьюер или машинный аккаунт, от имени которого создаются PR.** Включите *Required approvals: 1*, *Dismiss stale approvals*, *Require review from Code Owners* и *Require approval of the most recent reviewable push*. Машинному аккаунту дайте роль `write`, не `admin`. После этого нужно обновить AC-M0-03 в плане.
- **План не поддерживает защиту** (см. «Предусловия»). Без защиты `ci-ok` виден в PR, но мерж не блокирует. Включите Pro, сделайте репозиторий публичным или перенесите его в организацию. До этого AC-M0-03 — `failed`.
