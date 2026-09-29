# ZCode vNext synthetic source fixtures

Status: pre-adapter source-contract fixtures. These files do not mean the released product supports ZCode yet.

Every fixture here is hand-authored synthetic data. No real ZCode prompt, response, workspace path, session identifier, reasoning, tool input/output, credential or account value was copied or anonymized into this repository.

## First-release source boundary

The first ZCode adapter is intentionally narrow:

- source family: the local SQLite conversation store at `$ZCODE_HOME/cli/db/db.sqlite` (default `~/.zcode/cli/db/db.sqlite`);
- selected unit: one explicit `session.id`;
- authoritative conversation rows: `session`, `message`, and `part` within one read transaction/snapshot;
- sparse `cli/rollout/model-io-*.jsonl` traces are not the canonical session inventory;
- terminal-output caches under `cli/exec/`, daily logs, usage telemetry and workflow journals are outside the first readable-conversation contract.

The observed store uses a split model:

- `message` carries role/container/visibility metadata;
- `part` carries content blocks;
- `part.type=text` is the only first-release readable text surface;
- `part.type=reasoning` is never rendered;
- `part.type=tool` may expose only bounded structural facts such as tool name/call linkage/status; raw input/output stays private;
- hidden synthetic/model-only messages are not ordinary user conversation and are not rendered;
- file parts are metadata-only until an explicit attachment/materialization contract is proven.

Unknown message semantics, unknown part types, malformed JSON, broken message/part linkage, duplicate ordering coordinates or unsupported visibility states block a future COMPLETE classification.

## SQLite snapshot/provenance

ZCode is a live WAL-backed SQLite store. A future adapter must use one read transaction/snapshot for the selected session.

The provider source identity should be a deterministic digest over the selected session's canonical ordered rows, not a hash of the entire database file: unrelated sessions can mutate the same database.

This digest proves selected persisted-source content identity only. It is not authenticity.

## Fixture inventory

The SQL fixtures create only the minimal `session`, `message`, and `part` columns needed for source-contract tests.

- `linear_visible.sql` — one visible user message and one visible assistant message.
- `privacy_hidden_tool.sql` — hidden synthetic text, reasoning and raw tool input/output canaries that must never enter the safe representation.
- `unknown_part.sql` — a future part type that must fail visibly.

These fixtures are ZCode-specific. They are not a generic provider schema.
