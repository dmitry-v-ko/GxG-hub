# Защита ветки `main` (AC-M0-03)

**Статус:** настраивает человек с правами администратора репозитория. Claude этого сделать не может. Пока шаги не выполнены и не проверены попыткой, AC-M0-03 — `manual_pending`.

Без этой настройки красный CI (AC-M0-02, AC-M0-04) не мешает смержить PR. Обязательная проверка `ci-ok` делает CI блокирующим. [`.github/CODEOWNERS`](../../.github/CODEOWNERS) назначает владельца для `schemas/`, `tests/contract/`, `scripts/ci/` и других чувствительных путей.

## Что должно получиться (цель AC-M0-03)

- В `main` нельзя пушить напрямую. Изменения попадают только через PR.
- PR нельзя смержить без зелёной проверки `ci-ok` на актуальном коммите.
- PR нельзя смержить без одобряющего ревью человека, а для путей из CODEOWNERS — без ревью владельца кода. Автор PR себя не одобряет. Новый push сбрасывает одобрение.
- Правило действует и на администраторов: bypass list пуст, `gh pr merge --admin` не обходит правило. Force-push и удаление `main` запрещены.

## Предусловия

Проверьте оба пункта до настройки. Иначе шаги ниже либо ничего не защитят, либо заблокируют все мержи.

### 1. План GitHub поддерживает защиту веток

`dmitry-v-ko/GxG-hub` — **private-репозиторий на личном аккаунте**. На **GitHub Free** rulesets, branch protection и обязательное ревью CODEOWNERS для private-репозиториев **не применяются**: GitHub даёт создать правило, но показывает баннер «Upgrade to GitHub Pro or make this repository public to enable this feature», и правило не действует.

Защита работает, если выполнено одно из условий:

- аккаунт на **GitHub Pro**;
- репозиторий **публичный**;
- репозиторий перенесён в **организацию** на плане **Team** или **Enterprise** (на Free для организаций — только публичные репозитории).

Проверка: **Settings → Rules → Rulesets** открывается без баннера про апгрейд, а у созданного ruleset статус `Active`, а не «not enforced».

### 2. Одобрять PR может кто-то, кроме автора

