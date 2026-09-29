"""Internal parser for one selected persisted ZCode SQLite session.

The first ZCode adapter reads one explicit session from a read-only SQLite
snapshot. It never reads rollout/debug logs, terminal output caches, or raw
reasoning/tool bodies into its returned safe model.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Dict, List, Optional, Tuple


_MAX_TEXT_CHARS = 65536
_SAFE_TOOL_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}\Z")
_VISIBLE = "visible"
_HIDDEN = "hidden"

_REQUIRED_TABLE_COLUMNS = {
    "session": {"id", "title", "directory", "version",
                "time_created", "time_updated"},
    "message": {"id", "session_id", "data", "sequence"},
    "part": {"id", "message_id", "session_id", "data", "sequence"},
}


@dataclass(frozen=True)
class ZCodeDiagnostic:
    code: str
    message_index: Optional[int] = None
    part_index: Optional[int] = None


@dataclass(frozen=True)
class ZCodeTextItem:
    record_id: str
    message_index: int
    part_index: int
    role: str
    semantic_kind: str
    text: str


@dataclass(frozen=True)
class ZCodeToolEvent:
    record_id: str
    message_index: int
    part_index: int
    tool_name: Optional[str]
    tool_ref: str
    status: Optional[str]


@dataclass(frozen=True)
class ZCodeParseResult:
    text_items: Tuple[ZCodeTextItem, ...]
    tool_events: Tuple[ZCodeToolEvent, ...]
    diagnostics: Tuple[ZCodeDiagnostic, ...]
    completeness: str
    source_stable: bool
    selected_session_sha256: Optional[str]
    message_count: int
    part_count: int
    hidden_message_count: int
    reasoning_part_count: int


def _diag(items: List[ZCodeDiagnostic], code: str,
          message_index: Optional[int] = None,
          part_index: Optional[int] = None) -> None:
    items.append(ZCodeDiagnostic(code, message_index, part_index))


def _table_columns(connection: sqlite3.Connection, table: str) -> set:
    return {row[1] for row in connection.execute(
        'PRAGMA table_info("%s")' % table
    )}


def _canonical_source_digest(session_row: tuple,
                             messages: List[tuple],
                             parts: List[tuple]) -> str:
    payload = {
        "session": list(session_row),
        "messages": [list(row) for row in messages],
        "parts": [list(row) for row in parts],
    }
    encoded = (json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ) + "\n").encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_message_semantics(raw: dict, diagnostics: List[ZCodeDiagnostic],
                            message_index: int) -> Tuple[str, Optional[str]]:
    role = raw.get("role")
    if role not in ("user", "assistant"):
        _diag(diagnostics, "UNKNOWN_MESSAGE_ROLE", message_index)
        return "unknown", None

    semantics = raw.get("semantics")
    if not isinstance(semantics, dict):
        _diag(diagnostics, "MISSING_MESSAGE_SEMANTICS", message_index)
        return "unknown", role

    transcript = semantics.get("transcriptVisibility")
    ui = semantics.get("uiVisibility")
    if transcript not in (_VISIBLE, _HIDDEN) or ui not in (_VISIBLE, _HIDDEN):
        _diag(diagnostics, "UNKNOWN_VISIBILITY", message_index)
        return "unknown", role

    if transcript == _HIDDEN or ui == _HIDDEN:
        synthetic = raw.get("synthetic")
        source = raw.get("source")
        kind = semantics.get("kind")
        if synthetic is True and kind in ("todo_reminder", "background_notification"):
            return "hidden", role
        if synthetic is True and source in ("todo_reminder", "background_task"):
            return "hidden", role
        _diag(diagnostics, "UNSUPPORTED_HIDDEN_MESSAGE", message_index)
        return "hidden", role

    kind = semantics.get("kind")
    origin = semantics.get("origin")
    if role == "user":
        if kind != "user_prompt" or origin != "real_user":
            _diag(diagnostics, "UNKNOWN_VISIBLE_USER_SEMANTICS", message_index)
            return "unknown", role
        return "visible_user", role

    if kind != "assistant_response" or origin != "agent_runtime":
        _diag(diagnostics, "UNKNOWN_VISIBLE_ASSISTANT_SEMANTICS", message_index)
        return "unknown", role
    return "visible_assistant", role


def parse_zcode_session(db_path: Path, session_id: str) -> ZCodeParseResult:
    """Parse one explicit ZCode SQLite session into a privacy-safe model."""
    diagnostics: List[ZCodeDiagnostic] = []
    text_items: List[ZCodeTextItem] = []
    tool_events: List[ZCodeToolEvent] = []

    if not isinstance(session_id, str) or not session_id:
        _diag(diagnostics, "INVALID_SESSION_SELECTION")
        return ZCodeParseResult(
            (), (), tuple(diagnostics), "NON_COMPLETE", False, None,
            0, 0, 0, 0,
        )

    path = Path(db_path)
    if not path.is_file() or path.is_symlink():
        _diag(diagnostics, "SOURCE_UNREADABLE")
        return ZCodeParseResult(
            (), (), tuple(diagnostics), "NON_COMPLETE", False, None,
            0, 0, 0, 0,
        )

    connection: Optional[sqlite3.Connection] = None
    source_stable = False
    session_row: Optional[tuple] = None
    message_rows: List[tuple] = []
    part_rows: List[tuple] = []
    try:
        connection = sqlite3.connect(
            "file:%s?mode=ro" % path.as_posix(), uri=True)
        connection.execute("PRAGMA query_only=ON")
        for table, required in _REQUIRED_TABLE_COLUMNS.items():
            columns = _table_columns(connection, table)
            if not required.issubset(columns):
                _diag(diagnostics, "UNSUPPORTED_DATABASE_SCHEMA")
                raise ValueError("unsupported schema")

        connection.execute("BEGIN")
        session_row = connection.execute(
            """
            SELECT id, title, directory, version, time_created, time_updated
            FROM session
            WHERE id = ?
            """,
            (session_id,),
        ).fetchone()
        if session_row is None:
            _diag(diagnostics, "SESSION_NOT_FOUND")
            connection.rollback()
            return ZCodeParseResult(
                (), (), tuple(diagnostics), "NON_COMPLETE", False, None,
                0, 0, 0, 0,
            )

        message_rows = connection.execute(
            """
            SELECT id, data, sequence
            FROM message
            WHERE session_id = ?
            ORDER BY sequence, id
            """,
            (session_id,),
        ).fetchall()
        part_rows = connection.execute(
            """
            SELECT id, message_id, data, sequence
            FROM part
            WHERE session_id = ?
            ORDER BY message_id, sequence, id
            """,
            (session_id,),
        ).fetchall()
        connection.commit()
        source_stable = True
    except (sqlite3.Error, ValueError):
        if connection is not None:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
        if not diagnostics:
            _diag(diagnostics, "SOURCE_UNREADABLE")
    finally:
        if connection is not None:
            connection.close()

    if session_row is None:
        return ZCodeParseResult(
            (), (), tuple(diagnostics), "NON_COMPLETE", False, None,
            0, 0, 0, 0,
        )

    message_seen_sequences = Counter()
    message_info: Dict[str, Tuple[int, str, str]] = {}
    hidden_count = 0

    for message_index, (raw_message_id, raw_data, sequence) in enumerate(
            message_rows, 1):
        if not isinstance(sequence, int):
            _diag(diagnostics, "INVALID_MESSAGE_SEQUENCE", message_index)
        else:
            message_seen_sequences[sequence] += 1

        try:
            message = json.loads(raw_data)
        except (TypeError, json.JSONDecodeError):
            _diag(diagnostics, "MALFORMED_MESSAGE_JSON", message_index)
            continue
        if not isinstance(message, dict):
            _diag(diagnostics, "INVALID_MESSAGE_JSON", message_index)
            continue

        classification, role = _safe_message_semantics(
            message, diagnostics, message_index)
        if classification == "hidden":
            hidden_count += 1
        if isinstance(raw_message_id, str) and raw_message_id:
            message_info[raw_message_id] = (
                message_index, classification, role or "unknown")
        else:
            _diag(diagnostics, "INVALID_MESSAGE_ID", message_index)

    if any(count > 1 for count in message_seen_sequences.values()):
        _diag(diagnostics, "DUPLICATE_MESSAGE_SEQUENCE")

    parts_by_message: Dict[str, List[Tuple[str, str, int]]] = {}
    for raw_part_id, raw_message_id, raw_data, sequence in part_rows:
        if not isinstance(raw_message_id, str) or raw_message_id not in message_info:
            _diag(diagnostics, "BROKEN_PART_MESSAGE_LINK")
            continue
        parts_by_message.setdefault(raw_message_id, []).append(
            (raw_part_id, raw_data, sequence))

    reasoning_count = 0
    record_no = 0
    for raw_message_id, (message_index, classification, role) in sorted(
            message_info.items(), key=lambda item: item[1][0]):
        rows = parts_by_message.get(raw_message_id, [])
        part_sequences = Counter(
            row[2] for row in rows if isinstance(row[2], int))
        if any(count > 1 for count in part_sequences.values()):
            _diag(diagnostics, "DUPLICATE_PART_SEQUENCE", message_index)

        for part_index, (_, raw_data, sequence) in enumerate(
                sorted(rows, key=lambda row: (
                    row[2] if isinstance(row[2], int) else 2**63,
                    str(row[0]),
                )), 1):
            if not isinstance(sequence, int):
                _diag(diagnostics, "INVALID_PART_SEQUENCE",
                      message_index, part_index)
            try:
                part = json.loads(raw_data)
            except (TypeError, json.JSONDecodeError):
                _diag(diagnostics, "MALFORMED_PART_JSON",
                      message_index, part_index)
                continue
            if not isinstance(part, dict) or not isinstance(part.get("type"), str):
                _diag(diagnostics, "INVALID_PART",
                      message_index, part_index)
                continue

            kind = part["type"]
            if classification == "hidden":
                # Hidden/model-only bodies are deliberately never retained.
                continue

            if kind == "text":
                if classification not in ("visible_user", "visible_assistant"):
                    _diag(diagnostics, "TEXT_ON_UNKNOWN_MESSAGE",
                          message_index, part_index)
                    continue
                text = part.get("text")
                if not isinstance(text, str):
                    _diag(diagnostics, "INVALID_TEXT_PART",
                          message_index, part_index)
                    continue
                if len(text) > _MAX_TEXT_CHARS:
                    _diag(diagnostics, "TEXT_LIMIT_EXCEEDED",
                          message_index, part_index)
                    continue
                record_no += 1
                text_items.append(ZCodeTextItem(
                    "record-%06d" % record_no,
                    message_index,
                    part_index,
                    role,
                    "ordinary_%s" % role,
                    text,
                ))
            elif kind == "reasoning":
                reasoning_count += 1
            elif kind == "tool":
                tool = part.get("tool")
                if not isinstance(tool, str) or not _SAFE_TOOL_NAME.fullmatch(tool):
                    _diag(diagnostics, "INVALID_TOOL_NAME",
                          message_index, part_index)
                    tool = None
                state = part.get("state")
                status = state.get("status") if isinstance(state, dict) else None
                if status is not None and not isinstance(status, str):
                    _diag(diagnostics, "INVALID_TOOL_STATUS",
                          message_index, part_index)
                    status = None
                record_no += 1
                tool_events.append(ZCodeToolEvent(
                    "record-%06d" % record_no,
                    message_index,
                    part_index,
                    tool,
                    "tool-%06d" % (len(tool_events) + 1),
                    status,
                ))
            elif kind in ("step-start", "step-finish", "file", "timeline"):
                # Known structure, but no raw body is retained in v0.2 first pass.
                pass
            else:
                _diag(diagnostics, "UNKNOWN_PART_TYPE",
                      message_index, part_index)

    source_digest = (
        _canonical_source_digest(session_row, message_rows, part_rows)
        if source_stable else None
    )
    completeness = (
        "COMPLETE" if source_stable and not diagnostics else "NON_COMPLETE"
    )
    return ZCodeParseResult(
        tuple(text_items),
        tuple(tool_events),
        tuple(diagnostics),
        completeness,
        source_stable,
        source_digest,
        len(message_rows),
        len(part_rows),
        hidden_count,
        reasoning_count,
    )
