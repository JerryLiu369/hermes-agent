"""Regression (#127869): reloaded transcript history must not duplicate state.db.

On desktop a failed turn rebuilds the history list from a reloaded transcript:
fresh dicts (different objects, no ``_DB_PERSISTED_MARKER``) with byte-identical
role/timestamp/content carry the same ``_row_id``/``message_uid`` as their
originals. The old identity-only flush (``id(msg)`` + marker + scan-prefix
``is`` check) missed them and re-appended the entire history on the next
flush. The store now also guards byte-identical rows as defense in depth.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from agent.context_compressor import _DB_PERSISTED_MARKER as _MARKER
from agent.message_metadata import MESSAGE_UID
from hermes_state import SessionDB
from run_agent import AIAgent


def _make_flush_agent(db: SessionDB, session_id: str):
    """Minimal agent shell owning the real flush implementation (offline-safe)."""
    agent = SimpleNamespace(
        _session_db=db,
        _session_db_created=True,
        _persist_disabled=False,
        session_id=session_id,
        _session_persist_lock=None,
        _flushed_db_message_ids=set(),
        _flushed_db_message_session_id=None,
        _last_flushed_db_idx=0,
        _persist_user_message_idx=None,
        _persist_user_message_override=None,
        _persist_user_message_timestamp=None,
        _pending_cli_user_message=None,
        _db_flush_scan_prefix=None,
        _mute_notification_reply=False,
    )
    agent._ensure_db_session = lambda: None
    agent._flush_messages_to_session_db = (
        AIAgent._flush_messages_to_session_db.__get__(agent, AIAgent)
    )
    agent._flush_messages_to_session_db_unlocked = (
        AIAgent._flush_messages_to_session_db_unlocked.__get__(agent, AIAgent)
    )
    return agent


def _seed_session(db: SessionDB, sid: str, turns: int = 3) -> None:
    db.create_session(sid, source="cli")
    for i in range(turns):
        db.append_message(sid, "user", f"question {i}")
        db.append_message(sid, "assistant", f"answer {i}")


def _contents(db: SessionDB, sid: str):
    return [r.get("content") for r in db.get_messages_as_conversation(sid, include_inactive=True)]


def _fresh_reload_copy(msg: dict, *, keep_row_id: bool = True, keep_uid: bool = True) -> dict:
    """A failed-turn rebuild copy: same durable content, fresh object, no marker."""
    fresh = {k: v for k, v in msg.items() if k != _MARKER}
    fresh.pop("_db_row_snapshot", None)
    fresh.pop("_canonical_row", None)
    if not keep_row_id:
        fresh.pop("_row_id", None)
    if not keep_uid:
        fresh.pop(MESSAGE_UID, None)
    return fresh


def test_reloaded_history_with_row_ids_does_not_duplicate(tmp_path: Path) -> None:
    """The #127869 shape: fresh dicts with same _row_id/uid/timestamps are history."""
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "RELOAD_ROW_IDS"
    _seed_session(db, sid, turns=3)
    baseline = _contents(db, sid)

    loaded = db.get_messages_as_conversation(sid, include_row_ids=True)
    assert all(m.get(_MARKER) is True for m in loaded)
    assert all(isinstance(m.get("_row_id"), int) for m in loaded)

    # Failed-turn rebuild: same rows, fresh objects, markers lost.
    rebuilt_history = [_fresh_reload_copy(m) for m in loaded]
    assert all(m.get(_MARKER) is None for m in rebuilt_history)
    live = [*[_fresh_reload_copy(m) for m in loaded],
            {"role": "user", "content": "new question"},
            {"role": "assistant", "content": "new answer"}]

    agent = _make_flush_agent(db, sid)
    agent._flush_messages_to_session_db(live, conversation_history=rebuilt_history)

    assert _contents(db, sid) == [*baseline, "new question", "new answer"]


