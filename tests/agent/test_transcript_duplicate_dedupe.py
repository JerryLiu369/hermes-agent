"""Regression for #129368: byte-identical assistant duplicates after #123462.

A message dict that lost its ``_row_id`` (copy, rebuild, marker strip) but still
carries the timestamp the first insert stamped is the SAME logical message.
``resolve_and_repair_transcript_batch`` must adopt the existing active row
instead of inserting a second byte-identical row.
"""

from pathlib import Path

from hermes_state import SessionDB
from run_agent import AIAgent  # noqa: F401 -- warm heavy imports at collection (see test_tool_call_incremental_persistence)


def _open_db(tmp_path: Path, session_id: str) -> SessionDB:
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id=session_id, source="tui", model="test/model")
    return db


def _active_rows(db: SessionDB, session_id: str):
    return db.get_messages(session_id)


def test_identity_less_assistant_with_same_timestamp_adopts(tmp_path):
    """The exact #129368 shape: same role/content/timestamp, no _row_id -> no duplicate."""
    session_id = "sess-129368-assistant"
    db = _open_db(tmp_path, session_id)
    try:
        ts = 1759000000.123456
        first = [{"role": "assistant", "content": "hello world", "timestamp": ts}]
        assert db.append_messages_batch(session_id, first) == 1
        first_id = first[0]["_row_id"]
        assert isinstance(first_id, int)

        # A copy that lost _row_id/marker but kept timestamp+content (the re-flush).
        copy = {"role": "assistant", "content": "hello world", "timestamp": ts}
        assert db.append_messages_batch(session_id, [copy]) == 0

        rows = _active_rows(db, session_id)
        assert len(rows) == 1
        assert rows[0]["id"] == first_id
        assert rows[0]["content"] == "hello world"
        # The copy adopted the durable identity instead of inserting.
        assert copy["_row_id"] == first_id
    finally:
        db.close()


def test_identity_less_user_with_same_timestamp_adopts(tmp_path):
    """The guard is role-generic: user rows dedupe the same way."""
    session_id = "sess-129368-user"
    db = _open_db(tmp_path, session_id)
    try:
        ts = 1759000001.5
        first = [{"role": "user", "content": "same question", "timestamp": ts}]
        assert db.append_messages_batch(session_id, first) == 1
        first_id = first[0]["_row_id"]

        copy = {"role": "user", "content": "same question", "timestamp": ts}
        assert db.append_messages_batch(session_id, [copy]) == 0

        rows = _active_rows(db, session_id)
        assert len(rows) == 1
        assert copy["_row_id"] == first_id
    finally:
        db.close()


def test_genuinely_new_rows_still_insert(tmp_path):
    """Different timestamp or content must still insert; timestamp-less rows insert."""
    session_id = "sess-129368-new"
    db = _open_db(tmp_path, session_id)
    try:
        ts = 1759000002.0
        assert db.append_messages_batch(
            session_id, [{"role": "assistant", "content": "hello", "timestamp": ts}]) == 1

        # Same content, different timestamp -> new logical message.
        assert db.append_messages_batch(
            session_id, [{"role": "assistant", "content": "hello", "timestamp": ts + 10.0}]) == 1

        # Same timestamp, different content -> new logical message.
        assert db.append_messages_batch(
            session_id, [{"role": "assistant", "content": "different", "timestamp": ts}]) == 1

        # Same content, different role -> new logical message.
        assert db.append_messages_batch(
            session_id, [{"role": "user", "content": "hello", "timestamp": ts}]) == 1

        # No timestamp -> cannot prove identity -> insert.
        assert db.append_messages_batch(
            session_id, [{"role": "assistant", "content": "hello"}]) == 1

        rows = _active_rows(db, session_id)
        assert len(rows) == 5
    finally:
        db.close()


def test_cross_session_copy_still_inserts(tmp_path):
    """Same triple in a DIFFERENT session must insert (dedupe is per-session)."""
    db = _open_db(tmp_path, "sess-129368-parent")
    try:
        ts = 1759000003.0
        assert db.append_messages_batch(
            "sess-129368-parent",
            [{"role": "assistant", "content": "shared", "timestamp": ts}]) == 1

        db.create_session(session_id="sess-129368-child", source="tui", model="test/model")
        assert db.append_messages_batch(
            "sess-129368-child",
            [{"role": "assistant", "content": "shared", "timestamp": ts}]) == 1

        assert len(_active_rows(db, "sess-129368-parent")) == 1
        assert len(_active_rows(db, "sess-129368-child")) == 1
    finally:
        db.close()


def test_identity_less_assistant_with_tool_calls_same_timestamp_adopts(tmp_path):
    """Tool-call assistant rows without content dedupe when timestamp matches."""
    session_id = "sess-129368-tool-call"
    db = _open_db(tmp_path, session_id)
    try:
        ts = 1759000004.0
        tool_calls = [{"id": "call_1", "type": "function", "function": {"name": "test", "arguments": "{}"}}]
        first = [{"role": "assistant", "content": None, "tool_calls": tool_calls, "timestamp": ts}]
        assert db.append_messages_batch(session_id, first) == 1
        first_id = first[0]["_row_id"]

        copy = {"role": "assistant", "content": None, "tool_calls": tool_calls, "timestamp": ts}
        assert db.append_messages_batch(session_id, [copy]) == 0

        rows = _active_rows(db, session_id)
        assert len(rows) == 1
        assert rows[0]["id"] == first_id
        assert copy["_row_id"] == first_id
    finally:
        db.close()

