"""Small internal adapter registration contract, not a plugin loader.

Provider entries own argument/selection/parser/spec/candidate wiring. The CLI
only dispatches and publishes the resulting schema-3 spec. Native parser
results remain provider-specific; there is no common conversation ontology.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Optional

from . import exporter
from ._claude_source import parse_claude_session
from ._claude_v3 import claude_v3_spec
from ._codex_v3 import codex_v3_spec
from ._kimi_source import parse_kimi_session
from ._kimi_v3 import kimi_v3_spec
from ._v3_package import V3PackageSpec
from ._zcode_source import parse_zcode_session
from ._zcode_v3 import zcode_v3_spec


DEFAULT_ZCODE_DB = "~/.zcode/cli/db/db.sqlite"


class ProviderExportBlocked(Exception):
    """Only fixed or privacy-scrubbed details may cross this CLI boundary."""

    def __init__(self, status: str, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class ProviderAdapter:
    key: str
    display_name: str
    description: str
    example: str
    configure_arguments: Callable[[argparse.ArgumentParser], None]
    select_and_parse: Callable[[argparse.Namespace], object]
    build_spec: Callable[[object], V3PackageSpec]
    parser_factory: Optional[Callable[[], argparse.ArgumentParser]] = None
    prepare_options: Optional[
        Callable[[argparse.Namespace, argparse.ArgumentParser], None]
    ] = None
    candidate_listing: Optional[Callable[[argparse.Namespace], Optional[dict]]] = None

    def build_parser(self, default_output_dir: str) -> argparse.ArgumentParser:
        parser = (self.parser_factory() if self.parser_factory
                  else argparse.ArgumentParser())
        parser.prog = "session-preserve export %s" % self.key
        parser.description = self.description
        self.configure_arguments(parser)
        if self.parser_factory is None:
            parser.add_argument("--output-dir", default=default_output_dir)
            parser.add_argument("--stdout-receipt", action="store_true")
            parser.add_argument("--quiet", action="store_true")
        else:
            parser.set_defaults(output_dir=default_output_dir)
        return parser


def _codex_arguments(parser):
    # The compatibility core already owns Codex's rich selection arguments.
    pass


def _codex_prepare(options, parser):
    options.generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if options.build_handoff_zip:
        parser.error(
            "--build-handoff-zip is a legacy package option and is not part of "
            "the schema-3 export surface"
        )


def _codex_candidates(options):
    if not options.list_candidates:
        return None
    rows = exporter.scrub_structure(
        exporter.list_candidates(options.workspace, options.since_hours),
        exporter.selection_privacy(),
    )
    return {"candidate_count": len(rows), "candidates": rows}


def _codex_source(options):
    try:
        return exporter.run_export(options)
    except exporter.ExportBlocked as blocked:
        raise ProviderExportBlocked(
            blocked.status, exporter.selection_privacy().clean(blocked.detail)
        ) from None


def _claude_arguments(parser):
    parser.add_argument("--source", required=True, help="top-level Claude session JSONL")


def _kimi_arguments(parser):
    parser.add_argument("--source", required=True,
                        help="Kimi Code session directory containing state.json and agents/main/wire.jsonl")


def _zcode_arguments(parser):
    parser.add_argument("--database", default=DEFAULT_ZCODE_DB,
                        help="ZCode conversation SQLite database (default: %(default)s)")
    parser.add_argument("--session-id", required=True, help="exact persisted ZCode session id")


ADAPTERS = (
    ProviderAdapter(
        "codex", "OpenAI Codex",
        "Preserve one already-persisted local Codex session as a schema-3 package.",
        "--session-id <uuid>", _codex_arguments, _codex_source, codex_v3_spec,
        exporter.build_parser, _codex_prepare, _codex_candidates,
    ),
    ProviderAdapter(
        "claude", "Claude Code", "Preserve one explicitly selected local claude session.",
        "--source ~/.claude/projects/.../session.jsonl", _claude_arguments,
        lambda o: parse_claude_session(Path(o.source).expanduser()), claude_v3_spec,
    ),
    ProviderAdapter(
        "kimi", "Kimi Code", "Preserve one explicitly selected local kimi session.",
        "--source ~/.kimi-code/sessions/.../<sessionId>", _kimi_arguments,
        lambda o: parse_kimi_session(Path(o.source).expanduser()), kimi_v3_spec,
    ),
    ProviderAdapter(
        "zcode", "ZCode", "Preserve one explicitly selected local zcode session.",
        "--session-id <id>", _zcode_arguments,
        lambda o: parse_zcode_session(Path(o.database).expanduser(), o.session_id),
        zcode_v3_spec,
    ),
)


def _registry(entries):
    result = {}
    for entry in entries:
        if not entry.key or entry.key in result:
            raise ValueError("provider registration keys must be unique and nonempty")
        result[entry.key] = entry
    return MappingProxyType(result)


PROVIDER_REGISTRY = _registry(ADAPTERS)
