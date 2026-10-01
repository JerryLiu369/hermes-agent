"""Regression tests for #126761: unparseable model_config must not be overwritten.

``_merge_model_config_json`` used to tolerant-parse the stored value to ``{}``
on any decode failure, so a ``set_session_yolo`` (or any patch) on a row with
truncated/corrupt JSON — or valid-but-non-dict JSON like ``'"5"'`` — replaced
the whole column with just the patch, permanently destroying session lineage
markers (``_branched_from``/``_delegate_from``). The merge must instead refuse
and preserve the raw string (logging an error); legal empty shapes
(``None``/``''``/``'{}'``) still merge normally.
"""

import json
import logging

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "state.db")
    yield session_db
    try:
        session_db.close()
    except Exception:
        pass


def _store_raw_model_config(db, session_id, raw):
    db._conn.execute(
        "UPDATE sessions SET model_config = ? WHERE id = ?", (raw, session_id)
    )
    db._conn.commit()


def test_malformed_json_preserved_on_set_yolo_and_logs_error(db, caplog):
    """Truncated JSON survives ``set_session_yolo`` and logs an error."""
    raw = '{"_branched_from": "parent_x", "yolo'
    db.create_session(session_id="s1", source="cli", model="m")
    _store_raw_model_config(db, "s1", raw)

    with caplog.at_level(logging.ERROR, logger="hermes_state"):
        db.set_session_yolo("s1", True)

    meta = db.get_session("s1")
    assert meta["model_config"] == raw
    assert any(
        r.levelno >= logging.ERROR and "refusing to merge" in r.message
        for r in caplog.records
    )


@pytest.mark.parametrize("raw", ['"5"', "[1,2]", "5", "null"])
def test_non_dict_json_preserved(db, caplog, raw):
    """Valid JSON that is not an object must not be overwritten by a merge."""
    db.create_session(session_id="s1", source="cli", model="m")
    _store_raw_model_config(db, "s1", raw)

    with caplog.at_level(logging.ERROR, logger="hermes_state"):
        db.set_session_yolo("s1", True)

    meta = db.get_session("s1")
    assert meta["model_config"] == raw
    assert any(
        r.levelno >= logging.ERROR and "refusing to merge" in r.message
        for r in caplog.records
    )


@pytest.mark.parametrize("raw", [None, "", "{}"])
def test_legal_empty_shapes_still_merge_normally(db, raw):
    """``None``/``''``/``'{}'`` are empty configs — the patch must apply."""
    db.create_session(session_id="s1", source="cli", model="m")
    _store_raw_model_config(db, "s1", raw)

    db.set_session_yolo("s1", True)

    meta = db.get_session("s1")
    assert json.loads(meta["model_config"]) == {"yolo_mode": True}
