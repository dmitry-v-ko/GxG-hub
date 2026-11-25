# Ponytail (vendored)

Source: https://github.com/DietrichGebert/ponytail
Commit: 96608e078b2d5ea607c5c3b8b5a54d1c1fba6846
License: MIT (see PONYTAIL-LICENSE)

Vendored files, unchanged: `skills/ponytail/SKILL.md`, `skills/ponytail-review/SKILL.md`, `skills/ponytail-audit/SKILL.md`.
Why here: every Claude Code session on this repository (implementers, critics, audits) loads these skills without installing the plugin.
How they are used in GxG Hub: docs/implementation-plan-v1.md → «Ponytail в процессе».
Update: re-download the three files at a new commit, review the diff, change the commit above.
Slash commands such as `/ponytail lite` belong to the plugin and are not available here; the level is set in the task text instead.
