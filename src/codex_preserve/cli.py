"""The ``codex-preserve`` console command.

A deliberately thin dispatcher. It owns no parser state: ``export`` hands the
remaining arguments straight to the existing exporter parser, and ``verify``
runs one small argparse over the existing integrity primitives.

Naming boundary: this command is never installed as ``codex``, and it never
offers an ``archive`` verb. ``codex archive`` is a different, built-in Codex
command that changes a saved session's lifecycle state inside Codex.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from . import __version__, exporter, verify

USAGE = """\
codex-preserve — export Codex sessions to durable files and verify them.

usage:
  codex-preserve [EXPORT OPTIONS]        export a session (default surface)
  codex-preserve export [EXPORT OPTIONS] the same export surface, named
  codex-preserve verify PACKAGE_DIR      verify one exported package
  codex-preserve --help | --version

Export reads one already-persisted local Codex rollout and writes a
human-readable conversation file, a machine receipt and a manifest into a
per-session package. The rollout is never modified.

Verify re-checks every manifest-attested member of an exported package.
  exit 0  package is intact
  exit 1  a manifest-attested member is missing or altered
  exit 2  the package could not be verified at all (fails closed)

Run `codex-preserve export --help` for the full export option list.

This is not `codex archive`. This project is not affiliated with, endorsed by,
or certified by OpenAI.
"""

_ARCHIVE_REFUSAL = """\
codex-preserve has no `%s` command, deliberately.

`codex archive` / `codex unarchive` are built-in Codex commands that change a
saved session's lifecycle state inside Codex. codex-preserve only exports a
durable copy outside Codex and verifies it.

Did you mean `codex-preserve export` or `codex-preserve verify`?
"""


def _verify_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="codex-preserve verify",
        description="Verify one exported package against its manifest.",
    )
    parser.add_argument("package_dir",
                        help="the exported package directory to verify")
    parser.add_argument("--json", action="store_true",
                        help="print the machine-readable receipt instead")
    options = parser.parse_args(argv)

    receipt = verify.verify_package(Path(options.package_dir))
    stream = sys.stdout if receipt["verdict"] == verify.VERDICT_PASS \
        else sys.stderr
    if options.json:
        print(json.dumps(receipt, indent=2, sort_keys=True,
                         ensure_ascii=False), file=stream)
    else:
        print(verify.render_human(receipt), file=stream)
    return int(receipt["exit_code"])


def main(argv: Optional[Sequence[str]] = None) -> int:
    args: List[str] = list(sys.argv[1:] if argv is None else argv)

    if not args or args[0] in ("-h", "--help", "help"):
        sys.stdout.write(USAGE)
        return 0
    if args[0] in ("-V", "--version", "version"):
        print("codex-preserve %s (conversation package schema %s, export "
              "core %s)" % (__version__, exporter.PACKAGE_SCHEMA_VERSION,
                            exporter.EXPORTER_VERSION))
        return 0
    if args[0] in ("archive", "unarchive"):
        sys.stderr.write(_ARCHIVE_REFUSAL % args[0])
        return 2
    if args[0] == "verify":
        return _verify_main(args[1:])
    if args[0] == "export":
        return exporter.main(args[1:])
    # Unprefixed invocation stays the export surface, so existing export
    # command lines keep working unchanged.
    return exporter.main(args)


if __name__ == "__main__":
    sys.exit(main())
