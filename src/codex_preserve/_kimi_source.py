"""Internal parser for one selected persisted Kimi Code session.

The first Kimi adapter reads only state.json and agents/main/wire.jsonl from an
explicit session directory. It never discovers sessions, reads subagent bodies,
or copies raw tool/debug payloads into its returned safe model.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Dict, List, Optional, Tuple


RENDER = "RENDER"
KNOWN_IGNORED_OR_SUMMARIZED = "KNOWN_IGNORED_OR_SUMMARIZED"
UNKNOWN = "UNKNOWN"

_SUPPORTED_PROTOCOLS = frozenset(("1.4",))
_SAFE_TOOL_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}\Z")
_MAX_TEXT_CHARS = 65536

_KNOWN_IGNORED_TOP_TYPES = frozenset((
    "config.update",
    "context.undo",
    "full_compaction.begin",
    "full_compaction.complete",
    "llm.request",
    "llm.tools_snapshot",
    "mcp.tools_discovered",
    "permission.set_mode",
    "tools.register_user_tool",
    "tools.set_active_tools",
    "tools.update_store",
    "turn.cancel",
    "turn.steer",
    "usage.record",
))

_KNOWN_USER_CONTEXT_ORIGINS = frozenset((
    "injection",
    "system_trigger",
    "skill_activation",
))


@dataclass(frozen=True)
class KimiDiagnostic:
    code: str
    line: int


@dataclass(frozen=True)
class KimiTextItem:
    record_id: str
    line: int
    role: str
    semantic_kind: str
    text: str


@dataclass(frozen=True)
class KimiToolEvent:
    record_id: str
    line: int
    kind: str
    tool_name: Optional[str] = None
    tool_ref: Optional[str] = None
    is_error: Optional[bool] = None


@dataclass(frozen=True)
class KimiParseResult:
    text_items: Tuple[KimiTextItem, ...]
    tool_events: Tuple[KimiToolEvent, ...]
    diagnostics: Tuple[KimiDiagnostic, ...]
    completeness: str
    protocol_version: Optional[str]
    mirrored_turn_prompt_count: int
    source_stable: bool
    state_sha256: Optional[str]
    state_size_bytes: Optional[int]
    wire_sha256: Optional[str]
    wire_size_bytes: Optional[int]


def _diag(items: List[KimiDiagnostic], code: str, line: int) -> None:
    items.append(KimiDiagnostic(code, line))


def _stat_identity(info: os.stat_result) -> tuple:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _stable_read(path: Path) -> Tuple[Optional[bytes], bool]:
    try:
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            first = handle.read()
            after = os.fstat(handle.fileno())
        with path.open("rb") as handle:
            second_before = os.fstat(handle.fileno())
            second = handle.read()
            second_after = os.fstat(handle.fileno())
        current = path.stat()
    except OSError:
        return None, False
    identities = {
        _stat_identity(before),
        _stat_identity(after),
        _stat_identity(second_before),
        _stat_identity(second_after),
        _stat_identity(current),
    }
    return first, len(identities) == 1 and first == second


def _text_blocks(value: object, diagnostics: List[KimiDiagnostic],
                 line: int) -> Tuple[str, ...]:
    if not isinstance(value, list):
        _diag(diagnostics, "INVALID_MESSAGE_CONTENT", line)
        return ()
    texts: List[str] = []
    for block in value:
        if not isinstance(block, dict) or not isinstance(block.get("type"), str):
            _diag(diagnostics, "UNKNOWN_MESSAGE_BLOCK", line)
            continue
        if block["type"] != "text":
            _diag(diagnostics, "UNKNOWN_MESSAGE_BLOCK", line)
            continue
        text = block.get("text")
        if not isinstance(text, str):
            _diag(diagnostics, "INVALID_TEXT_BLOCK", line)
            continue
        if len(text) > _MAX_TEXT_CHARS:
            _diag(diagnostics, "TEXT_LIMIT_EXCEEDED", line)
            continue
        texts.append(text)
    return tuple(texts)


def parse_kimi_session(session_dir: Path) -> KimiParseResult:
    """Parse one explicit Kimi Code session directory into a privacy-safe model."""
    session_dir = Path(session_dir)
    diagnostics: List[KimiDiagnostic] = []
    text_items: List[KimiTextItem] = []
    tool_events: List[KimiToolEvent] = []
    protocol_version: Optional[str] = None
    canonical_user_hashes: List[str] = []
    prompt_hashes: List[str] = []
    tool_ids: Dict[str, str] = {}
    pending_tool_results: List[Tuple[int, str, Optional[bool]]] = []

    state_path = session_dir / "state.json"
    wire_path = session_dir / "agents" / "main" / "wire.jsonl"
    if (not session_dir.is_dir() or session_dir.is_symlink() or
            wire_path.is_symlink() or state_path.is_symlink()):
        _diag(diagnostics, "INVALID_SOURCE_SELECTION", 0)
        return KimiParseResult(
            (), (), tuple(diagnostics), "NON_COMPLETE", None, 0, False,
            None, None, None, None,
        )

    state_bytes, state_stable = _stable_read(state_path)
    wire_bytes, wire_stable = _stable_read(wire_path)
    if state_bytes is None or wire_bytes is None:
        _diag(diagnostics, "SOURCE_UNREADABLE", 0)
        return KimiParseResult(
            (), (), tuple(diagnostics), "NON_COMPLETE", None, 0, False,
            None, None, None, None,
        )
    source_stable = state_stable and wire_stable
    if not source_stable:
        _diag(diagnostics, "SOURCE_CHANGED", 0)

    try:
        state = json.loads(state_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        state = None
    if not isinstance(state, dict):
        _diag(diagnostics, "INVALID_STATE_JSON", 0)

    record_no = 0
    for line_no, payload in enumerate(wire_bytes.splitlines(), 1):
        if not payload:
            _diag(diagnostics, "MALFORMED_WIRE_JSONL", line_no)
            continue
        try:
            raw = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            _diag(diagnostics, "MALFORMED_WIRE_JSONL", line_no)
            continue
        if not isinstance(raw, dict) or not isinstance(raw.get("type"), str):
            _diag(diagnostics, "INVALID_RECORD", line_no)
            continue
        kind = raw["type"]
        record_no += 1
        record_id = "record-%06d" % record_no

        if kind == "metadata":
            value = raw.get("protocol_version")
            if not isinstance(value, str):
                _diag(diagnostics, "PROTOCOL_NOT_ESTABLISHED", line_no)
            elif protocol_version is None:
                protocol_version = value
                if value not in _SUPPORTED_PROTOCOLS:
                    _diag(diagnostics, "UNSUPPORTED_PROTOCOL", line_no)
            elif protocol_version != value:
                _diag(diagnostics, "MIXED_PROTOCOL_VERSION", line_no)
            continue

        if kind == "context.append_message":
            message = raw.get("message")
            if not isinstance(message, dict):
                _diag(diagnostics, "INVALID_MESSAGE", line_no)
                continue
            role = message.get("role")
            origin = message.get("origin")
            origin_kind = origin.get("kind") if isinstance(origin, dict) else None
            if role == "user" and origin_kind == "user":
                texts = _text_blocks(message.get("content"), diagnostics, line_no)
                for text in texts:
                    canonical_user_hashes.append(
                        hashlib.sha256(text.encode("utf-8")).hexdigest())
                    text_items.append(KimiTextItem(
                        record_id, line_no, "user", "ordinary_user", text))
            elif role == "user" and origin_kind in _KNOWN_USER_CONTEXT_ORIGINS:
                # Known model/context machinery: deliberately do not retain body.
                pass
            elif role == "system":
                # Observed system context is not ordinary user conversation.
                pass
            else:
                _diag(diagnostics, "UNKNOWN_MESSAGE_ORIGIN", line_no)
            continue

        if kind == "turn.prompt":
            input_rows = raw.get("input")
            if not isinstance(input_rows, list):
                _diag(diagnostics, "INVALID_TURN_PROMPT", line_no)
                continue
            prompt_texts: List[str] = []
            for block in input_rows:
                if not isinstance(block, dict) or block.get("type") != "text":
                    _diag(diagnostics, "UNKNOWN_TURN_PROMPT_BLOCK", line_no)
                    continue
                text = block.get("text")
                if not isinstance(text, str):
                    _diag(diagnostics, "INVALID_TURN_PROMPT", line_no)
                    continue
                prompt_texts.append(text)
            if prompt_texts:
                joined = "".join(prompt_texts)
                prompt_hashes.append(hashlib.sha256(
                    joined.encode("utf-8")).hexdigest())
            continue

        if kind == "context.append_loop_event":
            event = raw.get("event")
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                _diag(diagnostics, "INVALID_LOOP_EVENT", line_no)
                continue
            event_type = event["type"]
            if event_type == "content.part":
                part = event.get("part")
                if not isinstance(part, dict) or not isinstance(part.get("type"), str):
                    _diag(diagnostics, "INVALID_CONTENT_PART", line_no)
                    continue
                part_type = part["type"]
                if part_type == "text":
                    text = part.get("text")
                    if not isinstance(text, str):
                        _diag(diagnostics, "INVALID_ASSISTANT_TEXT", line_no)
                    elif len(text) > _MAX_TEXT_CHARS:
                        _diag(diagnostics, "TEXT_LIMIT_EXCEEDED", line_no)
                    else:
                        text_items.append(KimiTextItem(
                            record_id, line_no, "assistant",
                            "ordinary_assistant", text))
                elif part_type == "think":
                    # Thinking is persisted debug/model context, not exported text.
                    pass
                else:
                    _diag(diagnostics, "UNKNOWN_CONTENT_PART", line_no)
            elif event_type == "tool.call":
                name = event.get("name")
                if not isinstance(name, str) or not _SAFE_TOOL_NAME.fullmatch(name):
                    _diag(diagnostics, "INVALID_TOOL_NAME", line_no)
                    name = None
                ref = "tool-%06d" % (len(tool_ids) + 1)
                raw_id = event.get("toolCallId")
                if isinstance(raw_id, str) and raw_id:
                    if raw_id in tool_ids:
                        _diag(diagnostics, "DUPLICATE_TOOL_ID", line_no)
                    else:
                        tool_ids[raw_id] = ref
                else:
                    _diag(diagnostics, "MISSING_TOOL_ID", line_no)
                tool_events.append(KimiToolEvent(
                    record_id, line_no, "tool_call", name, ref, None))
            elif event_type == "tool.result":
                raw_id = event.get("toolCallId")
                result = event.get("result")
                is_error = result.get("isError") if isinstance(result, dict) else None
                if is_error is not None and not isinstance(is_error, bool):
                    is_error = None
                    _diag(diagnostics, "INVALID_TOOL_RESULT_STATUS", line_no)
                if isinstance(raw_id, str) and raw_id:
                    pending_tool_results.append((line_no, raw_id, is_error))
                else:
                    _diag(diagnostics, "MISSING_TOOL_RESULT_REF", line_no)
            elif event_type in ("step.begin", "step.end"):
                pass
            else:
                _diag(diagnostics, "UNKNOWN_LOOP_EVENT", line_no)
            continue

        if kind == "context.apply_compaction":
            # Summary bodies may contain prior conversation. Count the event but
            # never retain raw summary/contextSummary text.
            continue

        if kind in _KNOWN_IGNORED_TOP_TYPES:
            continue

        _diag(diagnostics, "UNKNOWN_RECORD", line_no)

    if protocol_version is None:
        _diag(diagnostics, "PROTOCOL_NOT_ESTABLISHED", 0)

    user_counts = Counter(canonical_user_hashes)
    prompt_counts = Counter(prompt_hashes)
    unmatched = prompt_counts - user_counts
    if unmatched:
        _diag(diagnostics, "UNMATCHED_TURN_PROMPT", 0)
    mirrored_count = sum((prompt_counts & user_counts).values())

    for line_no, raw_id, is_error in pending_tool_results:
        ref = tool_ids.get(raw_id)
        if ref is None:
            _diag(diagnostics, "UNMATCHED_TOOL_RESULT", line_no)
        record_no += 1
        tool_events.append(KimiToolEvent(
            "record-%06d" % record_no,
            line_no,
            "tool_result",
            None,
            ref,
            is_error,
        ))

    blocking = bool(diagnostics)
    completeness = "COMPLETE" if source_stable and not blocking else "NON_COMPLETE"
    return KimiParseResult(
        tuple(text_items),
        tuple(tool_events),
        tuple(diagnostics),
        completeness,
        protocol_version,
        mirrored_count,
        source_stable,
        hashlib.sha256(state_bytes).hexdigest() if source_stable else None,
        len(state_bytes) if source_stable else None,
        hashlib.sha256(wire_bytes).hexdigest() if source_stable else None,
        len(wire_bytes) if source_stable else None,
    )
