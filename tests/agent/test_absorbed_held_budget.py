"""Regression for reviewer feedback on #129123: repair bookkeeping must not hit the compaction budget.

``_absorbed_held`` stashed a full copy of the dropped dict (content + ``_db_persisted``),
but was not in ``PERSISTENCE_ONLY_MESSAGE_FIELDS``, so ``model_metadata._wire_message_shadow``
priced it inside the token estimate. Fix: carry identity (role + content + marker),
and list both repair fields as persistence-only.
"""

from __future__ import annotations

import hermes_bootstrap  # noqa: F401 — pre-warm PM activation before home-IO guard installs.

from agent.agent_runtime_helpers import _remember_absorbed_row
from agent.context_compressor import _DB_PERSISTED_MARKER
from agent.conversation_compression_archive import held_archive_coverage
from agent.message_metadata import (
    ABSORBED_HELD,
    ABSORBED_ROW_IDS,
    PERSISTENCE_ONLY_MESSAGE_FIELDS,
    without_persistence_fields,
)
from agent.model_metadata import _wire_message_shadow, estimate_messages_tokens_rough


def test_absorbed_repair_fields_are_persistence_only() -> None:
    assert ABSORBED_HELD in PERSISTENCE_ONLY_MESSAGE_FIELDS
    assert ABSORBED_ROW_IDS in PERSISTENCE_ONLY_MESSAGE_FIELDS


def test_wire_shadow_strips_absorbed_bookkeeping() -> None:
    big = "x" * 5000
    msg = {
        "role": "user",
        "content": "hello",
        ABSORBED_ROW_IDS: [1, 2, 3],
        ABSORBED_HELD: [{"role": "tool", "content": big, _DB_PERSISTED_MARKER: True}],
    }
    shadow = _wire_message_shadow(msg)
    assert ABSORBED_HELD not in shadow
    assert ABSORBED_ROW_IDS not in shadow
    assert big not in str(shadow)
    stripped = without_persistence_fields(msg)
    assert ABSORBED_HELD not in stripped
    assert ABSORBED_ROW_IDS not in stripped


def test_estimate_ignores_absorbed_held_payload() -> None:
    big = "y" * 8000
    base = {"role": "user", "content": "hello"}
    bloated = {
        "role": "user",
        "content": "hello",
        ABSORBED_HELD: [{"role": "tool", "content": big, _DB_PERSISTED_MARKER: True}],
        ABSORBED_ROW_IDS: [7, 8],
    }
    assert estimate_messages_tokens_rough([base]) == estimate_messages_tokens_rough([bloated])


def test_remember_absorbed_row_stores_identity_not_payload() -> None:
    survivor: dict = {"role": "user", "content": "q"}
    dropped = {
        "role": "tool",
        "content": "stray-payload",
        "tool_call_id": "orphan-1",
        "tool_calls": [{"id": "killed-1"}],
        "timestamp": 123.0,
        "display_metadata": {"inline_diff": "z" * 9000},
        _DB_PERSISTED_MARKER: True,
    }
    _remember_absorbed_row(survivor, dropped, folded=False)
    held = survivor.get(ABSORBED_HELD)
    assert isinstance(held, list) and len(held) == 1
    identity = held[0]
    # Identity for the in-txn role+content match plus the durable marker.
    assert identity["role"] == "tool"
    assert identity["content"] == "stray-payload"
    assert identity.get(_DB_PERSISTED_MARKER) is True
    # Payload must not ride along.
    assert "tool_calls" not in identity
    assert "tool_call_id" not in identity
    assert "timestamp" not in identity
    assert "display_metadata" not in identity
    assert "_row_id" not in identity


def test_minimal_held_identity_still_counts_for_coverage() -> None:
    survivor: dict = {"role": "user", "content": "q", _DB_PERSISTED_MARKER: True}
    dropped = {"role": "tool", "content": "stray", _DB_PERSISTED_MARKER: True}
    _remember_absorbed_row(survivor, dropped, folded=False)
    covered, unresolved = held_archive_coverage([survivor])
    assert covered == []
    # Survivor itself (no _row_id) + one minimal held identity.
    assert len(unresolved) == 2
    assert {_DB_PERSISTED_MARKER} <= set(unresolved[1])
