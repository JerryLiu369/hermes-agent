"""Regression tests for #129123: alternation repair drops must count toward archive coverage.

A turn killed after its tool call was saved leaves an assistant row with
``tool_calls`` and no result; a stray tool result similarly has no call.
``repair_message_sequence`` drops both from reload, but the rows stay active
in state.db. In-place compaction must archive them as summarized, not clone
them after the compacted set behind the running turn.

Covers both commit paths:
* row-ID coverage (``--resume``/TUI/Desktop): dropped ids retire onto the
  survivor's ``_absorbed_row_ids`` and ``held_archive_coverage`` names them;
* id-less coverage (gateway/ACP): dropped marker-bearing dicts retire onto the
  survivor's ``_absorbed_held`` and resolve as unresolved held coverage.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import hermes_bootstrap  # noqa: F401 — pre-warm import-time PM activation at collection,
# before the home-IO guard installs; a lazy import mid-test would probe the
# worktree manifest under the real home and trip the guard.
from agent.conversation_compression_archive import coverage_for_commit, held_archive_coverage
from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path: Path) -> SessionDB:
    handle = SessionDB(db_path=tmp_path / "state.db")
    try:
        yield handle
    finally:
        handle.close()


def _seed_killed_turn_plus_stray(db: SessionDB, sid: str) -> None:
    """User/ack/stray/user/killed layout: no mergeable neighbours, unique bodies."""
    db.create_session(sid, source="cli")
    db.append_message(sid, "user", content="q1-unique-129123")
    db.append_message(sid, "assistant", content="ack-unique-129123")
    db.append_message(
        sid, "tool", content="stray-payload-unique-129123",
        tool_call_id="orphan-129123", tool_name="read_file",
    )
    db.append_message(sid, "user", content="q2-unique-129123")
    db.append_message(
        sid, "assistant", content="",
        tool_calls=[{
            "id": "killed-129123", "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
        }],
    )


def _active_contents(db: SessionDB, sid: str):
    return [r["content"] for r in db.get_messages(sid)]


def _all_flags(db: SessionDB, sid: str):
    return [
        {"content": r.get("content"), "active": r.get("active"), "compacted": r.get("compacted")}
        for r in db.get_messages(sid, include_inactive=True)
    ]


def test_row_id_path_archives_repair_dropped_rows(db: SessionDB) -> None:
    """#129123 (row IDs): killed tool-call turn + stray result are covered, not cloned."""
    sid = "s-129123-named"
    _seed_killed_turn_plus_stray(db, sid)
    assert len(db.get_active_message_ids(sid)) == 5

    held = db.get_messages_as_conversation(sid, repair_alternation=True, include_row_ids=True)
    # Both drops fired: the killed assistant turn and the stray tool result are gone.
    assert [m["role"] for m in held] == ["user", "assistant", "user"]
    assert all(m.get("tool_calls") is None for m in held if m["role"] == "assistant")

    covered, _ = held_archive_coverage(held)
    assert sorted(covered) == [1, 2, 3, 4, 5] or len(set(covered)) == 5, (
        f"dropped rows must be covered via absorbed ids: {covered}"
    )

    covered_ids, unresolved = coverage_for_commit(db, sid, held)
    assert covered_ids is not None and len(set(covered_ids)) == 5

    compacted = [
        {"role": "user", "content": "[CONTEXT COMPACTION] summary-129123"},
        {"role": "assistant", "content": "continuing-129123"},
    ]
    db.archive_and_compact(
        sid, compacted, covered_ids=covered_ids, unresolved_held=unresolved,
    )

    assert _active_contents(db, sid) == [
        "[CONTEXT COMPACTION] summary-129123", "continuing-129123",
    ], "dropped rows must not be cloned behind the running turn"
    rows = _all_flags(db, sid)
    for content in ("stray-payload-unique-129123", ""):
        matches = [r for r in rows if r["content"] == content]
        assert len(matches) == 1 and matches[0]["active"] == 0 and matches[0]["compacted"] == 1, (
            f"dropped row {content!r} must be archived once as summarized history: {matches}"
        )


def test_id_less_path_archives_repair_dropped_rows(db: SessionDB) -> None:
    """#129123 (no row IDs): dropped marker-bearing rows resolve as held coverage."""
    sid = "s-129123-idless"
    _seed_killed_turn_plus_stray(db, sid)

    held = db.get_messages_as_conversation(sid, repair_alternation=True, include_row_ids=False)
    assert all("_row_id" not in m for m in held)
    assert [m["role"] for m in held] == ["user", "assistant", "user"]

    covered_ids, unresolved = coverage_for_commit(db, sid, held)
    # No row IDs: nothing covered, but every survivor plus both absorbed drops
    # is unresolved held coverage for the in-txn content match.
    assert covered_ids == [], f"expected id-less coverage, got {covered_ids}"
    assert len(unresolved) == 5, (
        "3 survivors + stray + killed drops must all be unresolved held coverage"
    )

    compacted = [
        {"role": "user", "content": "[CONTEXT COMPACTION] summary-129123"},
        {"role": "assistant", "content": "continuing-129123"},
    ]
    db.archive_and_compact(
        sid, compacted, covered_ids=covered_ids, unresolved_held=unresolved,
    )

    assert _active_contents(db, sid) == [
        "[CONTEXT COMPACTION] summary-129123", "continuing-129123",
    ], "dropped rows must not be cloned behind the running turn"
    rows = _all_flags(db, sid)
    for content in ("stray-payload-unique-129123", ""):
        matches = [r for r in rows if r["content"] == content]
        assert len(matches) == 1 and matches[0]["active"] == 0 and matches[0]["compacted"] == 1, (
            f"dropped row {content!r} must be archived once as summarized history: {matches}"
        )


def test_absorbed_held_is_persistence_only_and_identity_minimal() -> None:
    """Verify _absorbed_held is excluded from wire shadow / token pricing and stashes identity only."""
    from agent.agent_runtime_helpers import _remember_absorbed_row
    from agent.context_compressor import _DB_PERSISTED_MARKER
    from agent.message_metadata import (
        ABSORBED_HELD,
        ABSORBED_ROW_IDS,
        PERSISTENCE_ONLY_MESSAGE_FIELDS,
        without_persistence_fields,
    )

    assert ABSORBED_ROW_IDS in PERSISTENCE_ONLY_MESSAGE_FIELDS
    assert ABSORBED_HELD in PERSISTENCE_ONLY_MESSAGE_FIELDS

    survivor = {"role": "assistant", "content": "survivor"}
    dropped = {
        "role": "tool",
        "content": "heavy payload" * 100,
        "tool_call_id": "call_123",        "extra_bloat": "x" * 1000,
        _DB_PERSISTED_MARKER: True,
    }
    _remember_absorbed_row(survivor, dropped, folded=False)

    assert ABSORBED_HELD in survivor
    assert len(survivor[ABSORBED_HELD]) == 1
    stashed = survivor[ABSORBED_HELD][0]
    assert stashed.get("role") == "tool"
    assert stashed.get("content") == dropped["content"]
    assert stashed.get(_DB_PERSISTED_MARKER) is True
    assert "extra_bloat" not in stashed
    assert "tool_call_id" not in stashed

    clean = without_persistence_fields(survivor)
    assert ABSORBED_HELD not in clean
    assert ABSORBED_ROW_IDS not in clean
