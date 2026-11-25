# M11 cycle 1 — ponytail-review (critic)

Reviewed diff: `fb14af1..2cebc81` of `claude/m11-telegram-read` (6 commits, 52 files). Also read: the connected kit code
(`cf_identity.py`, `grants.py`), `connectors/demo` (the copied pipeline), the stand (`standlib.py`, `test_sc.py`) and the
existing service `gxg_agents_v2@f1c4d6d` (`archive-repository.ts`, `archive-folder-collection.ts`, `connection-scope.ts`,
`chat-ref.ts`, `docs/specs/platform/capability-hub/connectors/telegram.md`, `docs/current/telegram-ingestion.md`).
Load assumed: the dev stand now; in production one connector process for ~10 tenants, hundreds of calls per minute
from a few agents, and Signals collecting on a schedule.

**What this change does:** It adds a read-only Telegram MCP connector (`telegram.messages.search`, `telegram.thread.read`,
`telegram.messages.collect`) that checks the ContextForge identity and the grants and then calls a Python port
`TelegramService`. The only implementation of that port is a fake with synthetic chats, so the connector starts only in
the test profile. It puts the connector on the M5 stand behind ContextForge, adds live tests for AC-M11-01…05 and two M5
backlog items. Its first commit also replaces `docs/` with an older copy from the orchestrator branch.

What I re-ran: root `pytest` 894 passed; `connectors/telegram` pytest 96 passed; `mutmut run` 1042 mutants, 1024 killed,
18 not killed, all 18 listed in `scripts/ci/mutmut-equivalent.txt` with correct reasons (I checked each diff); the M11
stand set 17 passed through ContextForge; my own 6 extra mutations in a temporary copy (service before grant, named-chat
check dropped, collect checked as read, excerpt in audit, no escaping, fake opening a session) were all caught;
`gitleaks tree HEAD`: no leaks. Full `stand/run.sh`: 3 x 46 passed (fresh, after restart, after recreate), so the M5 suite does not regress at HEAD; only the committed `M5.json` says otherwise (finding 5).

## Must fix

1. **The milestone silently rolls back accepted specs and the M4/M5 decisions** (`docs/adr/0001-identity-propagation.md`,
   `docs/specs/security-v1.md` SEC-02/SEC-03, `docs/specs/target-platform-v1.md` TEN-01, `docs/specs/roadmap-v1.md`,
   `docs/specs/resource-grants-v1.md:62`, `docs/backlog.md`; commit `90691ec` "docs and GOAL from the plan branch")
   - **What this is:** The specs that describe how identity reaches a connector and the record that M5 said GO.
   - **Problem:** The orchestrator branch never got the M4/M5 doc changes, and the executor copied its `docs/` over them.
     On this branch ADR-0001 is back to "Proposed / Не принято", SEC-03 lost the concrete rules (EdDSA `X-GxG-Identity`,
     `tool`, `ahs`, shared `jti` store, `PLUGINS_FAIL_ON_PLUGIN_ERROR=true`, `direct_proxy` off), SEC-02/TEN-01 are back to
     VERIFY, the Roadmap GO decision (AC-M5-05) is gone, and four open M5 backlog items (F6, F11, F12, F13) were deleted
     without being done. When this branch is merged after M5, it undoes AC-M4-02, AC-M4-04 and AC-M5-05. The executor
     template says "do not change specs silently"; nothing in the plan or report mentions it.
   - **Fix:** `git checkout fb14af1 -- docs/adr docs/specs docs/backlog.md`, then re-apply only the one M11 backlog line.
     Take only `docs/implementation-plan-v1.md` from the plan branch (it is the same file).
   - **If we skip it:** The code (kit, plugin, connectors) enforces rules the spec no longer states; the next reviewer
     or milestone builds on the weaker text.

2. **AC-M11-01 is not met: the connector does not talk to the existing Telegram service at all**
   (`connectors/telegram/src/telegram_connector/server.py:88-89`, `service.py:10-11`, `reports/M11-ESCALATION.md`)
   - **What this is:** M11 is "a facade over the domain ports of the existing service", tested "with a test account and
     test chats".
   - **Problem:** The existing service has no network read port (confirmed in `gxg_agents_v2`: `search` is in-process
     TypeScript; `thread.read` and per-chat `collect` do not exist). So the only implementation is the fake, and
     `Settings.from_env` refuses to start with anything else. Every AC test runs against fixtures the executor wrote.
     This is a gap that needs the owner (where the port lives, and whether a test account may be used). The rules say
     such a gap stops the cycle; the executor recorded it as `pending_owner_decision` and reported AC-M11-01 as
     `passed_on_fake_service`.
   - **Fix:** Escalate now. Either the owner opens the port (default in the escalation: HTTP route in gxg_agents_v2) and
     the connector gets an HTTP adapter plus a run on a test account and test chats, or the owner changes AC-M11-01/02
     to "on the port fake" and moves the real run to a later milestone. Only the owner can change an AC.
   - **If we skip it:** M12 and M13 build on a connector that has never read one real archived message.

