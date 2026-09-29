"""Kimi Code adapter projection into the internal schema-3 package spec."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from typing import List

from ._kimi_source import KimiParseResult
from ._v3_package import V3PackageSpec


ADAPTER_NAME = "kimi-code-local"
ADAPTER_VERSION = "1"


def _source_identity(source: KimiParseResult):
    if not source.source_stable:
        return None
    payload = {
        "state": {
            "sha256": source.state_sha256,
            "bytes": source.state_size_bytes,
        },
        "wire": {
            "sha256": source.wire_sha256,
            "bytes": source.wire_size_bytes,
        },
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _render_markdown(source: KimiParseResult) -> str:
    lines: List[str] = [
        "# Conversation",
        "",
        "Provider: Kimi Code",
        "",
        "This file preserves safe readable content persisted in the selected "
        "local Kimi Code session source at export time.",
        "",
    ]
    for item in source.text_items:
        heading = "User" if item.role == "user" else "Assistant"
        lines.extend(["## %s" % heading, "", item.text, ""])
    return "\n".join(lines).rstrip() + "\n"


def kimi_v3_spec(source: KimiParseResult) -> V3PackageSpec:
    receipt = {
        "protocol_version": source.protocol_version,
        "mirrored_turn_prompt_count": source.mirrored_turn_prompt_count,
        "diagnostics": [asdict(item) for item in source.diagnostics],
        "text_item_count": len(source.text_items),
        "tool_events": [asdict(item) for item in source.tool_events],
        "source_components": {
            "state": {
                "sha256": source.state_sha256,
                "bytes": source.state_size_bytes,
            },
            "main_wire": {
                "sha256": source.wire_sha256,
                "bytes": source.wire_size_bytes,
            },
        },
        "subagent_bodies_included": False,
        "ui_completeness_attested": False,
        "session_terminal_attested": False,
    }
    return V3PackageSpec(
        provider="kimi",
        adapter_name=ADAPTER_NAME,
        adapter_version=ADAPTER_VERSION,
        coverage_status=source.completeness,
        source_stable=source.source_stable,
        source_identity_scope="kimi:selected_state_and_main_wire",
        source_identity_sha256=_source_identity(source),
        conversation_markdown=_render_markdown(source),
        provider_receipt=receipt,
    )
