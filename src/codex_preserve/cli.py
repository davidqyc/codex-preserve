"""Public session-preserve command.

The public CLI is provider-aware while the proven legacy Codex exporter remains
an internal compatibility core. New exports use package schema 3.0.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence

from . import __version__, exporter, verify
from ._claude_source import parse_claude_session
from ._claude_v3 import claude_v3_spec
from ._codex_v3 import codex_v3_spec
from ._kimi_source import parse_kimi_session
from ._kimi_v3 import kimi_v3_spec
from ._v3_package import (
    CONVERSATION_FILENAME,
    MANIFEST_FILENAME,
    PACKAGE_SCHEMA_VERSION,
    RECEIPT_FILENAME,
    V3PackageSpec,
    generate_v3_transfer_zip,
    write_v3_package,
)
from ._zcode_source import parse_zcode_session
from ._zcode_v3 import zcode_v3_spec
from ._shared_core import json_bytes, sha256_bytes


DEFAULT_OUTPUT_DIR = "~/Desktop/SessionPreserve"
DEFAULT_ZCODE_DB = "~/.zcode/cli/db/db.sqlite"
PROVIDERS = ("codex", "claude", "kimi", "zcode")

USAGE = """\
session-preserve — preserve local coding-agent sessions as durable,
human-readable packages with provenance and integrity verification.

usage:
  session-preserve export PROVIDER [OPTIONS]
  session-preserve verify PACKAGE_DIR [--json]
  session-preserve pack PACKAGE_DIR
  session-preserve --help | --version

providers:
  codex    OpenAI Codex local persisted session
  claude   Claude Code local top-level session JSONL
  kimi     Kimi Code local session directory
  zcode    ZCode local SQLite session

Examples:
  session-preserve export codex --session-id <uuid>
  session-preserve export claude --source ~/.claude/projects/.../session.jsonl
  session-preserve export kimi --source ~/.kimi-code/sessions/.../<sessionId>
  session-preserve export zcode --session-id <id>
  session-preserve verify ./exports/codex__123456789abc

New exports use package schema 3.0. The verifier also permanently supports
legacy Codex package schemas 2.1 and 2.2.