def test_reloaded_history_content_only_does_not_duplicate(tmp_path: Path) -> None:
    """Same timestamps/content but no _row_id/uid still match history stably."""
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "RELOAD_CONTENT_ONLY"
    _seed_session(db, sid, turns=2)
    baseline = _contents(db, sid)

    loaded = db.get_messages_as_conversation(sid, include_row_ids=True)
    rebuilt_history = [_fresh_reload_copy(m) for m in loaded]
    live_prefix = [_fresh_reload_copy(m, keep_row_id=False, keep_uid=False) for m in loaded]
    # Timestamps survive even when row ids/uids are dropped (JSON round-trip shape).
    assert all(m.get("timestamp") is not None for m in live_prefix)
    live = [*live_prefix, {"role": "user", "content": "follow-up"}]

    agent = _make_flush_agent(db, sid)
    agent._flush_messages_to_session_db(live, conversation_history=rebuilt_history)

    assert _contents(db, sid) == [*baseline, "follow-up"]


def test_identityless_marker_stripped_reload_does_not_duplicate(tmp_path: Path) -> None:
    """No history boundary at all: the store guard still skips byte-identical rows."""
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "RELOAD_NO_HISTORY"
    _seed_session(db, sid, turns=2)
    baseline = _contents(db, sid)

    loaded = db.get_messages_as_conversation(sid, include_row_ids=True)
    stripped = [_fresh_reload_copy(m) for m in loaded]

    agent = _make_flush_agent(db, sid)
    agent._flush_messages_to_session_db(stripped)

    assert _contents(db, sid) == baseline

    # And repeated flushes stay flat (the 635→~308 duplication loop).
    agent2 = _make_flush_agent(db, sid)
    reloaded = db.get_messages_as_conversation(sid, include_row_ids=True)
    agent2._flush_messages_to_session_db([_fresh_reload_copy(m) for m in reloaded])
    assert _contents(db, sid) == baseline


def test_mutated_row_with_snapshot_still_rewrites(tmp_path: Path) -> None:
    """Guard against over-skipping: a filled blank row must rewrite, not skip."""
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "RELOAD_FILL"
    db.create_session(sid, source="cli")
    db.append_message(sid, "assistant", "")

    agent = _make_flush_agent(db, sid)
    loaded = db.get_messages_as_conversation(sid, include_row_ids=True)
    # First flush stamps the live dicts (marker + _row_id + snapshot).
    agent._flush_messages_to_session_db(loaded)
    assert loaded[0].get(_MARKER) is True

    # Finalizer fill: same _row_id/uid, new content, marker popped, snapshot kept.
    loaded[0]["content"] = "the final answer"
    loaded[0].pop(_MARKER, None)
    agent._db_flush_scan_prefix = None
    agent._flush_messages_to_session_db(loaded)

    assert _contents(db, sid) == ["the final answer"]


def test_parent_row_ids_to_empty_child_still_insert(tmp_path: Path) -> None:
    """A parent _row_id carried onto an empty child is not 'already durable' there."""
    db = SessionDB(db_path=tmp_path / "state.db")
    _seed_session(db, "PARENT", turns=2)
    db.create_session("CHILD", source="cli")

    parent_loaded = db.get_messages_as_conversation("PARENT", include_row_ids=True)
    carried = [_fresh_reload_copy(m) for m in parent_loaded]
    assert all(isinstance(m.get("_row_id"), int) for m in carried)

    agent = _make_flush_agent(db, "CHILD")
    agent._flush_messages_to_session_db(carried)

    assert _contents(db, "CHILD") == _contents(db, "PARENT")
    # Idempotent thereafter: the child flush stamped its own _row_ids.
    agent._flush_messages_to_session_db(carried)
    assert _contents(db, "CHILD") == _contents(db, "PARENT")


def test_store_guard_skips_byte_identical_batch(tmp_path: Path) -> None:
    """Direct store-level guard: re-inserting the tail changes nothing."""
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "STORE_GUARD"
    _seed_session(db, sid, turns=2)
    baseline = _contents(db, sid)

    tail = db.get_messages_as_conversation(sid, include_row_ids=True)[-2:]
    duplicates = [_fresh_reload_copy(m) for m in tail]
    inserted = db.append_messages_batch(sid, duplicates)

    assert inserted == 0
    assert _contents(db, sid) == baseline
