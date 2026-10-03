"""Public session-preserve command.

The public CLI is provider-aware while the proven legacy Codex exporter remains
an internal compatibility core. New exports use package schema 3.0.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from . import __version__, verify
from ._provider_registry import PROVIDER_REGISTRY, ProviderExportBlocked
from ._v3_package import (
    CONVERSATION_FILENAME,
    MANIFEST_FILENAME,
    PACKAGE_SCHEMA_VERSION,
    RECEIPT_FILENAME,
    V3PackageSpec,
    generate_v3_transfer_zip,
    write_v3_package,
)
from ._shared_core import json_bytes, sha256_bytes


DEFAULT_OUTPUT_DIR = "~/Desktop/SessionPreserve"
PROVIDERS = tuple(PROVIDER_REGISTRY)

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


def _export_main(argv: Sequence[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(
            "usage: session-preserve export PROVIDER [OPTIONS]\n\n"
            "providers: %s\n"
            "run 'session-preserve export PROVIDER --help' for provider options"
            % ", ".join(PROVIDER_REGISTRY)
        )
        return 0
    provider = argv[0].lower()
    if provider not in PROVIDER_REGISTRY:
        print(
            "unknown provider %r; choose one of: %s"
            % (argv[0], ", ".join(PROVIDERS)),
            file=sys.stderr,
        )
        return 2
    adapter = PROVIDER_REGISTRY[provider]
    parser = adapter.build_parser(DEFAULT_OUTPUT_DIR)
    options = parser.parse_args(argv[1:])
    if adapter.prepare_options is not None:
        adapter.prepare_options(options, parser)
    if adapter.candidate_listing is not None:
        candidates = adapter.candidate_listing(options)
        if candidates is not None:
            print(json.dumps(candidates, indent=2, sort_keys=True, ensure_ascii=False))
            return 0
    try:
        source = adapter.select_and_parse(options)
        spec = adapter.build_spec(source)
    except ProviderExportBlocked as blocked:
        print(json.dumps({
            "export_status": blocked.status,
            "detail": blocked.detail,
        }, indent=2, sort_keys=True, ensure_ascii=False), file=sys.stderr)
        return 2
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
    return _write_spec(spec, Path(options.output_dir),
                       quiet=bool(options.quiet),
                       stdout_receipt=bool(options.stdout_receipt))


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
