# Защита ветки `main` (AC-M0-03)

**Статус:** настраивается человеком с правами администратора репозитория. Claude этого сделать не может. Пока шаги не выполнены и не проверены попыткой, AC-M0-03 — `manual_pending`.

Без этой настройки красный CI (AC-M0-02, AC-M0-04) не мешает смержить PR. Обязательная проверка `ci-ok` — то, что делает CI блокирующим.

## Что должно получиться

- В `main` нельзя пушить напрямую. Изменения попадают только через PR.
- PR нельзя смержить без зелёной проверки `ci-ok` на актуальном коммите.
- PR нельзя смержить без одобряющего ревью человека. Автор PR себя не одобряет. Новый push сбрасывает одобрение.
- Правило действует и на администраторов. Force-push и удаление `main` запрещены.

## Шаги (Ruleset, GitHub UI)

1. Убедитесь, что workflow `ci` хотя бы раз отработал в репозитории. Иначе проверку `ci-ok` нельзя выбрать из списка: откройте PR из `claude/m0-foundation` и дождитесь прогона.
2. **Settings → Rules → Rulesets → New ruleset → New branch ruleset.**
3. **Ruleset name:** `main-protection`. **Enforcement status:** `Active`.
4. **Bypass list:** оставьте пустым. Не добавляйте ни роли, ни приложения, включая Claude GitHub App.
5. **Target branches → Add target → Include default branch.** Проверьте, что default branch — `main`.
6. **Branch rules** — включите:
   - **Restrict deletions**;
   - **Block force pushes**;
   - **Require a pull request before merging**:
     - *Required approvals:* `1`;
     - *Dismiss stale pull request approvals when new commits are pushed:* вкл.;
     - *Require approval of the most recent reviewable push:* вкл. Тогда последний push одобряет не его автор;
     - *Require conversation resolution before merging:* вкл.;
   - **Require status checks to pass**:
     - *Require branches to be up to date before merging:* вкл.;
     - **Add checks →** `ci-ok` (источник — GitHub Actions).
7. **Create.**

Если вместо Rulesets используется классическая защита (**Settings → Branches → Add branch protection rule**, pattern `main`), включите те же пункты: *Require a pull request before merging* (1 approval, dismiss stale approvals, require approval of the most recent push), *Require status checks to pass* (`ci-ok`, strict), *Require conversation resolution*, *Do not allow bypassing the above settings*. *Allow force pushes* и *Allow deletions* оставьте выключенными.

## Проверка попыткой (критерий AC-M0-03)

Выполните и запишите результат в `reports/conformance/M0.json` (`AC-M0-03`: `passed`/`failed`, кто проверял, дата, ссылка на PR):

1. **Прямой push запрещён:**
   ```bash
   git switch main && git pull
   git commit --allow-empty -m "probe: direct push must be rejected"
   git push origin main        # ожидаемо: remote rejected, "protected branch" / "rule violations"
   git reset --hard origin/main
   ```
2. **Без ревью нельзя смержить:** откройте тестовый PR с безобидным изменением (например, строка в `reports/progress.md`) и дождитесь зелёного `ci-ok`. Кнопка **Merge** недоступна: «Review required». `gh pr merge <n> --merge` тоже отказывает.
3. **Красный CI не мержится (AC-M0-02):** в тестовом PR удалите `required` из `schemas/effect-intent.schema.json`. `ci-ok` красный, merge заблокирован даже после одобрения.
4. **Секрет не мержится (AC-M0-04):** в тестовом PR добавьте пустой файл `probe.pem` (`git add -f probe.pem`). Job `secrets` красный, merge заблокирован.
5. Закройте тестовые PR без мержа и удалите их ветки.
