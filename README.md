# codex-preserve

`codex-preserve` exports an OpenAI Codex session out of Codex into durable,
human-readable files, and verifies that every manifest-attested member is
present and byte-identical to the SHA-256 and size recorded in the manifest.
It reads a Codex session; it never modifies one.

> **This is not `codex archive`.** The built-in `codex archive` command changes
> a saved session's lifecycle state *inside* Codex. `codex-preserve` exports a
> durable copy *outside* Codex and verifies the exported package. It does not
> modify session state, and it is not a replacement for `codex archive`.

**Not affiliated with OpenAI.** `codex-preserve` is an independent, unofficial
tool. It is not affiliated with, endorsed by, sponsored by, or certified by
OpenAI, and it is not an OpenAI product or a first-party Codex component. It
ships no OpenAI logo or other visual branding. It is named for the Codex
sessions it reads.

## What it does

- **Exports** one already-persisted local Codex rollout into a per-session
  package: a readable conversation file, a machine receipt, input attachments
  and output files, and a manifest.
- **Verifies** an exported package against that manifest: every attested
  member must be present with the attested size and SHA-256. See
  [What verification does and does not prove](#what-verification-does-and-does-not-prove).
- **Records provenance** for exported artifacts and attachments, including
  which turn produced them and whether the payload is complete.
- **Normalizes and redacts** by default: home paths are normalized for
  readability, and credential-shaped values are removed before anything is
  written.
- **Fails closed.** What the tool cannot prove mechanically is reported as
  unknown rather than guessed, and a package that cannot be verified is never
  reported as intact.

## What it does not do

- It does not modify, archive, unarchive, delete or move Codex sessions.
- It makes no model call and no network call. There is no daemon, no hook, no
  telemetry and no event database. It does run local read-only `git` queries by
  default — see [What it reads](#what-it-reads).
- It does not decode opaque or encrypted reasoning. Such items are counted,
  never reconstructed.
- It does not sign anything. The manifest attests SHA-256 completeness and
  integrity; that is not a cryptographic signature and says nothing about who
  produced a package.

## What verification does and does not prove

`codex-preserve verify` answers exactly one question: **is every member the
manifest attests present, and do its bytes still hash to the SHA-256 and size
the manifest records?**

That is worth having. It detects accidental loss, truncation, corruption in
transit or storage, and edits made to a payload without also rewriting the
manifest.

It is not tamper-proofing, and the following are outside the proof:

- **The manifest is co-distributed and is not independently signed.** It
  travels inside the package it describes. There is no signature, no
  certificate, no trust root and no transparency log, so the check is
  *manifest-relative* integrity, not authenticity. It says nothing about who
  produced the package.
- **A coordinated rewrite is not detected.** Anyone who edits a payload *and*
  recomputes its manifest row produces a package that verifies. Detecting that
  requires a trust anchor this tool does not have.
- **Extra files are not part of the current check.** Verification walks the
  members the manifest lists. A file added to the package directory that the
  manifest never mentions does not make verification fail, so "complete" means
  "nothing attested is missing", not "nothing else is present".

If you need authenticity rather than integrity, sign the exported package with
a tool built for that. `codex-preserve` deliberately does not implement one.

## What it reads

Export is local and read-only, but it is not limited to the one rollout file
you name. On a normal run it may also:

- **read `~/.codex/session_index.jsonl`**, read-only, to recover the session's
  display name. The lookup is filtered by session id and returns silently if
  the file is absent or unreadable. Nothing under `~/.codex` is ever written.
- **run local read-only `git` queries in the session's recorded workspace**
  (`rev-parse`, `branch --show-current`, `remote get-url`, `merge-base`) to
  record where the work happened. These are local repository queries; they
  contact no network and no remote.
- **include a normalized repository identity such as `owner/repo`** in the
  exported provenance, in the receipt and in the human-readable file.
  The raw remote URL is discarded and never exported — but if you share an
  export, the `owner/repo` name goes with it.

Pass `--no-git-probe` to skip the live git queries entirely. That is the
entire scope of the flag: it stops the export from *asking* the local
repository anything. It does not erase repository identity that is already
persisted in the rollout's own session metadata. A persisted identity is
still normalized to `owner/repo` and may appear in the receipt, in the
human-readable export, in the stable output basename, and in the output
directory / bucket names. The raw remote URL is discarded and never
exported, with or without the flag.

## Install

```bash
pip install .
```

Python 3.9 or newer. No runtime dependencies.

## Try it in one command

You do not need a Codex session to see what this tool does. Three synthetic
example packages ship with the repository and exercise the entire
verification contract:

```bash
./examples/run_examples.sh
```

That prints one `PASS`, one `FAIL` and one `UNVERIFIABLE` verdict and checks
that each exits `0`, `1` and `2` respectively. See
[examples/README.md](examples/README.md) for what each one demonstrates and
why. The same three packages are asserted by the test suite, so they stay
honest.

## Use

```bash
# See the surfaces.
codex-preserve --help

# List the sessions that could be exported (sanitized output).
codex-preserve --list-candidates

# Export one session by id.
codex-preserve --session-id <uuid> --output-dir ./exports

# The same export surface, named explicitly.
codex-preserve export --session-id <uuid> --output-dir ./exports

# Verify an exported package.
codex-preserve verify ./exports/<bucket>/<package-dir>

# Machine-readable verification receipt.
codex-preserve verify ./exports/<bucket>/<package-dir> --json
```

`codex-preserve export --help` lists the full export option set, including
selection (`--rollout`, `--session-id`, `--workspace`, `--since-hours`),
provenance (`--artifact ROLE=PATH`, `--review-bundle`, `--no-git-probe`) and
policy (`--no-normalize`, `--reasoning-cap`, `--max-package-bytes`).

Sessions are discovered under `~/.codex/sessions` and
`~/.codex/archived_sessions` only. The home directory is never scanned
broadly.

### Verify exit codes

| exit | meaning |
| ---- | ------- |
| `0`  | every manifest-attested member is present and matches |
| `1`  | a manifest-attested member is missing or altered |
| `2`  | the package could not be verified at all — fails closed |

The reason is always printed. `--json` emits the same verdict as a receipt
with per-member reason codes.

Manifest-attested package members must be real files under the package tree;
symlinked member paths are rejected.

Exit 2 also covers a manifest this build cannot interpret: an unrecognized or
missing `package_schema_version`, or a manifest collection or row whose shape
is not the expected JSON array/object. A verifier that cannot read the
manifest reports UNVERIFIABLE rather than claiming the members it never looked
at are fine.

## Development

```bash
PYTHONPATH=src python3 -m unittest discover -t . -s tests
python3 tools/public_hygiene_scan.py .
./examples/run_examples.sh
```

The test suite is deterministic and entirely synthetic: it builds its own
rollout fixtures in a temporary directory and never reads a real Codex session
directory.

`tools/public_hygiene_scan.py` is a deterministic check that no private
coordinate or credential-shaped literal has entered the tree.

## Localization

Package member filenames and the default output directory are currently
Simplified Chinese, because they are part of the already-proven package format
this tool exports. Changing them is a package-format decision rather than a
cosmetic one, and it is still open. Everything else — the CLI, the receipts,
the manifest keys and this documentation — is English.

## License

Apache License 2.0. See [LICENSE](LICENSE) for the full text.

```text
SPDX-License-Identifier: Apache-2.0
```

## Status

Version 0.1.0, first public release line. The export/verify contract
described above — the three verdicts, the exit codes, and the limits of what
the manifest proves — is what this version commits to. Package member
filenames are still an open question; see [Localization](#localization).

**Not affiliated with OpenAI.** `codex-preserve` is an independent, unofficial
tool. It is not affiliated with, endorsed by, sponsored by, or certified by
OpenAI, and it is not an OpenAI product or a first-party Codex component. It
ships no OpenAI logo or other visual branding.
