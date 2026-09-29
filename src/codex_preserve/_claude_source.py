"""Internal, CLI-entrypoint-only parser for one selected Claude Code JSONL.

This module neither discovers sessions nor creates packages. Raw JSON values
exist only while one line is classified; the returned model contains allowed
text, normalized graph coordinates, and bounded structural facts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Tuple


RENDER = "RENDER"
KNOWN_IGNORED_OR_SUMMARIZED = "KNOWN_IGNORED_OR_SUMMARIZED"
UNKNOWN = "UNKNOWN"

_GRAPH_TYPES = frozenset(("user", "assistant", "attachment", "system"))
_BOOKKEEPING_TYPES = frozenset(("last-prompt", "queue-operation",
                                "file-history-snapshot", "title"))
_IGNORED_BLOCK_TYPES = frozenset(("thinking", "tool_use", "tool_result",
                                  "fallback"))
_ATTACHMENT_TYPES = frozenset((
    "total_tokens_reminder", "prompt_snapshot", "environment",
    "session_context", "instructions", "mcp_instructions", "skill_listing",
))
_STOP_REASONS = frozenset(("end_turn", "tool_use", "max_tokens",
                            "stop_sequence", "pause_turn", "refusal"))
_SAFE_TOOL_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,63}\Z")
_MAX_RENDER_CHARS = 65536
_RECORD_FIELDS = frozenset((
    "type", "uuid", "parentUuid", "isSidechain", "sessionId", "entrypoint",
    "version", "message", "isCompactSummary", "ownerAccountUuid",
    "ownerOrganizationUuid", "toolUseResult", "attachment", "subtype",
    "compactMetadata", "operation", "content", "messageId", "snapshot",
    "isSnapshotUpdate", "leafUuid", "lastPrompt", "title", "timestamp",
    "cwd", "gitBranch", "slug", "userType", "requestId", "isMeta",
    "sourceToolAssistantUUID", "permissionMode", "agentId", "teamName",
    "explicit", "rewound",
))
_MESSAGE_FIELDS = frozenset(("role", "content", "stop_reason", "id",
                             "model", "usage"))
_TEXT_FIELDS = frozenset(("type", "text"))


@dataclass(frozen=True)
class Diagnostic:
    code: str
    line: int
    block: Optional[int] = None


@dataclass(frozen=True)
class Block:
    policy: str
    kind: str
    index: int
    semantic_kind: Optional[str] = None
    text: Optional[str] = None
    tool_name: Optional[str] = None
    tool_ref: Optional[str] = None
    result_present: Optional[bool] = None


@dataclass(frozen=True)
class Record:
    line: int
    policy: str
    kind: str
    node_id: Optional[str] = None
    parent_id: Optional[str] = None
    parent_link: Optional[str] = None  # root, linked, missing, or invalid
    sidechain: Optional[bool] = None
    semantic_kind: Optional[str] = None
    attachment_kind: Optional[str] = None
    stop_reason: Optional[str] = None  # message-level fact only
    blocks: Tuple[Block, ...] = ()
    leaf_hint_id: Optional[str] = None  # never an active-head decision
    leaf_hint_explicit: Optional[bool] = None
    leaf_hint_rewound: Optional[bool] = None
    sidecar_relation: Optional[str] = None
    referenced_sidecar_present: Optional[bool] = None


@dataclass(frozen=True)
class ParseResult:
    records: Tuple[Record, ...]
    diagnostics: Tuple[Diagnostic, ...]
    completeness: str  # COMPLETE means source classification eligibility only
    entrypoint: str  # cli, unsupported, mixed, or absent
    node_count: int
    branch_count: int
    active_head_id: None = None  # selection belongs to a later stage
    source_stable: bool = False
    source_sha256: Optional[str] = None  # stable snapshot content identity only
    source_size_bytes: Optional[int] = None


def bounded_safe_view(result: ParseResult) -> dict:
    """Internal preview shape with fixed limits; never includes source paths."""
    return {
        "completeness": result.completeness,
        "entrypoint": result.entrypoint,
        "node_count": result.node_count,
        "branch_count": result.branch_count,
        "records": [
            {
                "line": record.line,
                "policy": record.policy,
                "kind": record.kind,
                "semantic_kind": record.semantic_kind,
                "node_id": record.node_id,
                "parent_id": record.parent_id,
                "parent_link": record.parent_link,
                "blocks": [
                    {"policy": block.policy, "kind": block.kind,
                     "semantic_kind": block.semantic_kind,
                     "text": block.text[:160] if block.text is not None else None,
                     "tool_name": block.tool_name, "tool_ref": block.tool_ref,
                     "result_present": block.result_present}
                    for block in record.blocks[:16]
                ],
            }
            for record in result.records[:32]
        ],
        "diagnostics": [
            {"code": item.code, "line": item.line, "block": item.block}
            for item in result.diagnostics[:64]
        ],
        "omitted_records": max(0, len(result.records) - 32),
        "omitted_diagnostics": max(0, len(result.diagnostics) - 64),
    }


def _diagnose(items: List[Diagnostic], code: str, line: int,
              block: Optional[int] = None) -> None:
    items.append(Diagnostic(code, line, block))


def _source_identity(info: os.stat_result) -> tuple:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _parse_blocks(content: object, semantic_kind: str, line: int,
                  diagnostics: List[Diagnostic], tool_ids: Dict[str, str],
                  pending_results: List[Tuple[int, int, str]]) -> Tuple[Block, ...]:
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list):
        _diagnose(diagnostics, "INVALID_MESSAGE_CONTENT", line)
        return ()
    blocks: List[Block] = []
    for index, raw in enumerate(content, 1):
        if not isinstance(raw, dict) or not isinstance(raw.get("type"), str):
            _diagnose(diagnostics, "UNKNOWN_BLOCK", line, index)
            blocks.append(Block(UNKNOWN, "unknown", index))
            continue
        kind = raw["type"]
        if kind == "text":
            if set(raw) - _TEXT_FIELDS:
                _diagnose(diagnostics, "EXTRA_TEXT_FIELD", line, index)
            value = raw.get("text")
            if not isinstance(value, str):
                _diagnose(diagnostics, "INVALID_TEXT_BLOCK", line, index)
                blocks.append(Block(UNKNOWN, "text", index))
            elif len(value) > _MAX_RENDER_CHARS:
                _diagnose(diagnostics, "TEXT_LIMIT_EXCEEDED", line, index)
                blocks.append(Block(UNKNOWN, "text", index))
            else:
                blocks.append(Block(RENDER, "text", index, semantic_kind, value))
        elif kind == "tool_use":
            name = raw.get("name")
            if not isinstance(name, str) or not _SAFE_TOOL_NAME.fullmatch(name):
                _diagnose(diagnostics, "INVALID_TOOL_NAME", line, index)
                name = None
            ref = "tool-%06d-%03d" % (line, index)
            raw_id = raw.get("id")
            if isinstance(raw_id, str) and raw_id:
                if raw_id in tool_ids:
                    _diagnose(diagnostics, "DUPLICATE_TOOL_ID", line, index)
                else:
                    tool_ids[raw_id] = ref
            else:
                _diagnose(diagnostics, "MISSING_TOOL_ID", line, index)
            blocks.append(Block(KNOWN_IGNORED_OR_SUMMARIZED, kind, index,
                                tool_name=name, tool_ref=ref))
        elif kind == "tool_result":
            raw_ref = raw.get("tool_use_id")
            if isinstance(raw_ref, str) and raw_ref:
                pending_results.append((line, index, raw_ref))
            else:
                _diagnose(diagnostics, "MISSING_TOOL_RESULT_REF", line, index)
            blocks.append(Block(KNOWN_IGNORED_OR_SUMMARIZED, kind, index,
                                result_present="content" in raw))
        elif kind in _IGNORED_BLOCK_TYPES:
            blocks.append(Block(KNOWN_IGNORED_OR_SUMMARIZED, kind, index))
        else:
            _diagnose(diagnostics, "UNKNOWN_BLOCK", line, index)
            blocks.append(Block(UNKNOWN, "unknown", index))
    return tuple(blocks)


def _sidecar_fact(raw: dict, source: Path, line: int,
                  diagnostics: List[Diagnostic]) -> Tuple[Optional[str], Optional[bool]]:
    """Recognize only explicit relative sidecar refs; inspect metadata, never body."""
    result = raw.get("toolUseResult")
    if not isinstance(result, dict):
        return None, None
    pointer = result.get("filePath", result.get("path"))
    if pointer is None:
        return None, None
    if not isinstance(pointer, str):
        _diagnose(diagnostics, "UNSAFE_SIDECAR_REFERENCE", line)
        return None, None
    relative = PurePosixPath(pointer)
    parts = relative.parts
    if (relative.is_absolute() or len(parts) < 2 or
            parts[0] not in ("tool-results", "subagents") or
            any(part in (".", "..") for part in parts)):
        _diagnose(diagnostics, "UNSAFE_SIDECAR_REFERENCE", line)
        return None, None
    relation = "tool_result" if parts[0] == "tool-results" else "subagent"
    target = source.parent
    try:
        for index, part in enumerate(parts):
            target = target / part
            mode = target.lstat().st_mode
            if index < len(parts) - 1 and not stat.S_ISDIR(mode):
                _diagnose(diagnostics, "SIDECAR_UNQUERYABLE", line)
                return relation, False
    except FileNotFoundError:
        _diagnose(diagnostics, "SIDECAR_MISSING", line)
        return relation, False
    except OSError:
        _diagnose(diagnostics, "SIDECAR_UNQUERYABLE", line)
        return relation, False
    if not stat.S_ISREG(mode):
        _diagnose(diagnostics, "SIDECAR_UNQUERYABLE", line)
        return relation, False
    return relation, True


def parse_claude_session(path: Path) -> ParseResult:
    """Parse an explicitly supplied top-level JSONL; never inspect sidecars.

    This is a provider-private source parser, not a public import or export API.
    The path and any raw parser exception are deliberately absent from output.
    """
    records: List[Record] = []
    diagnostics: List[Diagnostic] = []
    raw_nodes: List[Tuple[int, str, object]] = []
    leaf_hints: List[Tuple[int, object, object, object]] = []
    tool_ids: Dict[str, str] = {}
    pending_results: List[Tuple[int, int, str]] = []
    entrypoints = set()
    session_ids = set()
    source_stable = False
    source_sha256 = None
    source_size_bytes = None
    source = Path(path)
    if source.suffix != ".jsonl" or source.parent.name in ("subagents", "tool-results"):
        _diagnose(diagnostics, "INVALID_SOURCE_SELECTION", 0)
        return ParseResult((), tuple(diagnostics), "NON_COMPLETE", "absent", 0, 0)
    try:
        with source.open("rb") as handle:
            before = os.fstat(handle.fileno())
            parsed_hash = hashlib.sha256()
            for line_no, payload in enumerate(handle, 1):
                parsed_hash.update(payload)
                try:
                    raw = json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    _diagnose(diagnostics, "MALFORMED_JSONL", line_no)
                    continue
                if not isinstance(raw, dict):
                    _diagnose(diagnostics, "INVALID_RECORD", line_no)
                    records.append(Record(line_no, UNKNOWN, "unknown"))
                    continue
                if set(raw) - _RECORD_FIELDS:
                    _diagnose(diagnostics, "EXTRA_RECORD_FIELD", line_no)
                session_id = raw.get("sessionId")
                if isinstance(session_id, str) and session_id:
                    session_ids.add(session_id)
                kind = raw.get("type")
                if not isinstance(kind, str):
                    kind = None
                if kind not in _GRAPH_TYPES and kind not in _BOOKKEEPING_TYPES:
                    _diagnose(diagnostics, "UNKNOWN_RECORD", line_no)
                    safe_kind, policy = "unknown", UNKNOWN
                else:
                    safe_kind, policy = kind, KNOWN_IGNORED_OR_SUMMARIZED
                if kind in _GRAPH_TYPES:
                    entrypoint = raw.get("entrypoint")
                    entrypoints.add(entrypoint if isinstance(entrypoint, str) else "<invalid>")
                    if entrypoint != "cli":
                        _diagnose(diagnostics, "UNSUPPORTED_ENTRYPOINT", line_no)

                if kind in ("user", "assistant"):
                    if not isinstance(raw.get("sessionId"), str) or not raw["sessionId"]:
                        _diagnose(diagnostics, "MISSING_SESSION_ID", line_no)
                    message = raw.get("message")
                    role = message.get("role") if isinstance(message, dict) else None
                    if role != kind:
                        _diagnose(diagnostics, "INVALID_MESSAGE_ROLE", line_no)
                        blocks = ()
                    else:
                        if set(message) - _MESSAGE_FIELDS:
                            _diagnose(diagnostics, "EXTRA_MESSAGE_FIELD", line_no)
                        summary = raw.get("isCompactSummary", False)
                        if not isinstance(summary, bool) or (summary and kind != "user"):
                            _diagnose(diagnostics, "INVALID_SUMMARY_FLAG", line_no)
                            summary = False
                        semantic = "compact_summary" if summary else "ordinary_" + kind
                        blocks = _parse_blocks(message.get("content"), semantic,
                                               line_no, diagnostics, tool_ids,
                                               pending_results)
                    if any(b.policy == UNKNOWN for b in blocks):
                        policy = UNKNOWN
                    elif any(b.policy == RENDER for b in blocks):
                        policy = RENDER
                    else:
                        policy = KNOWN_IGNORED_OR_SUMMARIZED
                    stop = message.get("stop_reason") if isinstance(message, dict) else None
                    if stop is not None and (not isinstance(stop, str) or
                                             stop not in _STOP_REASONS):
                        _diagnose(diagnostics, "UNKNOWN_STOP_REASON", line_no)
                        stop = None
                    record = Record(line_no, policy, kind, semantic_kind=(
                        "compact_summary" if raw.get("isCompactSummary") is True and kind == "user"
                        else "ordinary_" + kind), stop_reason=stop, blocks=blocks)
                elif kind == "attachment":
                    attachment = raw.get("attachment")
                    attachment_type = attachment.get("type") if isinstance(attachment, dict) else None
                    if isinstance(attachment_type, str) and attachment_type in _ATTACHMENT_TYPES:
                        record = Record(line_no, policy, kind, attachment_kind=attachment_type)
                    else:
                        _diagnose(diagnostics, "UNKNOWN_ATTACHMENT", line_no)
                        record = Record(line_no, UNKNOWN, kind, attachment_kind="unknown")
                elif kind == "system":
                    if raw.get("subtype") != "compact_boundary":
                        _diagnose(diagnostics, "UNKNOWN_SYSTEM_SUBTYPE", line_no)
                        policy = UNKNOWN
                    record = Record(line_no, policy, kind,
                                    semantic_kind="compact_boundary" if policy != UNKNOWN else None)
                else:
                    record = Record(line_no, policy, safe_kind)
                    if kind == "last-prompt":
                        explicit = raw.get("explicit")
                        rewound = raw.get("rewound")
                        if explicit is not None and not isinstance(explicit, bool):
                            _diagnose(diagnostics, "INVALID_LEAF_HINT_FLAG", line_no)
                            explicit = None
                        if rewound is not None and not isinstance(rewound, bool):
                            _diagnose(diagnostics, "INVALID_LEAF_HINT_FLAG", line_no)
                            rewound = None
                        leaf_hints.append((len(records), raw.get("leafUuid"),
                                           explicit, rewound))

                if kind in _GRAPH_TYPES or policy == UNKNOWN:
                    node_uuid = raw.get("uuid")
                    parent_uuid = raw.get("parentUuid")
                    if not isinstance(node_uuid, str) or not node_uuid:
                        _diagnose(diagnostics, "MISSING_UUID", line_no)
                    elif "parentUuid" not in raw or (parent_uuid is not None and
                            (not isinstance(parent_uuid, str) or not parent_uuid)):
                        _diagnose(diagnostics, "INVALID_PARENT_UUID", line_no)
                    else:
                        raw_nodes.append((len(records), node_uuid, parent_uuid))
                    sidechain = raw.get("isSidechain")
                    if sidechain is not None and not isinstance(sidechain, bool):
                        _diagnose(diagnostics, "INVALID_SIDECHAIN_FLAG", line_no)
                    elif isinstance(sidechain, bool):
                        record = replace(record, sidechain=sidechain)
                relation, present = ((None, None) if kind not in ("user", "assistant")
                                     else _sidecar_fact(raw, source, line_no, diagnostics))
                if relation is not None:
                    record = replace(record, sidecar_relation=relation,
                                     referenced_sidecar_present=present)
                records.append(record)
            after = os.fstat(handle.fileno())
        with source.open("rb") as verification:
            verify_before = os.fstat(verification.fileno())
            verified_hash = hashlib.sha256()
            while True:
                block = verification.read(1024 * 1024)
                if not block:
                    break
                verified_hash.update(block)
            verify_after = os.fstat(verification.fileno())
        current = source.stat()
        identities = {_source_identity(info) for info in
                      (before, after, verify_before, verify_after, current)}
        if len(identities) != 1 or parsed_hash.digest() != verified_hash.digest():
            _diagnose(diagnostics, "SOURCE_CHANGED", 0)
        else:
            source_stable = True
            source_sha256 = parsed_hash.hexdigest()
            source_size_bytes = before.st_size
    except OSError:
        _diagnose(diagnostics, "SOURCE_UNREADABLE", 0)
    if len(session_ids) > 1:
        _diagnose(diagnostics, "MULTIPLE_SESSION_IDS", 0)

    node_ids: Dict[str, str] = {}
    for _, raw_uuid, _ in raw_nodes:
        if raw_uuid in node_ids:
            continue
        node_ids[raw_uuid] = "node-%06d" % (len(node_ids) + 1)
    children: Dict[str, set] = {}
    parents: Dict[str, Optional[str]] = {}
    parent_relations: Dict[str, Tuple[str, Optional[str]]] = {}
    seen_node_ids = set()
    for record_index, raw_uuid, raw_parent in raw_nodes:
        record = records[record_index]
        node_id = node_ids[raw_uuid]
        duplicate = node_id in seen_node_ids
        if duplicate:
            _diagnose(diagnostics, "DUPLICATE_UUID", record.line)
        seen_node_ids.add(node_id)
        if raw_parent is None:
            link, parent_id = "root", None
        elif raw_parent in node_ids:
            link, parent_id = "linked", node_ids[raw_parent]
            children.setdefault(parent_id, set()).add(node_id)
        else:
            link, parent_id = "missing", None
            _diagnose(diagnostics, "GRAPH_LINK_GAP", record.line)
        relation = (link, parent_id)
        if duplicate and parent_relations.get(node_id) != relation:
            _diagnose(diagnostics, "DUPLICATE_UUID_PARENT_CONFLICT", record.line)
        elif node_id not in parent_relations:
            parent_relations[node_id] = relation
            parents[node_id] = parent_id
        records[record_index] = replace(record, node_id=node_id,
                                        parent_id=parent_id, parent_link=link)
    branch_count = sum(len(child_ids) > 1 for child_ids in children.values())
    if branch_count:
        _diagnose(diagnostics, "GRAPH_BRANCH_PRESENT", 0)
    # Parent links have at most one edge each; walk chains without recursion.
    checked = set()
    for node_id in parents:
        if node_id in checked:
            continue
        path_seen = set()
        cursor = node_id
        while cursor is not None and cursor not in checked:
            if cursor in path_seen:
                _diagnose(diagnostics, "GRAPH_CYCLE", 0)
                break
            path_seen.add(cursor)
            cursor = parents.get(cursor)
        checked.update(path_seen)

    for record_index, raw_leaf, explicit, rewound in leaf_hints:
        record = records[record_index]
        leaf_id = node_ids.get(raw_leaf) if isinstance(raw_leaf, str) else None
        if leaf_id is None:
            _diagnose(diagnostics, "UNRESOLVED_LEAF_HINT", record.line)
        elif children.get(leaf_id, 0):
            _diagnose(diagnostics, "LEAF_HINT_HAS_DESCENDANTS", record.line)
        records[record_index] = replace(
            record,
            leaf_hint_id=leaf_id,
            leaf_hint_explicit=explicit,
            leaf_hint_rewound=rewound,
        )

    for line, block_index, raw_ref in pending_results:
        ref = tool_ids.get(raw_ref)
        if ref is None:
            _diagnose(diagnostics, "UNMATCHED_TOOL_RESULT", line, block_index)
        for record_index, record in enumerate(records):
            if record.line != line:
                continue
            blocks = tuple(replace(block, tool_ref=ref)
                           if block.index == block_index and block.kind == "tool_result"
                           else block for block in record.blocks)
            records[record_index] = replace(record, blocks=blocks)
            break

    if not entrypoints:
        entrypoint_state = "absent"
    elif entrypoints == {"cli"}:
        entrypoint_state = "cli"
    elif len(entrypoints) > 1:
        entrypoint_state = "mixed"
    else:
        entrypoint_state = "unsupported"
    if entrypoint_state != "cli":
        _diagnose(diagnostics, "CLI_ENTRYPOINT_NOT_ESTABLISHED", 0)
    blocking = any(item.code not in ("GRAPH_BRANCH_PRESENT",
                                     "LEAF_HINT_HAS_DESCENDANTS",
                                     "SIDECAR_MISSING", "SIDECAR_UNQUERYABLE")
                   for item in diagnostics)
    # A branch has no selected active path in G4a, so it also lacks COMPLETE.
    completeness = "NON_COMPLETE" if blocking or branch_count else "COMPLETE"
    return ParseResult(tuple(records), tuple(diagnostics), completeness,
                       entrypoint_state, len(node_ids), branch_count,
                       source_stable=source_stable,
                       source_sha256=source_sha256,
                       source_size_bytes=source_size_bytes)
