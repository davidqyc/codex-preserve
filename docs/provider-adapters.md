# Internal provider adapter extension contract

[简体中文](provider-adapters.zh-CN.md)

The shared CLI dispatches a registered provider and publishes its schema-3
package spec. Source selection, native parsing, privacy, coverage and rendering
belong to the adapter. This is a small internal static registry: no entry points,
dynamic discovery, runtime plugin installation or common conversation ontology.
The registered providers are `codex` (OpenAI Codex), `claude` (Claude Code),
`kimi` (Kimi Code) and `zcode` (ZCode). The meaning of `kimi` stays Kimi Code.

`src/codex_preserve/_provider_registry.py` defines frozen `ProviderAdapter`
entries and the immutable `PROVIDER_REGISTRY`. Each entry owns:

| Field | Responsibility |
| --- | --- |
| `key`, `display_name` | Stable CLI/package identity and human-readable name |
| `description`, `example` | Provider parser help and example metadata |
| `configure_arguments(parser)` | Native selection arguments |
| `select_and_parse(options)` | Explicit local selection, returning the provider's native safe result |
| `build_spec(source)` | Native rendering and receipt projection into `V3PackageSpec` |
| `parser_factory()` (optional) | Reuse an existing rich parser, including Codex's argument groups and defaults |
| `prepare_options(options, parser)` (optional) | Prepare options and reject unsupported combinations |
| `candidate_listing(options)` (optional) | Return sanitized candidate output, or `None` to continue exporting |

Ordinary entries receive `--output-dir`, `--quiet` and `--stdout-receipt`.
A parser factory already owns those options; the registry sets its public output
default without adding duplicate options. Preparation runs before candidate
listing. A candidate result, including an empty list, ends dispatch before
source parsing or package publication.

`ProviderExportBlocked` carries only fixed or privacy-scrubbed diagnostics and
produces exit 2. Unknown exceptions are not converted into a successful export.
A spec with an unstable source is blocked before the writer runs. Codex retains
its existing rich parser, timestamp preparation, privacy scrub for candidate
lists and blocked selection, and legacy ZIP-option rejection.

The package writer, manifest, verifier and derived ZIP flow are shared. Schema
remains 3.0, and legacy 2.1/2.2 verification remains supported. Each provider
keeps its native source and conversation semantics.

## Adding a provider in a future scoped change

1. Establish the canonical persisted local transcript source and exact selection
   identity. Inspect only necessary structures, read-only. Confirm that the
   source supports readable session preservation before exposing a new public
   export command. Do not use network or model calls to fill missing content.
2. Add a source/parser module returning safe recognized information. Keep
   reasoning, raw tool bodies, browser/account/credential data, private file
   bodies and unknown raw values out. Define source stability and digest scope
   honestly; unsupported structures produce diagnostics without raw values.
3. Add a projection/renderer that returns `V3PackageSpec`. Reuse the package
   envelope while preserving native conversation semantics and truthful
   coverage. Document digest scope; it does not attest authenticity, UI
   completeness or execution success.
4. Add one static registration entry with native arguments and parse/spec hooks.
   No provider-specific dispatch branch should be needed in the shared CLI.
   Update the public help table and examples deliberately, with compatibility
   checks for the existing commands.
5. Add hand-authored synthetic fixtures and tests for selection, parser schemas,
   stable/changing sources, privacy, hook dispatch, CLI behavior and schema-3
   verification. Compare existing providers' help, canonical package bytes,
   normalized CLI output and error/exit behavior before and after the change.
6. Document the source, selection, coverage and privacy boundaries in English
   and Chinese. Run the full suite, legacy verifier checks, public hygiene,
   golden regression, examples and `git diff --check`.

Export and verification remain local-first, source-read-only and free of model
or network calls. This registry introduces no runtime dependency or schema bump.
It does not add restore, import, resume, sync, daemon or runtime-control features.
