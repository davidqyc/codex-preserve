# Kimi Code vNext synthetic source fixtures

Status: pre-adapter source-contract fixtures. These files do not mean the released product supports Kimi Code yet.

Every fixture here is hand-authored synthetic data. No real Kimi prompt, response, workspace path, session identifier, tool input/output, credential or account value was copied or anonymized into this repository.

## First-release source boundary

The first Kimi Code adapter is intentionally narrow:

- source family: local Kimi Code sessions under `$KIMI_CODE_HOME/sessions/`;
- selected unit: one explicit session directory;
- metadata: `state.json`;
- primary conversation stream: `agents/main/wire.jsonl`;
- top-level `session_index.jsonl` is discovery metadata only and is not a canonical package member;
- subagent `agents/<id>/wire.jsonl` bodies are deferred until a linkage/completeness contract is proven;
- logs, background task output, cron persistence and debug ZIP contents are outside the first COMPLETE contract;
- no Kimi web/cloud conversation ingestion.

## Readable conversation contract

The bounded local corpus established these first-release rules:

- canonical visible user text comes from `context.append_message` records whose `message.role=user` and `message.origin.kind=user`;
- `turn.prompt` is a redundant prompt surface in the observed corpus: every observed `turn.prompt` text had an exact matching canonical user message, so it is counted but not rendered independently;
- assistant readable text comes from `context.append_loop_event` where `event.type=content.part` and `event.part.type=text`;
- `event.part.type=think` is never rendered or retained as readable text;
- tool calls/results may expose only bounded structural facts such as tool name, call linkage and success/error status; raw args and result bodies are never copied;
- injected/system-trigger/skill-activation user-role messages are context machinery, not ordinary user messages, and are not rendered as user speech;
- compaction summary/context bodies are not rendered merely because they are persisted.

Unknown top-level record types, unknown loop-event types, unknown content-part types, or malformed required structures block a future COMPLETE classification.

## Source stability and provenance

The selected session source is a pair of persisted files:

- `state.json`
- `agents/main/wire.jsonl`

A future Kimi adapter must establish a stable read of both before attaching source hashes/sizes. A stable persisted source does not imply Kimi's UI has no additional in-memory/unflushed state.

## Fixture inventory

- `linear_cli/` — ordinary user prompt + assistant text with a mirrored `turn.prompt`.
- `privacy_tool_cli/` — hidden thinking, raw tool args/results and context canaries that must never enter the safe representation.
- `compaction_cli/` — compaction/control records with summary-body canaries that stay non-rendered.
- `unknown_record_cli/` — future/unknown event record that must fail visibly.

These fixtures are Kimi-specific. They are not a generic provider schema.
