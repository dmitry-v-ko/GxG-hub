# Миграции KB из gxg_agents_v2 — дословные копии

Нужны для локальной копии KB (AC-M6-03/04, [аудит](../../../../docs/audits/kb-gap-audit.md#локальная-копия-kb-ac-m6-0304)). **Сами по себе базу KB не строят:** базовая схема `kb.*` (таблица `kb.entities`, функции `kb.update_updated_at` и др., таблицы/представления `kb_entity_types`, `kb_taxonomy_*`, `kb_entity_relations`, `kb_entity_taxonomy`) в репозитории не хранится — `supabase/migrations/0001_init.sql:1` говорит «applied directly via Supabase dashboard, not tracked locally». Проверено на чистом PostgreSQL 16 (роли `anon`, `authenticated`, `service_role` созданы заранее): каждая миграция ниже падает с `schema "kb" does not exist` или `relation "kb_…" does not exist`.

- Репозиторий: `dmitry-v-ko/gxg_agents_v2`, commit `f1c4d6d9e7bd5202fc703dabf3b0fba3fc732f0f`.
- Файлы: все миграции `supabase/migrations/`, где упоминаются `kb.` или `kb_…` (на этом commit последняя — `20260710110000`), без изменений. `20260701000000_fix_security_advisor_findings.sql` дополнительно включает RLS для таблиц `mastra_*` — оставлено как есть: копия дословная.
- Сервер KB MCP (KB Access Layer, `kb_access`) в gxg_agents_v2 отсутствует, его миграций или схемы здесь нет.
- Лицензия: в клоне gxg_agents_v2 нет файла `LICENSE`/`COPYING`; `package.json`, `mastra/package.json`, `frontend/package.json` — `"private": true`, поля `license` нет. Условия использования копий — вопрос владельцу (`pending_owner_decision`, [аудит, вопрос Q6](../../../../docs/audits/kb-gap-audit.md#открытые-вопросы-владельцу)).

```text
6c37e13f4db21a5477d69cd87486a8392c3f8203b3234cc4ac615f2af844ec88  20260528000000_kb_entities_add_public.sql
b2137234be161ee81a9f877a498c1f7e0176311ec2b722063ff0846d047a1d62  20260603000000_kb_entities_public_default_true.sql
9f970ec5a02833861ae30a9e75b9df9d05f8b335a445decedaa913a49ef98bb9  20260606000000_drop_visibility_column.sql
f8e581d9261076ecc8010fe397ce2c97b81c241e60f5e6fe04cf20bcc4289433  20260606120000_entity_type_to_taxonomy.sql
6ba8078309b84f1f5c226afe10035ce43c50779eb2ba890021462d0bd88a8842  20260608000000_kb_entities_fts.sql
a87633caf52b11032f7bf6b75f738faeb9aea5543b2f7a240700dfbb89cffd27  20260608120000_kb_entity_feedback.sql
8ca68fadcd2182e7f6a23362c56ccc5096945953b326c09f6f870d7a03b3cdd3  20260628143000_kb_domain_tree_from_hermes.sql
2610e5c862871b2ca4f95f954d79f494241bfc929962912cb71a008b9f6f561b  20260628152000_kb_type_guidance.sql
d00e96adbd5aa90f646bb91f4bd35b9d62ddfbbab1074ee933487c2089185490  20260701000000_fix_security_advisor_findings.sql
b250d4fe54856e7b40755a2aaab203d517920de04a0675671b135a56f3335ac7  20260701001000_fix_remaining_security_warnings.sql
b54c6568b57a512f60f2948c9d50a210d90e89a352dfb2cf860800121c001e6f  20260703000000_kb_profiles.sql
2b79d76d93ec78ee3a45ddb900401f27e8f414104796b5ef845c4da6aeba7a76  20260703001000_fix_kb_profile_functions.sql
2b79d76d93ec78ee3a45ddb900401f27e8f414104796b5ef845c4da6aeba7a76  20260703002000_fix_kb_profile_checksum.sql
2b79d76d93ec78ee3a45ddb900401f27e8f414104796b5ef845c4da6aeba7a76  20260703003000_remove_kb_profile_digest_dependency.sql
75d48d0aba7d4ecd9682d5b9353ac44f45e3e75ab3c931abe6317afcc9d42031  20260703004000_add_content_seed_entity_type.sql
4c3bec487fe18997ac835fd612171485c4c47ef7457b62b257e1c33dace1e561  20260710110000_restore_kb_feedback_insert_policy.sql
```

Три файла `20260703001000…003000` в upstream побайтно одинаковы (один хеш) — это так в gxg_agents_v2.

Сверка с upstream (клон gxg_agents_v2, без сети):

```bash
C=f1c4d6d9e7bd5202fc703dabf3b0fba3fc732f0f
for f in connectors/kb/vendor/kb-migrations/*.sql; do
  git -C ../gxg_agents_v2 show "$C:supabase/migrations/$(basename "$f")" | cmp - "$f"
done
(cd connectors/kb/vendor/kb-migrations && sha256sum -c <(sed -n '/^```text$/,/^```$/p' SOURCE.md | grep -E '^[0-9a-f]{64}  '))
```

Обновление: поднять commit, скопировать заново все миграции с `kb.`/`kb_…`, обновить хеши. Скрипта `check_upstream.sh`, как у Telegram, пока нет: он не нужен, пока копии не используются тестами (локальная копия KB заблокирована, gap G1).
