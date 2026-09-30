# Examples

These three small packages prove the verifier's public three-state contract:
PASS / FAIL / UNVERIFIABLE with exit codes 0 / 1 / 2.

They are deliberately **legacy Codex schema-2 fixtures**. Keeping them unchanged
proves that Session Preserve v0.2 continues to verify packages created by the
published codex-preserve 0.1.x line.

Run all three:

~~~bash
./examples/run_examples.sh
~~~

The same fixtures are asserted by the test suite.

| example | command | verdict | exit |
| --- | --- | --- | ---: |
| pass | session-preserve verify examples/pass | PASS | 0 |
| fail | session-preserve verify examples/fail | FAIL | 1 |
| unverifiable | session-preserve verify examples/unverifiable | UNVERIFIABLE | 2 |

Add --json to print the machine-readable verification receipt.

## pass

The package is an unchanged synthetic codex-preserve export. Every
manifest-attested member is present and matches its recorded byte size and
SHA-256.

There is no real Codex conversation, real account, or real machine coordinate
in these files.

## fail

One byte of the attested legacy conversation file 对话记录.md was changed while
the manifest was left untouched.

Verification therefore reports a SHA-256 mismatch and exits 1.

This proves integrity relative to the current manifest. It does not prove
authenticity: if someone rewrites both the payload and its manifest row, the
result can verify again.

## unverifiable

The payload is otherwise the same, but package_schema_version was changed to an
unsupported value.

The verifier cannot safely interpret the package contract, so it returns
UNVERIFIABLE / exit 2 instead of guessing PASS or FAIL.

## Schema 3

New Session Preserve exports use schema 3.0 and provider-neutral physical names:

~~~text
conversation.md
export.receipt.json
package.manifest.json
~~~

Synthetic end-to-end tests cover schema-3 exports for Codex, Claude Code, Kimi
Code, and ZCode. The examples in this directory stay on schema 2.x on purpose,
as permanent backward-compatibility evidence.
