# Claude Code session retention and independent preservation

Claude Code stores local session transcripts on your machine, but those files are not a permanent archive.

By default, Claude Code can remove inactive session transcripts after a retention period. Its settings reference documents \`cleanupPeriodDays\`, and current issue reports show that users can lose local JSONL transcripts without an interactive warning.

If a session matters, treat retention and preservation as two separate problems:

1. **Reduce cleanup risk inside Claude Code.**
2. **Keep an independent copy outside Claude Code for sessions you cannot afford to lose.**

Session Preserve addresses the second problem. It does not replace Claude Code's retention settings, and it cannot recover a transcript that has already been deleted.

## 1. Increase Claude Code's retention period

Claude Code reads user settings from:

~~~text
~/.claude/settings.json
~~~

If you use \`CLAUDE_CONFIG_DIR\`, use that directory instead.

Set \`cleanupPeriodDays\` to a **large positive integer** appropriate for your workflow. For example:

~~~json
{
  "cleanupPeriodDays": 3650
}
~~~

That example asks Claude Code to retain inactive session transcripts for roughly ten years.

Do **not** use \`0\` as a "keep forever" value. Current Claude Code versions reject \`cleanupPeriodDays: 0\`; older versions had behavior where \`0\` could disable transcript persistence instead of disabling cleanup.

After changing the file:

1. start Claude Code;
2. run \`/status\` and confirm your user settings file loaded;
3. run \`claude doctor\` if Claude Code reports a settings error.

Official settings documentation:

- https://code.claude.com/docs/en/settings
- https://code.claude.com/docs/en/settings-reference

## 2. Do not treat the retention setting as a backup

A long \`cleanupPeriodDays\` value reduces one documented cleanup risk. It is not an independent backup.

There are open Claude Code issue reports where users say transcripts were deleted despite a large configured value, including paths that may load a restricted set of settings. The main long-running data-loss thread is:

- https://github.com/anthropics/claude-code/issues/59248

So, for important sessions, keep a copy outside Claude Code's own session store.

## 3. Preserve one important session with Session Preserve

Session Preserve reads an already-persisted Claude Code session and writes an independent schema-3 preservation package.

Install it:

~~~bash
python -m pip install session-preserve
~~~

Then select the **top-level session JSONL** you want to preserve and export it:

~~~bash
session-preserve export claude \
  --source ~/.claude/projects/.../<session>.jsonl \
  --output-dir ./exports
~~~

The resulting package contains:

~~~text
conversation.md
export.receipt.json
package.manifest.json
~~~

You can verify it later:

~~~bash
session-preserve verify ./exports/<package-dir>
~~~

And optionally build a transfer ZIP after verification:

~~~bash
session-preserve pack ./exports/<package-dir>
~~~

Project:

- https://github.com/davidqyc/session-preserve
- https://pypi.org/project/session-preserve/

## 4. What Session Preserve does not do

Session Preserve is intentionally narrower than a continuous backup or recovery system.

It does **not**:

- recover a transcript that Claude Code has already deleted;
- continuously mirror every Claude Code session;
- restore a package back into Claude Code;
- sync sessions between machines;
- choose every session automatically;
- guarantee that every message visible in the Claude UI had already been flushed to disk.

The Claude adapter preserves one explicitly selected, already-persisted top-level session at a time.

That last boundary matters. A stable local JSONL is evidence about what was persisted to disk; it is not proof that the UI had no newer unflushed content.

## 5. Why keep an independent package?

Copying the raw JSONL is already better than having no outside copy.

Session Preserve adds a stricter preservation layer:

- a readable conversation view;
- source provenance and coverage information;
- provider-specific diagnostics;
- a manifest with byte sizes and SHA-256 values;
- later manifest-relative integrity verification;
- read-only behavior toward the Claude Code source session.

This is useful when a particular session is important enough that you want to know later whether the preservation package is still complete and byte-identical to what was recorded at export time.

It is **not** an authenticity system. The manifest is stored with the package and is not independently signed.

## Practical rule

For normal Claude Code history:

> use a sensible positive \`cleanupPeriodDays\` value and verify that Claude Code loaded it.

For a session you would be unhappy to lose:

> keep an independent copy outside Claude Code as well.

For a session that is already gone:

> Session Preserve cannot reconstruct it; use recovery tools or filesystem backups instead.

## References

- Claude Code settings: https://code.claude.com/docs/en/settings
- Claude Code retention/data-loss thread: https://github.com/anthropics/claude-code/issues/59248
- \`cleanupPeriodDays: 0\` documentation/behavior history: https://github.com/anthropics/claude-code/issues/41800
- Settings-source retention bug report: https://github.com/anthropics/claude-code/issues/45735