GitHub не засчитывает одобрение от автора PR. Сейчас единственный collaborator — `dmitry-v-ko` (admin), и PR создаются от его имени (так был создан PR #1: Claude пушит и открывает PR с учётными данными владельца). Если в этой ситуации включить «Required approvals: 1», «Require review from Code Owners» или «Require approval of the most recent reviewable push» при пустом bypass list, **ни один PR смержить не получится**.

Выясните, кто автор PR и кто ревьюер:

- **Есть второй человек** с правами `write` (collaborator) — он одобряет PR владельца, а владелец одобряет его PR → вариант **(а)**.
- **PR создаёт отдельная учётная запись** (машинный аккаунт или GitHub App, от имени которых Claude открывает PR), а владелец только ревьюит. Для GitHub это тоже «второй участник» → вариант **(а)**: автор — машинный аккаунт, ревьюер и code owner — `dmitry-v-ko`.
- **Один мейнтейнер, и PR от его имени** → вариант **(б)**.
- **План не поддерживает защиту** (предусловие 1 не выполнено) → вариант **(в)**.

## Варианты настройки

> **Выбор варианта — за владельцем репозитория.** Claude его не делает и настройки не меняет. Только вариант (а) полностью выполняет AC-M0-03. При вариантах (б) и (в) AC-M0-03 остаётся `failed`/`manual_pending`, и это нужно записать в `reports/conformance/M0.json` с выбранным вариантом.

| | (а) Второй ревьюер | (б) Один мейнтейнер | (в) Защита недоступна |
| --- | --- | --- | --- |
| Прямой push в `main` | запрещён | запрещён | не запрещён |
| Обязательный `ci-ok` | да | да | нет (только видимый статус) |
| Обязательное одобрение | 1, не автор | нет (0) | нет |
| Ревью CODEOWNERS | обязательно | файл есть, ревью не обязательно | файл есть, не действует |
| AC-M0-03 | `passed` после проверки | не выполнен, задокументирован | не выполнен |

### (а) Есть второй ревьюер — полная настройка

1. Убедитесь, что workflow `ci` хотя бы раз отработал в репозитории. Иначе проверку `ci-ok` нельзя выбрать из списка: откройте PR из `claude/m0-foundation` и дождитесь прогона.
2. Если PR создаёт машинный аккаунт: добавьте его как collaborator с ролью `write` (не `admin`). Владелец остаётся единственным code owner.
3. **Settings → Rules → Rulesets → New ruleset → New branch ruleset.**
4. **Ruleset name:** `main-protection`. **Enforcement status:** `Active`.
5. **Bypass list:** оставьте пустым. Не добавляйте ни роли (в том числе Repository admin), ни приложения, включая Claude GitHub App.
6. **Target branches → Add target → Include default branch.** Проверьте, что default branch — `main`.
7. **Branch rules** — включите:
   - **Restrict deletions**;
   - **Block force pushes**;
   - **Require a pull request before merging**:
     - *Required approvals:* `1`;
     - *Dismiss stale pull request approvals when new commits are pushed:* вкл.;
     - *Require review from Code Owners:* вкл. Тогда PR, меняющий `schemas/`, `tests/contract/`, `scripts/ci/` и другие пути из `.github/CODEOWNERS`, требует одобрения владельца кода;
     - *Require approval of the most recent reviewable push:* вкл. Тогда последний push одобряет не его автор;
     - *Require conversation resolution before merging:* вкл.;
   - **Require status checks to pass**:
     - *Require branches to be up to date before merging:* вкл.;
     - **Add checks →** `ci-ok` (источник — GitHub Actions).
8. **Create.**

Классическая защита вместо Rulesets: **Settings → Branches → Add branch protection rule**, pattern `main`. Включите те же пункты: *Require a pull request before merging* (1 approval, *Dismiss stale pull request approvals*, *Require review from Code Owners*, *Require approval of the most recent reviewable push*), *Require status checks to pass* (`ci-ok`, *Require branches to be up to date*), *Require conversation resolution before merging*, *Do not allow bypassing the above settings*. *Allow force pushes* и *Allow deletions* оставьте выключенными.

### (б) Один мейнтейнер, PR от его имени — без скрытой блокировки

Обязательное одобрение здесь технически невыполнимо, поэтому его заменяет правило, которое не блокирует мерж. CI остаётся блокирующим.

1. Шаги 1, 3–6 из варианта (а).
2. **Branch rules:**
   - **Restrict deletions**, **Block force pushes** — вкл.;
   - **Require a pull request before merging** — вкл., но:
     - *Required approvals:* `0`;
     - *Require review from Code Owners:* **выкл.** (иначе блокировка: code owner — сам автор);
     - *Require approval of the most recent reviewable push:* **выкл.**;
     - *Require conversation resolution before merging:* вкл. — неблокирующая замена одобрения: все комментарии ревью (в том числе критика) должны быть закрыты;
   - **Require status checks to pass** — `ci-ok`, *Require branches to be up to date* — вкл.
3. `.github/CODEOWNERS` остаётся в репозитории: GitHub автоматически запрашивает ревью владельца и показывает его в PR. Перед мержем владелец просматривает diff путей из CODEOWNERS сам.
4. Запишите в `reports/conformance/M0.json`: `AC-M0-03` — `failed` (или `manual_pending`), `variant: "b"`, причина «один мейнтейнер», дата. Когда появится второй ревьюер или машинный аккаунт для PR — перейдите на вариант (а) и повторите проверку.

### (в) План не поддерживает защиту

Пока выполняется предусловие 1, ни Rulesets, ни branch protection не действуют. Варианты:

1. **GitHub Pro** для личного аккаунта, затем вариант (а) или (б);
2. сделать репозиторий **публичным** (только если в нём нет закрытой информации — решает владелец), затем (а) или (б);
3. перенести репозиторий в **организацию** на плане Team, затем (а) или (б).

До этого `ci-ok` виден в PR, но не блокирует мерж. AC-M0-03 — `failed`, `variant: "c"`.

## Проверка попыткой (критерий AC-M0-03)

Выполните и запишите результат в `reports/conformance/M0.json` (`AC-M0-03`: `passed`/`failed`, вариант, кто проверял, дата, ссылки на PR). Тексты ошибок GitHub могут слегка отличаться; в таблицах указаны ключевые фразы для Rulesets и классической защиты.

1. **Прямой push запрещён:**
   ```bash
   git switch main && git pull
   git commit --allow-empty -m "probe: direct push must be rejected"
   git push origin main
   git reset --hard origin/main
   ```
   Ожидаемо push отклонён:

   | Защита | Ожидаемый вывод `git push` |
   | --- | --- |
   | Ruleset | `remote: error: GH013: Repository rule violations found for refs/heads/main.` и `- Changes must be made through a pull request.`, затем `! [remote rejected] main -> main (push declined due to repository rule violations)` |
   | Классическая | `remote: error: GH006: Protected branch update failed for refs/heads/main.`, затем `! [remote rejected] main -> main (protected branch hook declined)` |

2. **Без ревью нельзя смержить** (только вариант (а)): откройте тестовый PR с безобидным изменением (например, строка в `reports/progress.md`) от имени автора PR и дождитесь зелёного `ci-ok`. Кнопка **Merge** недоступна, в PR — «Review required» / «At least 1 approving review is required by reviewers with write access». Затем обе команды должны отказать:
   ```bash
   gh pr merge <n> --merge
   gh pr merge <n> --merge --admin
   ```

   | Команда | Ожидаемый результат |
   | --- | --- |
   | `gh pr merge <n> --merge` | `X Pull request dmitry-v-ko/GxG-hub#<n> is not mergeable: the base branch policy prohibits the merge.` и подсказки про `--auto` / `--admin` |
   | `gh pr merge <n> --merge --admin` | отказ, а не мерж: Ruleset — `GraphQL: Repository rule violations found` … `A review is required` / `At least 1 approving review is required`; классическая с *Do not allow bypassing* — `GraphQL: At least 1 approving review is required by reviewers with write access. (mergePullRequest)` |

   Если `--admin` смержил PR — bypass list не пуст или *Do not allow bypassing* выключен: AC-M0-03 `failed`, исправьте настройку.
3. **Ревью владельца кода обязательно** (только вариант (а)): в тестовом PR измените файл в `schemas/` (например, только `description`) и получите одобрение от участника, который не code owner. Merge заблокирован: «Review required — Waiting on code owner review from dmitry-v-ko».
4. **Красный CI не мержится (AC-M0-02):** в тестовом PR удалите `idempotency_key` из `required` в `schemas/effect-intent.schema.json` и выполните `uv run python scripts/ci/schema_lock.py --write`. `ci-ok` красный (`tests/contract/test_security_required_fields.py`). Merge заблокирован даже после одобрения, `gh pr merge <n> --merge --admin` отказывает: `Required status check "ci-ok" is failing` / `Repository rule violations found`. Проверяется и в варианте (б).
5. **Секрет не мержится (AC-M0-04):** в тестовом PR добавьте пустой файл `probe.pem` (`git add -f probe.pem`). Job `secrets` красный, merge заблокирован. Проверяется и в варианте (б).
6. Закройте тестовые PR без мержа и удалите их ветки.
