"""Internal, loss-averse readable projection of a Claude ParseResult.

This module accepts only G4a's already classified data. Source line order is
the deterministic presentation order, not an active conversation path. No
leaf hint, branch, or graph gap removes persisted safe text.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Optional, Tuple

from ._claude_source import (
    KNOWN_IGNORED_OR_SUMMARIZED, RENDER, UNKNOWN, ParseResult,
)


@dataclass(frozen=True)
class ReadableBlock:
    index: int
    semantic_kind: Optional[str]
    text: str


@dataclass(frozen=True)
class ReadableNode:
    # A record occurrence has its own identity even if a graph UUID repeats.
    record_id: str
    source_line: int
    node_id: Optional[str]
    parent_id: Optional[str]
    parent_record_id: Optional[str]
    parent_link: Optional[str]
    sidechain: Optional[bool]
    role: Optional[str]
    semantic_kind: Optional[str]
    source_policy: str
    text_blocks: Tuple[ReadableBlock, ...]


@dataclass(frozen=True)
class ReadableEvent:
    record_id: str
    source_line: int
    kind: str
    block_index: Optional[int] = None
    tool_name: Optional[str] = None
    tool_ref: Optional[str] = None
    result_present: Optional[bool] = None


@dataclass(frozen=True)
class ReadableLeafHint:
    record_id: str
    source_line: int
    leaf_hint_id: Optional[str]
    leaf_hint_explicit: Optional[bool]
    leaf_hint_rewound: Optional[bool]


@dataclass(frozen=True)
class ReadableBranchPoint:
    parent_node_id: str
    # None means the normalized node ID has multiple persisted occurrences.
    parent_record_id: Optional[str]
    child_record_ids: Tuple[str, ...]


@dataclass(frozen=True)
class ReadableDiagnosticSummary:
    # Codes and coordinates come from G4a, never from raw source values.
    counts_by_code: Tuple[Tuple[str, int], ...]
    graph_gap_count: int
    unknown_count: int  # unknown blocks plus unknown records without such blocks


@dataclass(frozen=True)
class ReadableGraph:
    source_completeness: str
    source_entrypoint: str
    nodes: Tuple[ReadableNode, ...]
    events: Tuple[ReadableEvent, ...]
    leaf_hints: Tuple[ReadableLeafHint, ...]
    branch_points: Tuple[ReadableBranchPoint, ...]
    diagnostics: ReadableDiagnosticSummary
    readable_node_count: int
    rendered_text_block_count: int
    branch_count: int
    graph_gap_count: int
    unknown_count: int
    active_head_id: None = None
    presentation_order: str = "persisted_source_line"


def build_readable_graph(source: ParseResult) -> ReadableGraph:
    """Preserve every G4a-approved text block, without choosing a path."""
    if not isinstance(source, ParseResult):
        raise TypeError("source must be a Claude ParseResult")

    # G4a records are in physical source order. The ordinal identifies an
    # occurrence independently of its (possibly duplicated) normalized UUID.
    ordered = sorted(enumerate(source.records, 1),
                     key=lambda pair: (pair[1].line, pair[0]))
    record_ids = {ordinal: "record-%06d" % ordinal for ordinal, _ in ordered}
    occurrences = defaultdict(list)
    for ordinal, record in ordered:
        if record.node_id is not None:
            occurrences[record.node_id].append(record_ids[ordinal])

    nodes = []
    events = []
    hints = []
    children = defaultdict(lambda: defaultdict(list))
    for ordinal, record in ordered:
        record_id = record_ids[ordinal]
        text_blocks = tuple(
            ReadableBlock(block.index, block.semantic_kind, block.text)
            for block in sorted(record.blocks, key=lambda item: item.index)
            if block.policy == RENDER and block.text is not None
        )
        if record.kind in ("user", "assistant", "attachment", "system") or \
                record.node_id is not None or text_blocks:
            parent_occurrences = occurrences.get(record.parent_id, ())
            parent_record_id = (parent_occurrences[0]
                                if record.parent_link == "linked" and
                                len(parent_occurrences) == 1 else None)
            nodes.append(ReadableNode(
                record_id, record.line, record.node_id, record.parent_id,
                parent_record_id, record.parent_link, record.sidechain,
                record.kind if record.kind in ("user", "assistant") else None,
                record.semantic_kind, record.policy, text_blocks,
            ))
            if (record.parent_link == "linked" and
                    record.parent_id is not None and record.node_id is not None):
                children[record.parent_id][record.node_id].append(record_id)

        if record.kind == "last-prompt":
            hints.append(ReadableLeafHint(
                record_id, record.line, record.leaf_hint_id,
                record.leaf_hint_explicit, record.leaf_hint_rewound,
            ))
        if record.semantic_kind == "compact_boundary" and \
                record.policy == KNOWN_IGNORED_OR_SUMMARIZED:
            events.append(ReadableEvent(record_id, record.line, "compact_boundary"))
        for block in sorted(record.blocks, key=lambda item: item.index):
            if block.policy != KNOWN_IGNORED_OR_SUMMARIZED:
                continue
            if block.kind == "tool_use":
                events.append(ReadableEvent(
                    record_id, record.line, "tool_use", block.index,
                    tool_name=block.tool_name, tool_ref=block.tool_ref,
                ))
            elif block.kind == "tool_result":
                events.append(ReadableEvent(
                    record_id, record.line, "tool_result_present", block.index,
                    tool_ref=block.tool_ref,
                    result_present=block.result_present,
                ))

    source_line_by_record_id = {node.record_id: node.source_line for node in nodes}
    branch_rows = []
    for parent_id, child_groups in children.items():
        if len(child_groups) <= 1:
            continue
        distinct_children = sorted(
            child_groups.items(),
            key=lambda pair: (
                source_line_by_record_id[pair[1][0]], pair[0]
            ),
        )
        branch_rows.append((
            min(source_line_by_record_id[group[0]]
                for _, group in distinct_children),
            parent_id,
            tuple(group[0] for _, group in distinct_children),
        ))
    branches = tuple(
        ReadableBranchPoint(
            parent_id,
            occurrences[parent_id][0]
            if len(occurrences[parent_id]) == 1 else None,
            child_record_ids,
        )
        for _, parent_id, child_record_ids in sorted(branch_rows)
    )
    counts = Counter(item.code for item in source.diagnostics)
    gap_count = counts["GRAPH_LINK_GAP"]
    unknown_count = sum(
        sum(block.policy == UNKNOWN for block in record.blocks) or
        (record.policy == UNKNOWN)
        for record in source.records
    )
    summary = ReadableDiagnosticSummary(
        tuple(sorted(counts.items())), gap_count, unknown_count,
    )
    return ReadableGraph(
        source.completeness, source.entrypoint, tuple(nodes), tuple(events),
        tuple(hints), branches, summary,
        sum(bool(node.text_blocks) for node in nodes),
        sum(len(node.text_blocks) for node in nodes),
        source.branch_count, gap_count, unknown_count,
    )
