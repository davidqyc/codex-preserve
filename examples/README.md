# Examples

Three small packages that demonstrate the whole user-visible verification
contract: `PASS` / `FAIL` / `UNVERIFIABLE` and exit codes `0` / `1` / `2`.

Run all three:

```bash
./examples/run_examples.sh
```

The same three packages are asserted by `tests/test_examples.py`, so they are
regression evidence rather than decoration: if the verdict or the exit code of
any example ever changes, the test suite fails.

| example | command | verdict | exit |
| ------- | ------- | ------- | ---- |
| `pass` | `codex-preserve verify examples/pass` | `PASS` | `0` |
| `fail` | `codex-preserve verify examples/fail` | `FAIL` | `1` |
| `unverifiable` | `codex-preserve verify examples/unverifiable` | `UNVERIFIABLE` | `2` |

Add `--json` to any of them to see the machine receipt with per-member reason
codes.

## Where these came from

`examples/pass` is unmodified `codex-preserve` export output. It was produced
by the exporter from a synthetic rollout built by the test suite, inside a
synthetic home directory. There is no real Codex conversation, no real
session, and no real machine path in any of these files — the session id,
the workspace, the repository name `testowner/demo-project` and every message
are fixtures.

`fail` and `unverifiable` are copies of `pass` with exactly one change each.

### `pass` — provably intact

Every member the manifest attests is present, and its bytes still hash to the
recorded SHA-256 and size.

### `fail` — provably altered

One byte of the attested conversation file `对话记录.md` was changed in place
(`Audit finished` became `Audit finishea`) and the manifest was **not**
rewritten. The file length is identical, so nothing about the package looks
suspicious from a directory listing. Verification still reports the payload
as altered:

```text
reason: member_sha256_mismatch [对话记录.md] — attested sha256 …, found …
```

That is the case the manifest exists to catch. Note what it does *not* catch:
someone who edits the payload **and** recomputes the manifest row produces a
package that verifies. See "What verification does and does not prove" in the
top-level README.

### `unverifiable` — cannot be determined, and says so

The package payload is byte-identical to `pass`. Only the manifest's
`package_schema_version` was changed to `99.0`, a version this build does not
recognize.

Nothing here has been shown to be wrong, so reporting `FAIL` would claim more
than is known. A verifier that cannot interpret the manifest also must not
report `PASS`, because a future schema could attest members through keys this
build never reads. The correct answer is the third state:

```text
reason: package_schema_unsupported — package_schema_version '99.0' is not one
of the supported versions 2.1, 2.2
```

Determinate bad evidence always outranks unknown evidence: a package with one
proven-altered member and one member whose state cannot be determined is
`FAIL`, not `UNVERIFIABLE`. Unknown state is never promoted to `FAIL`.