3. **The port contract differs from the real service in ways the fake hides** (`service.py`, `fake.py:94-106`,
   `tools.py:208-296`)
   - **What this is:** `TelegramService` is meant to be the service's contract, so an HTTP adapter can drop in later.
   - **Problem:** (a) Real `PostgresTelegramArchiveRepository.search` does **not** check the Connection scope; it filters
     `chat_ref = ANY(...)` and marks unknown chats `not_synced` (archive-repository.ts:67, 299-337). The fake raises
     `chat_forbidden`. So "the service keeps its own Connection scope; grants only narrow it" (tools.py:6) is false for
     the real search: a chat with a mask grant that is outside scope gives `not_found` on the fake and an empty
     `complete:false` success on the real service. Scope must be checked by the caller (the real code says "the caller
     derives them from that scope"). (b) The real Signals collection is per **folder**, with `query`,
     `minimumMessageLength`, a 16 KiB opaque `tga1:` cursor of per-peer positions, and dedupe key `peer_id:message_id`
     (archive-folder-collection.ts, CON-TG-11). The connector's `collect` is per chat, has no query or length filter,
     its own `tgk1.` cursor and `chat_ref:message_id` ids. AC-M12-01 ("the same set of SourceInboxItem by dedupe keys as
     the old path") cannot hold on this contract. (c) `read_thread` and `scope_chats` exist in no service code.
   - **Fix:** Write the port from the real code: scope read from Connection metadata (`telegramConnectionScopeSchema`)
     and checked by the connector before `search`; `collect` over the folder collection (or an owner decision that
     per-chat collect is the target, recorded in service-integration-v1); make the fake follow the real behaviour
     (no scope check inside `search`).
   - **If we skip it:** The tests prove behaviour of a service that does not exist; the adapter will change the
     security path (scope) after the critic has signed it off.

## Should fix

4. **The AC-M11-02 test measures a constant** (`fake.py:85-88`, `stand/tests/test_telegram.py:460-469`,
   `connectors/telegram/tests/test_telegram.py:740-747`)
   - **What this is:** "Active Telegram sessions of the existing service did not change".
   - **Problem:** `open_sessions` is `len(self.sessions)`, filled once from fixtures; nothing the connector does can change
     it. A connector that opened its own Telethon session would still pass. The real evidence is structural (no client
     library in `uv.lock`, no session env), which is good, but it is not what the AC measures.
   - **Fix:** Keep the lockfile test; for the counter, read the service's own session/lock state (for example
     `PrimarySessionLock` holders or Connection health) once the port exists, or say in the report that AC-M11-02 is
     shown structurally.
   - **If we skip it:** A green AC that cannot fail.

5. **Committed evidence is wrong** (`reports/conformance/M5.json`, `reports/conformance/M11.json:"regression"`)
   - **What this is:** The conformance reports are the milestone's proof (DoD-3).
   - **Problem:** Commit `c8d1ac5` overwrote `M5.json` with a run of `3d590e4` where SC-01 and SC-07 **failed**
     (`all_passed: false`, `commit_dirty: true`). `M11.json` says "reports/conformance/M5.json left as M5 committed it" —
     not true. `M11.json` itself is for `654ee9a` with `commit_dirty: true`, not for HEAD. CI on all 5 commits is red
     (GitHub billing, jobs never started), so DoD-1 has no CI evidence.
   - **Fix:** Restore `M5.json` from `fb14af1` (or commit a passing run at HEAD), regenerate `M11.json` at a clean HEAD,
     fix the sentence, record that CI did not run and why.
   - **If we skip it:** The repository says M5 regressed.

6. **Stage B tests that MCC-10 requires before registration were not run for `telegram`** (`stand/tests/test_telegram.py`)
   - **What this is:** MCC-10: a connector is registered in ContextForge only after the stage tests pass.
   - **Problem:** For telegram there is no SC-06 (reach from the agent network), no MC-01 (schemas through ContextForge),
     no SC-01 call-by-name of t-beta's telegram tools by a t-alpha agent, and SC-08 is in-process only. Concrete case: a
     later edit that puts `telegram` on the `edge` network or publishes port 8080 passes every M11 test.
   - **Fix:** Parametrize the existing `test_sc06_*`, `test_mc01_*` and the SC-01 by-name test over `demo` and `telegram`
     (the helpers already take `service`/`url`).
   - **If we skip it:** Network isolation of the second connector is held only by `compose.yaml`.

## Nice to have

7. **~150 lines copied from `demo`** (`tools.py:95-142` = `DemoTool.run`; `server.py` `parse_keys`, `BundleFiles`,
   `NoVerifier`, most of `Settings`). Marked `shortcut:` with a limit (third connector), so a decision, not a bug. It
   also copies demo's known backlog defect: `internal_error` frames as multi-line stderr text (`tools.py:137-138`).
   Move the pipeline and the bundle loader into `gxg-connector-kit` when the template is made.
8. **Escaping and truncation** (`tools.py:150-161`): the 4000-char cut is applied after escaping, so a 3999-char message
   with two bidi marks is flagged `truncated` and loses real text, and a cut can leave `\u{20`. Cut the raw text, then
   escape.
9. **Search results carry no untrusted mark** (`tools.py:237-243`): CON-TG-08 wants every Telegram-controlled field
   marked untrusted; `thread.read`/`collect` have `untrusted: true`, search excerpts do not (search-result schema has
   `additionalProperties: false`, so it needs a schema-compatible marker, e.g. in the tool description).
10. **The fake and its fixtures ship in the production image** (`Dockerfile`: only `testctl.py` is removed). Harmless
    today (profile check), but `fixtures/service.json` with fake session strings is in every image.

Verdict: fix 1, 2 and 3 first (2 needs the owner).
Lean: -150 lines possible.
Not checked: behaviour against the real service and a real Telegram account (no port; not allowed without the owner).
