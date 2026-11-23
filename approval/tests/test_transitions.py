"""The installed transition table: one row per allowed move (AS-03); the DB tests check behaviour."""

from __future__ import annotations

import pytest

from gxg_approval import transitions


def test_install_sql_has_every_rule() -> None:
    sql = transitions.install_sql()
    assert sql.startswith("DELETE FROM transition_rules; INSERT INTO transition_rules VALUES ")
    assert sql.count("), (") == len(transitions.rules()) - 1
    assert "('T12a', 'complete_refused_before_dispatch', 'executing', 'reserved', 'failed')" in sql


def test_tids_cover_t1_to_t18_t12a_and_t12b() -> None:
    assert {r[0] for r in transitions.rules()} == {f"T{n}" for n in range(1, 19)} | {"T12a", "T12b"}  # T12b: M10


def test_install_sql_refuses_values_that_would_need_quoting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transitions, "rules", lambda: (("T1", "propose", "", "", "x'); DROP TABLE intents; --"),))
    with pytest.raises(ValueError, match=r"^transition rules must be plain identifiers$"):
        transitions.install_sql()
