# Integration 2026-11-23: M1.1 (MCP Toolkit) into `claude/integration`

Status: in progress. Merge `22346df` pushed; gate `scripts/ci/check-all.sh` running on `22346df`.

Heartbeat: first gate run red in root pytest (check-connectors on demo with M8 effects, template drift), see `INTEGRATION-ESCALATION.md`; harness fixed in `16d6d85`. Docker Hub answered 429 to this container: dockerd uses `registry-mirrors: mirror.gcr.io` (digest-pinned images unchanged). Remaining gate steps restarted.
