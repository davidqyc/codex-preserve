# Claude vNext synthetic source fixtures

Status: internal parser contract fixtures. These files do not mean the released product supports Claude yet.

Every fixture in this directory is hand-authored synthetic data. No real Claude transcript, project name, path, account identifier, prompt, tool output, or session identifier was copied or anonymized into this repository.

## First-release source boundary

The first Claude adapter is intentionally narrow:

- source family: local Claude Code transcripts under `$CLAUDE_CONFIG_DIR/projects` or `~/.claude/projects`;
- entrypoint claimed at first release: `cli` only;
- unit of export: one explicitly selected top-level session JSONL;
- `claude-desktop`, `sdk-cli`, IDE and Cowork are not claimed until separately tested;
- mixed or unsupported entrypoints are refused and can never produce `COMPLETE`;
- no `claude.ai` cloud-chat ingestion;
- no restore, reindex, resume, import, sync, hook, daemon or bulk export.

The initial coverage target is **safe readable content persisted in the selected top-level JSONL**. A stable file and `COMPLETE` parser classification do not attest that the current UI's visible replies have been flushed, or that a session has ended.

## Structural contract frozen before implementation

Private reconnaissance emitted aggregate structure only. It established enough to freeze these parser rules without publishing transcript values:

- UUID-bearing conversation records form a parent-linked `uuid` / `parentUuid` graph; branching exists;
- append order alone is not a conversation model;
- `last-prompt.leafUuid` is useful metadata but is not a universal active-leaf authority;
- the G4a parser preserves persisted graph facts and fails visibly on ambiguity, cycles or missing parents; main-chain selection belongs to a later stage;
- compaction uses a `compact_boundary` plus an `isCompactSummary` record; summaries must be labelled as summaries rather than ordinary user prompts;
- no reliable session-level terminal marker has been established; message `stop_reason` values must not be inflated into “the session is finished”;
- source coverage and session terminal state are separate concepts;
- referenced tool-result and subagent sidecars exist, but the first adapter does **not** read their bodies. Sidecar-body support is a later, separately gated expansion;
- malformed or truncated live tails fail visibly;
- the selected source must be stable across the read; a changed source may not yield `COMPLETE`.

## Three-way record policy

Claude's local transcript format is undocumented internal state. The adapter therefore uses three buckets.

### `RENDER`

Only content explicitly allowed into the human-readable conversation:

- ordinary user text;
- ordinary assistant text;
- compact summaries, clearly labelled as summaries.

### `KNOWN_IGNORED_OR_SUMMARIZED`

Known structures that may be counted or represented by bounded structural metadata without copying their raw bodies:

- `thinking` blocks and signatures;
- `tool_use` / `tool_result` raw payloads;
- attachment/control records;
- queue records;
- file-history records;
- titles and bookkeeping records;
- sidecar bodies.

Tool activity may expose a bounded, redacted structural summary, such as the tool name and whether a result was present.

Raw tool inputs, raw tool results, `stdout`, `stderr`, prompts, environment/context bodies and thinking signatures are never copied merely because their enclosing record type is known.

### `UNKNOWN`

Any unknown top-level record type or unknown message content-block type blocks `COMPLETE`.

An unknown attachment type also blocks `COMPLETE` unless later evidence proves that the attachment class cannot solely carry user-visible conversation for the exact claimed versions.

Missing required fields on a rendered/message-bearing structure block `COMPLETE`. Extra fields on rendered/message-bearing structures are surfaced as drift and cannot silently change semantics.

Unknown raw values are never copied. Safe drift evidence may include only bounded shape facts such as type/key names, counts, source coordinates, byte sizes and source-file hashes.

## Privacy contract

Never-export surfaces include account/organization identifiers, thinking signatures, raw system prompts, raw prompt snapshots, raw environment/session-context/instruction bodies, and arbitrary raw unknown values.

The eventual adapter's privacy canary gate applies to package members, receipt, manifest, member names, directory names, CLI stdout/stderr and machine-readable CLI output.

Project-directory names and local source paths are privacy-sensitive coordinates and must be normalized rather than published verbatim.

## Fixture inventory

- `linear_cli.jsonl` — ordinary linear CLI conversation.
- `branching_cli.jsonl` — explicit parent-graph branching.
- `stale_last_prompt_cli.jsonl` — a historical `last-prompt.leafUuid` has persisted descendants and is retained only as a pointer fact.
- `parallel_stale_leaf_cli.jsonl` — a non-explicit pointer targets a parallel tool-result branch while later complete readable answers remain persisted.
- `rewound_last_prompt_cli.jsonl` — an explicit/rewound pointer intentionally targets an older persisted node; active-head interpretation remains deferred.
- `compaction_cli.jsonl` — compact boundary plus labelled compact summary.
- `duplicate_replay_cli.jsonl` — repeated persisted occurrences reuse the same graph UUIDs without becoming a semantic branch.
- `known_ignored_cli.jsonl` — known bookkeeping/control material that must not be copied raw.
- `fallback_cli.jsonl` — a known non-text content block that is intentionally not rendered.
- `unknown_record_cli.jsonl` — future top-level record blocks `COMPLETE`.
- `unknown_block_cli.jsonl` — future message block blocks `COMPLETE`.
- `unknown_attachment_cli.jsonl` — future attachment type blocks `COMPLETE`.
- `sensitive_canaries_cli.jsonl` — sensitive-value canaries for later zero-leak tests.
- `mixed_entrypoint.jsonl` — mixed entrypoints are unsupported.
- `unsupported_sdk_cli.jsonl` — an unclaimed entrypoint is refused.
- `truncated_tail_cli.jsonl` — malformed/truncated tail fails visibly.
- `phantom_parent_after_resume_cli.jsonl` — a resumed user has a missing parent while its attachment and assistant descendants persist.
- `persisted_ui_lag_possible_cli.jsonl` — valid, stable persisted user and metadata rows with no assistant row; parser eligibility cannot attest to what the UI displayed.

These fixtures are provider-specific. They are not a generic conversation schema.
