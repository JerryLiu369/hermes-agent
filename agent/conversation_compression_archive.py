"""Exact held-row coverage for an in-place archive.

A watermark cap archives every active row up to the newest id the caller held.
That deletes a gap below that id, and a trailing unpersisted turn makes the cap
fall back to the lease watermark. When the caller can name the rows the
compressor actually held, the commit archives those and clones the rest.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

ABSORBED_ROW_IDS = "_absorbed_row_ids"
# Id-less durable payload a repair folded into a survivor: copies of dropped
# dicts born durable (persist marker) but carrying no ``_row_id`` (gateway/ACP
# reloads). Surfaced as unresolved held coverage so the commit archives the
# originals instead of cloning them behind the running turn.
ABSORBED_HELD = "_absorbed_held"


def _positive_id(value: Any) -> Optional[int]:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def held_archive_coverage(
    messages: Sequence[Any], verbatim_tail: Optional[Sequence[Any]] = None,
) -> Tuple[List[int], List[Dict[str, Any]]]:
    """``(covered ids, unresolved dicts)`` from the history the compressor was handed.

    A positive ``_row_id`` is covered, including ids a repair merged into that dict.
    A dict with no id is unresolved: the commit matches one durable row, and a
    marker-less miss is an unpersisted turn rather than a reason to archive the
    lease watermark. Id-less durable payload a repair stashed on
    ``_absorbed_held`` counts as unresolved too, so a dropped stray result or a
    killed tool-call turn is archived, not cloned behind the running turn.
    """
    covered: List[int] = []
    unresolved: List[Dict[str, Any]] = []
    for batch in (messages or (), verbatim_tail or ()):
        for message in batch:
            if not isinstance(message, dict):
                continue
            row_id = _positive_id(message.get("_row_id"))
            if row_id is None:
                unresolved.append(message)
            else:
                covered.append(row_id)
            for absorbed in message.get(ABSORBED_ROW_IDS) or ():
                absorbed_id = _positive_id(absorbed)
                if absorbed_id is not None:
                    covered.append(absorbed_id)
            for held in message.get(ABSORBED_HELD) or ():
                if isinstance(held, dict):
                    unresolved.append(held)
    return list(dict.fromkeys(covered)), unresolved


def newest_exact_held_id(
    messages: Sequence[Any], verbatim_tail: Optional[Sequence[Any]] = None,
) -> Optional[int]:
    """Newest held id that still names the row the compressor saw.

    A head row counts only while it still carries the persist marker. A ``here N``
    tail is marker-swept copies; its id counts when the copy kept one.
    """
    from agent.context_compressor import _DB_PERSISTED_MARKER

    exact: List[int] = []
    for message in messages or ():
        if not isinstance(message, dict) or not message.get(_DB_PERSISTED_MARKER):
            continue
        row_id = _positive_id(message.get("_row_id"))
        if row_id is not None:
            exact.append(row_id)
    for message in verbatim_tail or ():
        if isinstance(message, dict):
            row_id = _positive_id(message.get("_row_id"))
            if row_id is not None:
                exact.append(row_id)
    return max(exact) if exact else None


def coverage_for_commit(
    session_db: Any, session_id: str, messages: Sequence[Any],
    verbatim_tail: Optional[Sequence[Any]] = None,
) -> Tuple[Optional[List[int]], Optional[List[Dict[str, Any]]]]:
    """Coverage to pass into ``archive_and_compact``, or ``(None, None)`` to keep the watermark.

    ``None`` when the newest exact held row is already inactive (another compaction
    won: archiving only the held ids would clone the winner) or when nothing held
    is a durable row. A trailing unpersisted turn does not take this branch: the
    rows above it stay unnamed and are cloned.
    """
    from agent.context_compressor import _DB_PERSISTED_MARKER

    newest = newest_exact_held_id(messages, verbatim_tail)
    role_of = getattr(session_db, "get_message_role", None)
    if newest is not None and callable(role_of) and role_of(session_id, newest) is None:
        return None, None
    covered, unresolved = held_archive_coverage(messages, verbatim_tail)
    marked = [
        message for message in unresolved
        if isinstance(message, dict) and message.get(_DB_PERSISTED_MARKER)
    ]
    if not covered and not marked:
        return None, None
    return covered, unresolved
