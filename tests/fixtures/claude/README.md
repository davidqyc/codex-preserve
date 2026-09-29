# Claude vNext synthetic source fixtures

Status: pre-adapter contract fixtures. These files do not mean the released product supports Claude yet.

Every fixture in this directory is hand-authored synthetic data. No real Claude transcript, project name, path, account identifier, prompt, tool output, or session identifier was copied or anonymized into this repository.

The fixture set freezes the intended parser safety contract before implementation:

- known rendered conversation content is explicit and allow-listed;
- known-but-ignored records may be counted without copying their bodies;
- any unknown top-level record type blocks COMPLETE;
- any unknown message content-block type blocks COMPLETE;
- unsupported or mixed entrypoints are refused or cannot produce COMPLETE;
- sensitive values are never copied merely because they occur in a known record;
- malformed or truncated tails fail visibly;
- branching is explicit and must use a mechanically justified leaf selection rule;
- compaction summaries must be labelled as summaries, not ordinary user prompts;
- referenced sidecars require mechanical linkage before they can contribute to completeness.

These fixtures are intentionally provider-specific. They are not a generic conversation schema.
