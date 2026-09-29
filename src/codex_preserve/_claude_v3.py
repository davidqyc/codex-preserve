"""Claude Code adapter projection into the internal schema-3 package spec."""

from __future__ import annotations

from dataclasses import asdict
from typing import List

from ._claude_payload import build_claude_payload
from ._claude_readable import build_readable_graph
from ._claude_source import ParseResult
from ._v3_package import V3PackageSpec


ADAPTER_NAME = "claude-code-local"
ADAPTER_VERSION = "1"


def _render_markdown(source: ParseResult) -> str:
    graph = build_readable_graph(source)
    lines: List[str] = [
        "# Conversation",
        "",
        "Provider: Claude Code",
        "",
        "This file preserves safe readable content that was persisted in the "
        "selected local source at export time.",
        "",
        "It does not attest that every message visible in the Claude UI had "
        "already been flushed to disk, and it does not attest that the session "
        "had ended.",
        "",
    ]
    if graph.branch_count or graph.graph_gap_count:
        lines.extend([
            "## Preservation notes",
            "",
            "- No active/current branch was guessed.",
        ])
        if graph.branch_count:
            lines.append(
                "- %d persisted branch point(s) were preserved."
                % graph.branch_count)
        if graph.graph_gap_count:
            lines.append(
                "- %d persisted parent-link gap(s) were preserved."
                % graph.graph_gap_count)
        lines.append("")

    for node in graph.nodes:
        if not node.text_blocks:
            continue
        if node.semantic_kind == "compact_summary":
            heading = "Compaction summary"
        elif node.role == "user":
            heading = "User"
        elif node.role == "assistant":
            heading = "Assistant"
        else:
            heading = "Persisted text"
        lines.extend(["## %s" % heading, ""])
        for block in node.text_blocks:
            lines.append(block.text)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def claude_v3_spec(source: ParseResult) -> V3PackageSpec:
    graph = build_readable_graph(source)
    payload = build_claude_payload(graph, source)
    receipt = payload.as_dict()
    receipt["active_head_selected"] = False
    return V3PackageSpec(
        provider="claude",
        adapter_name=ADAPTER_NAME,
        adapter_version=ADAPTER_VERSION,
        coverage_status=source.completeness,
        source_stable=source.source_stable,
        source_identity_scope="claude:selected_top_level_jsonl",
        source_identity_sha256=source.source_sha256,
        conversation_markdown=_render_markdown(source),
        provider_receipt=receipt,
    )