This project is independent and unofficial. It is not affiliated with,
endorsed by, sponsored by, or certified by OpenAI, Anthropic, Moonshot AI,
or Z.ai.
"""


def _verify_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="session-preserve verify",
        description="Verify one exported package against its manifest.",
    )
    parser.add_argument("package_dir",
                        help="the exported package directory to verify")
    parser.add_argument("--json", action="store_true",
                        help="print the machine-readable receipt instead")
    options = parser.parse_args(argv)

    receipt = dict(verify.verify_package(Path(options.package_dir)))
    # The legacy verifier core keeps its historical identity for byte/regression
    # compatibility. The public v0.2 CLI reports the product that performed the
    # verification, including when the package itself is schema 2.1/2.2.
    receipt["tool"] = "session-preserve"
    stream = sys.stdout if receipt["verdict"] == verify.VERDICT_PASS \
        else sys.stderr
    if options.json:
        print(json.dumps(receipt, indent=2, sort_keys=True,
                         ensure_ascii=False), file=stream)
    else:
        print(verify.render_human(receipt), file=stream)
    return int(receipt["exit_code"])


def _pack_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="session-preserve pack",
        description=(
            "Verify one schema-3 package and build its derived session-package.zip."
        ),
    )
    parser.add_argument("package_dir")
    options = parser.parse_args(argv)
    try:
        result = generate_v3_transfer_zip(Path(options.package_dir))
    except (OSError, ValueError) as error:
        print(json.dumps({
            "status": "PACK_BLOCKED",
            "detail": str(error),
        }, indent=2, sort_keys=True, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({
        "status": "PACKED",
        "path": str(result["path"]),
        "bytes": result["bytes"],
        "sha256": result["sha256"],
        "canonical_package_dependency": False,
    }, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


def _fallback_package_coordinate(spec: V3PackageSpec) -> str:
    identity = spec.source_identity_sha256
    if identity is None:
        payload = (
            spec.provider.encode("utf-8")
            + b"\0"
            + spec.conversation_markdown.encode("utf-8")
            + b"\0"
            + json_bytes(spec.provider_receipt)
        )
        identity = sha256_bytes(payload)
    return "%s__%s" % (spec.provider, identity[:12])


def _write_spec(spec: V3PackageSpec, output_dir: Path,
                quiet: bool = False, stdout_receipt: bool = False) -> int:
    output_dir = Path(output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    package = output_dir / _fallback_package_coordinate(spec)
    try:
        write_v3_package(package, spec)
    except FileExistsError:
        print(json.dumps({
            "export_status": "BLOCKED_TARGET_EXISTS",
            "detail": "target package already exists; choose another output directory",
            "package": str(package),
        }, indent=2, sort_keys=True, ensure_ascii=False), file=sys.stderr)
        return 2

    receipt_path = package / RECEIPT_FILENAME
    if stdout_receipt:
        sys.stdout.write(receipt_path.read_text(encoding="utf-8"))
    elif not quiet:
        print(json.dumps({
            "export_status": spec.coverage_status,
            "provider": spec.provider,
            "package_schema_version": PACKAGE_SCHEMA_VERSION,
            "package": str(package),
            "conversation": str(package / CONVERSATION_FILENAME),
            "receipt": str(receipt_path),
            "manifest": str(package / MANIFEST_FILENAME),
            "source_stable": spec.source_stable,
        }, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


def _codex_export_main(argv: Sequence[str]) -> int:
    parser = exporter.build_parser()
    parser.prog = "session-preserve export codex"
    parser.description = (
        "Preserve one already-persisted local Codex session as a schema-3 package."
    )
    parser.set_defaults(output_dir=DEFAULT_OUTPUT_DIR)
    options = parser.parse_args(argv)
    options.generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if options.build_handoff_zip:
        parser.error(
            "--build-handoff-zip is a legacy package option and is not part of "
            "the schema-3 export surface"
        )

    if options.list_candidates:
        rows = exporter.scrub_structure(
            exporter.list_candidates(options.workspace, options.since_hours),
            exporter.selection_privacy(),
        )
        print(json.dumps({"candidate_count": len(rows), "candidates": rows},
                         indent=2, sort_keys=True, ensure_ascii=False))
        return 0

    try:
        context = exporter.run_export(options)
        spec = codex_v3_spec(context)
    except exporter.ExportBlocked as blocked:
        print(json.dumps({
            "export_status": blocked.status,
            "detail": exporter.selection_privacy().clean(blocked.detail),
        }, indent=2, sort_keys=True, ensure_ascii=False), file=sys.stderr)
        return 2
    return _write_spec(
        spec,
        Path(options.output_dir),
        quiet=bool(options.quiet),
        stdout_receipt=bool(options.stdout_receipt),
    )


def _simple_export_parser(provider: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="session-preserve export %s" % provider,
        description="Preserve one explicitly selected local %s session."
                    % provider,
    )
    if provider in ("claude", "kimi"):
        parser.add_argument(
            "--source",
            required=True,
            help=(
                "top-level Claude session JSONL"
                if provider == "claude"
                else "Kimi Code session directory containing state.json and agents/main/wire.jsonl"
            ),
        )
    elif provider == "zcode":
        parser.add_argument(
            "--database",
            default=DEFAULT_ZCODE_DB,
            help="ZCode conversation SQLite database (default: %(default)s)",
        )
        parser.add_argument(
            "--session-id",
            required=True,
            help="exact persisted ZCode session id",
        )
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--stdout-receipt", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    return parser


def _noncodex_export_main(provider: str, argv: Sequence[str]) -> int:
    parser = _simple_export_parser(provider)
    options = parser.parse_args(argv)

    if provider == "claude":
        source = parse_claude_session(Path(options.source).expanduser())
        spec = claude_v3_spec(source)
    elif provider == "kimi":
        source = parse_kimi_session(Path(options.source).expanduser())
        spec = kimi_v3_spec(source)
    elif provider == "zcode":
        source = parse_zcode_session(
            Path(options.database).expanduser(), options.session_id
        )
        spec = zcode_v3_spec(source)
    else:
        parser.error("unsupported provider")

    if not spec.source_stable:
        print(json.dumps({
            "export_status": "BLOCKED_SOURCE_UNSTABLE",
            "provider": provider,
            "coverage_status": spec.coverage_status,
            "detail": (
                "the selected persisted source could not be read as one stable "
                "snapshot; no package was published"
            ),
        }, indent=2, sort_keys=True, ensure_ascii=False), file=sys.stderr)
        return 2

    return _write_spec(
        spec,
        Path(options.output_dir),
        quiet=bool(options.quiet),
        stdout_receipt=bool(options.stdout_receipt),
    )


def _export_main(argv: Sequence[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(
            "usage: session-preserve export PROVIDER [OPTIONS]\n\n"
            "providers: codex, claude, kimi, zcode\n"
            "run 'session-preserve export PROVIDER --help' for provider options"
        )
        return 0
    provider = argv[0].lower()
    if provider not in PROVIDERS:
        print(
            "unknown provider %r; choose one of: %s"
            % (argv[0], ", ".join(PROVIDERS)),
            file=sys.stderr,
        )
        return 2
    if provider == "codex":
        return _codex_export_main(argv[1:])
    return _noncodex_export_main(provider, argv[1:])


def main(argv: Optional[Sequence[str]] = None) -> int:
    args: List[str] = list(sys.argv[1:] if argv is None else argv)

    if not args or args[0] in ("-h", "--help", "help"):
        sys.stdout.write(USAGE)
        return 0
    if args[0] in ("-V", "--version", "version"):
        print(
            "session-preserve %s (package schema %s; legacy verify 2.1, 2.2)"
            % (__version__, PACKAGE_SCHEMA_VERSION)
        )
        return 0
    if args[0] in ("archive", "unarchive"):
        print(
            "session-preserve has no `%s` command. "
            "For Codex, `codex %s` changes Codex's own saved-session state; "
            "Session Preserve only writes independent preservation packages."
            % (args[0], args[0]),
            file=sys.stderr,
        )
        return 2
    if args[0] == "verify":
        return _verify_main(args[1:])
    if args[0] == "pack":
        return _pack_main(args[1:])
    if args[0] == "export":
        return _export_main(args[1:])

    print(
        "unknown command %r; expected 'export', 'verify', or 'pack'" % args[0],
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
