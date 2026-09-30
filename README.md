# Session Preserve

**English** | [简体中文](README.zh-CN.md)

Session Preserve keeps an independent, readable copy of coding-agent sessions that are already persisted on your machine.

v0.2.0 supports four local sources: OpenAI Codex, Claude Code, Kimi Code, and ZCode.

The exported package records where its readable content came from, what the adapter could and could not establish, and a manifest of the package files. Later, session-preserve verify can check whether those manifest-attested files are still present and byte-identical to their recorded SHA-256 and size.

Source sessions are read-only. Session Preserve does not restore, resume, import, sync, reindex, repair, archive, or delete them.

> If you only need a readable transcript, use the product's built-in export surface when one is available. Session Preserve is for keeping an **independent preservation package** with provenance, coverage information, and later manifest-relative integrity verification.

## Install

~~~bash
python -m pip install session-preserve
~~~

Python 3.9 or newer. The runtime has no third-party Python dependencies.

~~~bash
session-preserve --help
session-preserve --version
~~~

Project pages: [GitHub](https://github.com/davidqyc/session-preserve) · [Releases](https://github.com/davidqyc/session-preserve/releases) · [Issues](https://github.com/davidqyc/session-preserve/issues)

## Quick start

Every new export uses package schema 3.0.

### Codex

List sanitized candidates:

~~~bash
session-preserve export codex --list-candidates
~~~

Export one exact persisted session:

~~~bash
session-preserve export codex --session-id <uuid> --output-dir ./exports
~~~

You can also select an exact rollout file with --rollout.

### Claude Code

Select one top-level session JSONL explicitly:

~~~bash
session-preserve export claude --source ~/.claude/projects/.../<session>.jsonl --output-dir ./exports
~~~

The default Claude Code store is under ~/.claude/projects, or $CLAUDE_CONFIG_DIR/projects when configured.

If you keep long-lived Claude Code sessions, see [Claude Code session retention and independent preservation](docs/claude-code-session-backup.md).

### Kimi Code

Select one current-format Kimi Code session directory:

~~~bash
session-preserve export kimi --source ~/.kimi-code/sessions/.../<session-id> --output-dir ./exports
~~~

The first adapter reads that session's state.json and agents/main/wire.jsonl. If KIMI_CODE_HOME is configured, use its sessions directory instead.

### ZCode

Select one persisted ZCode session from the local conversation database:

~~~bash
session-preserve export zcode --session-id <session-id> --output-dir ./exports
~~~

The default database is ~/.zcode/cli/db/db.sqlite. Use --database PATH to point at another ZCode data root.

### Verify and make a transfer ZIP

~~~bash
session-preserve verify ./exports/<package-dir>
session-preserve verify ./exports/<package-dir> --json
session-preserve pack ./exports/<package-dir>
~~~

pack first verifies a schema-3 package and then writes the derived session-package.zip beside its canonical members. The ZIP excludes itself and does not become a canonical package dependency.

Verifier exit codes are stable:

| exit | meaning |
| ---: | --- |
| 0 | every manifest-attested member is present and matches |
| 1 | at least one attested member is missing or altered |
| 2 | the package cannot be verified safely; the verifier fails closed |

## What a schema-3 package contains

The physical names are provider-neutral:

~~~text
<package>/
├── conversation.md
├── export.receipt.json
├── package.manifest.json
├── attachments/          # optional
└── artifacts/            # optional
    └── index.md           # present when artifacts exist
~~~

An on-demand transfer ZIP, when supported by the release surface, is named session-package.zip and is a derived transfer artifact, not a canonical package dependency.

conversation.md is the human-readable preservation view. export.receipt.json records provider-specific coverage, provenance, privacy, lifecycle/graph facts, and diagnostics. package.manifest.json records canonical package members, byte sizes, and SHA-256 values.

The shared package layer does not force the four providers into one artificial conversation model. Each provider adapter keeps its own source semantics.

## Provider boundaries

### Codex

Session Preserve reuses the mature Codex parser that shipped in codex-preserve 0.1.x, then projects the result into schema 3.

It may read the selected rollout, ~/.codex/session_index.jsonl read-only for a display name, local read-only Git metadata unless --no-git-probe is used, and explicitly selected attachments/artifacts within the existing safety limits.

It never calls codex archive and never changes Codex session state.

### Claude Code

The first Claude adapter reads one explicitly selected top-level local session JSONL.

It is deliberately loss-averse. Safe persisted text on parallel branches is retained. A last-prompt or rewind marker does not make other persisted text disappear. Missing parent links and duplicate persisted records are reported rather than guessed away. Raw thinking signatures, tool payloads, environment/context bodies, account identifiers, and unknown raw values are not exported.

A critical boundary: **a stable Claude JSONL does not prove that every message already visible in the Claude UI has been flushed to disk.** The receipt therefore never attests UI completeness or session terminality.

Subagent and tool-result sidecar bodies are outside the first v0.2 adapter.

### Kimi Code

The first Kimi adapter targets the current Kimi Code session layout:

~~~text
<session>/
├── state.json
└── agents/
    └── main/
        └── wire.jsonl
~~~

It keeps recognized user/assistant text and bounded tool structure while excluding raw thinking, model-request/debug material, tool arguments/results, and unknown record bodies.

Subagent bodies are not included in the first adapter. The older Python-era ~/.kimi storage family is not claimed by v0.2.0.

### ZCode

The first ZCode adapter reads one explicit session from the local SQLite conversation store in read-only mode.

It uses the selected session's structured session, message, and part rows. Visible text is preserved; hidden/model-only messages, reasoning bodies, and raw tool bodies are not.

~/.zcode/cli/rollout/model-io-*.jsonl is diagnostic model-I/O data and is **not** treated as the canonical conversation source.

## Coverage is not the same as the whole UI conversation

Session Preserve only makes claims it can support from persisted local data.

A package can state whether the selected source snapshot was stable while it was read, whether recognized persisted records fit the adapter contract, how much safe readable content was preserved, and whether unknown schema, graph gaps, unsupported entrypoints, or other limitations were observed.

It does not infer that every message currently visible in an app UI has already been persisted, that the session has ended, that hidden model state has been reconstructed, or that excluded sidecars are somehow covered.

When the adapter sees unsupported or unknown persisted structure, it marks coverage NON_COMPLETE rather than silently inventing certainty.

## What verification proves

session-preserve verify checks manifest-relative integrity:

> Are the files attested by this package's manifest present, with the same byte length and SHA-256 recorded in that manifest?

That detects missing, truncated, corrupted, or independently edited package members.

It does **not** prove authenticity. The manifest travels with the package and is not independently signed. A coordinated rewrite of both a payload and its manifest can still verify. There is no certificate, trust root, authorship attestation, or transparency log.

If you need authenticity, sign or timestamp the package with a separate trust mechanism.

## Legacy compatibility

codex-preserve 0.1.3 remains published and is not yanked.

Session Preserve's verifier permanently retains support for legacy Codex package schemas 2.1 and 2.2. New Session Preserve exports use schema 3.0.

The old distribution is not currently a compatibility shim for the new one.

The repository was renamed from davidqyc/codex-preserve to davidqyc/session-preserve; GitHub's repository redirect preserves old links.

## 30-second verifier demo

The repository still carries three synthetic legacy packages specifically to prove backward-compatible verification:

~~~bash
git clone --depth 1 https://github.com/davidqyc/session-preserve.git
cd session-preserve
./examples/run_examples.sh
~~~

The script expects PASS / exit 0, FAIL / exit 1, and UNVERIFIABLE / exit 2. Schema-3 provider exports are covered by the synthetic test suite as well.

## Privacy and local behavior

Session Preserve is local-first and makes no model call or network call during export or verification.

Provider parsers are allowlist-based. Unknown raw values are not copied just because they exist in a local session store.

The project includes a deterministic public-hygiene scan to prevent real session payloads, private machine coordinates, and credential-shaped literals from entering the public repository.

## Non-goals

Session Preserve is not a cloud-chat importer, transcript viewer, restore/resume/import/sync tool, history repair/reindex tool, background daemon, provider-conversion layer, generic provider/plugin SDK, or authenticity/forensic chain-of-custody system.

The v0.2.0 provider scope is intentionally limited to Codex, Claude Code, Kimi Code, and ZCode.

## Development

~~~bash
PYTHONPATH=src python3 -m unittest discover -t . -s tests
python3 tools/public_hygiene_scan.py .
python3 tools/g3_golden_regression.py
./examples/run_examples.sh
~~~

All committed provider fixtures are hand-authored synthetic data. Tests do not copy real user transcripts into the repository.

## License

Apache License 2.0. See [LICENSE](LICENSE).

~~~text
SPDX-License-Identifier: Apache-2.0
~~~

## Independence

Session Preserve is an independent, unofficial open-source project. It is not affiliated with, endorsed by, sponsored by, or certified by OpenAI, Anthropic, Moonshot AI, or Z.ai. Product and company names are used only to identify the local session formats the adapters read.
