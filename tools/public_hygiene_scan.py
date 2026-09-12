#!/usr/bin/env python3
"""Deterministic public-hygiene scan for this repository.

Fails on things that must never reach a public repository: a real home
directory path, a committed Codex rollout or session-index payload, a key or
environment file, a symlink, credential-shaped literals, an unqualified
issue or retry reference that reads as leftover internal history, and a
release workflow reaching for a stored publishing credential instead of
Trusted Publishing.

Every rule here is generic by design. This scan deliberately encodes no
knowledge of any particular account, repository, project, helper or machine.
A hygiene tool that had to name the private things it protects would publish
them itself the moment the repository went public, which is the opposite of
the job.

Scope discipline: every rule is a narrow, high-confidence shape. Ordinary
public vocabulary — "issue", "retry", "Codex", "owner", "archive" — and
synthetic fixture paths such as ``/Users/testowner/...`` must keep passing, or
the reused test suite becomes impossible to publish.

Two narrow escape hatches exist, and both are bounded by the code rather than
by a promise.

The first is the rule table below. A few rules have to spell out the shape
they forbid — an environment variable name, a token prefix — so content rules
are skipped between the two rule-table markers, but only in this file. Any
other file containing a rule-table marker is reported as
`hygiene_region_marker_misuse` and is still scanned in full, so a scanned file
can never switch the gate off for itself.

The second is for the redaction tests, which must carry credential-shaped
fixtures in order to prove those values never reach an export. Put

    # public-hygiene: synthetic-credential-fixture

on the offending line or the line directly above it. The marker exempts
credential-shape rules only. It can never exempt a real home path, a stored
publishing credential, or any other non-credential rule.

Usage:
    python3 tools/public_hygiene_scan.py [ROOT]
    exit 0  clean
    exit 1  findings (printed, deterministic order)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

MARKER = "public-hygiene: synthetic-credential-fixture"
# This scanner has to name the coordinates it forbids, so its own rule table
# is the one region content rules do not apply to. The region is bounded by
# these two markers and its size is reported on every run, so it cannot grow
# quietly. Filename rules still apply to this file like any other.
REGION_BEGIN = "hygiene-rule-table: begin"
REGION_END = "hygiene-rule-table: end"
#: The only path whose rule-table markers are honoured. Anywhere else they are
#: a finding, so a scanned file cannot exempt itself from content rules.
SCANNER_SELF_PATH = "tools/public_hygiene_scan.py"

SKIP_DIR_NAMES = {
    ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".tox", ".venv", "venv", "build", "dist", ".eggs", "node_modules",
}
SKIP_DIR_SUFFIXES = (".egg-info",)

# Home directories that are allowed to appear because they are synthetic
# fixture coordinates rather than a real person's machine.
SYNTHETIC_HOME_NAMES = {"testowner", "alice"}

# hygiene-rule-table: begin
# Filenames that must never be committed, whatever their contents.
FORBIDDEN_NAME_RULES: Tuple[Tuple[str, str, "re.Pattern[str]"], ...] = (
    ("real_session_payload",
     "a Codex rollout / session index file must never be committed",
     re.compile(r"^(rollout-.*\.jsonl|session_index\.jsonl)$")),
    ("secret_bearing_file",
     "a key, certificate or environment file must never be committed",
     re.compile(r"^(\.env(\..+)?|id_[a-z]+|.*\.(pem|p12|pfx|key|keystore))$")),
)

# (code, description, pattern, exemptable_by_marker)
CONTENT_RULES: Tuple[Tuple[str, str, "re.Pattern[str]", bool], ...] = (
    ("internal_retry_reference",
     "an internal retry-round reference",
     re.compile(r"\bRetry ?\d+\b"), False),
    ("credential_pem_private_key",
     "a PEM private key block",
     re.compile(r"-----BEGIN [A-Z ]{0,24}PRIVATE KEY-----"), True),
    ("credential_github_token",
     "a GitHub token literal",
     re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), True),
    ("credential_openai_key",
     "an OpenAI API key literal",
     re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}"), True),
    ("credential_slack_token",
     "a Slack token literal",
     re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), True),
    ("credential_aws_access_key_id",
     "an AWS access key id literal",
     re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), True),
    ("credential_google_api_key",
     "a Google API key literal",
     re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), True),
    ("credential_jwt",
     "a JSON Web Token literal",
     re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}"
                r"\.[A-Za-z0-9_-]{8,}"), True),
    ("credential_cookie_header",
     "a cookie header value",
     re.compile(r"(?i)\b(?:set-)?cookie\s*:\s*\S{16,}"), True),
    # The release workflow is the one artifact in this tree that could carry
    # an index credential. Trusted Publishing mints a short-lived token over
    # OIDC and needs no stored secret, so both a pasted token literal and a
    # regression back to stored-password auth are findings here.
    ("credential_pypi_token",
     "a PyPI API token literal",
     re.compile(r"\bpypi-AgE[A-Za-z0-9_-]{16,}"), True),
    ("stored_index_credential_reference",
     "a stored index credential; release auth must be Trusted Publishing/OIDC",
     re.compile(r"(?i)secrets\.(?:PYPI|TEST_PYPI|TWINE)[A-Z0-9_]*"
                r"|\bTWINE_PASSWORD\b"), False),
)

_HOME_RE = re.compile(r"/Users/([A-Za-z0-9._-]+)")
_ISSUE_RE = re.compile(r"#\d{2,6}\b")
# A repository-qualified reference such as ``openai/codex#24289`` is a public
# upstream coordinate and stays allowed.
_QUALIFIED_ISSUE_RE = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
# hygiene-rule-table: end


#: Per-file size of the rule-table exempt region, for reporting.
_REGION_LINES: Dict[str, int] = {}


class Finding(object):
    def __init__(self, path: str, line: int, code: str, detail: str,
                 excerpt: str) -> None:
        self.path = path
        self.line = line
        self.code = code
        self.detail = detail
        self.excerpt = excerpt

    def key(self) -> Tuple[str, int, str]:
        return (self.path, self.line, self.code)

    def render(self) -> str:
        return "%s:%d: %s: %s | %s" % (self.path, self.line, self.code,
                                       self.detail, self.excerpt)


def _excerpt(text: str, start: int, end: int) -> str:
    left = max(0, start - 24)
    right = min(len(text), end + 24)
    return text[left:right].strip().replace("\n", " ")[:120]


def _skip_dir(name: str) -> bool:
    return name in SKIP_DIR_NAMES or name.endswith(SKIP_DIR_SUFFIXES)


def iter_files(root: Path) -> Iterable[Path]:
    """Every candidate file, in deterministic order."""
    entries: List[Path] = []

    def walk(directory: Path) -> None:
        for child in sorted(directory.iterdir(), key=lambda item: item.name):
            if child.is_symlink():
                entries.append(child)
                continue
            if child.is_dir():
                if not _skip_dir(child.name):
                    walk(child)
            elif child.is_file():
                entries.append(child)

    walk(root)
    return entries


def _read_text(path: Path) -> Optional[str]:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def scan_file(path: Path, relative: str) -> List[Finding]:
    findings: List[Finding] = []
    for code, detail, pattern in FORBIDDEN_NAME_RULES:
        if pattern.match(path.name):
            findings.append(Finding(relative, 0, code, detail, path.name))
    text = _read_text(path)
    if text is None:
        return findings
    lines = text.split("\n")
    is_scanner = relative == SCANNER_SELF_PATH
    marked = set()
    exempt_region = set()
    inside_region = False
    for index, line in enumerate(lines, start=1):
        if MARKER in line:
            marked.add(index)
            marked.add(index + 1)
        if REGION_BEGIN not in line and REGION_END not in line:
            if inside_region:
                exempt_region.add(index)
            continue
        if not is_scanner:
            # Only this scanner's own rule table may switch content rules off.
            findings.append(Finding(
                relative, index, "hygiene_region_marker_misuse",
                "a rule-table marker outside %s cannot disable content rules"
                % SCANNER_SELF_PATH, _excerpt(line, 0, len(line))))
            continue
        if REGION_BEGIN in line:
            inside_region = True
        exempt_region.add(index)
        if REGION_END in line:
            inside_region = False
    if not is_scanner:
        exempt_region = set()
    _REGION_LINES[relative] = len(exempt_region)
    for index, line in enumerate(lines, start=1):
        if index in exempt_region:
            continue
        # The synthetic-credential marker only exempts the credential rules
        # below via ``marked``; it must never switch a whole line off, or a
        # non-credential finding sharing the line would escape.
        for code, detail, pattern, exemptable in CONTENT_RULES:
            for match in pattern.finditer(line):
                if exemptable and index in marked:
                    continue
                findings.append(Finding(
                    relative, index, code, detail,
                    _excerpt(line, match.start(), match.end())))
        for match in _HOME_RE.finditer(line):
            if match.group(1) not in SYNTHETIC_HOME_NAMES:
                findings.append(Finding(
                    relative, index, "private_home_path",
                    "a home directory that is not a synthetic fixture path",
                    _excerpt(line, match.start(), match.end())))
        for match in _ISSUE_RE.finditer(line):
            before = line[:match.start()]
            if _QUALIFIED_ISSUE_RE.search(before):
                continue
            findings.append(Finding(
                relative, index, "internal_issue_reference",
                "an unqualified issue/PR reference reads as internal history",
                _excerpt(line, match.start(), match.end())))
    return findings


def scan(root: Path) -> List[Finding]:
    findings: List[Finding] = []
    for path in iter_files(root):
        # POSIX separators, so SCANNER_SELF_PATH compares identically on
        # every platform.
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            findings.append(Finding(
                relative, 0, "symlink_in_tree",
                "a symlink must not be published", relative))
            continue
        findings.extend(scan_file(path, relative))
    findings.sort(key=Finding.key)
    return findings


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(args[0]) if args else Path(__file__).resolve().parent.parent
    root = root.resolve()
    findings = scan(root)
    counts: Dict[str, int] = {}
    for item in findings:
        counts[item.code] = counts.get(item.code, 0) + 1
    for item in findings:
        print(item.render())
    exempt = sum(_REGION_LINES.values())
    print("public-hygiene scan: root=%s files=%d findings=%d "
          "rule_table_exempt_lines=%d" % (
              root.name, len(list(iter_files(root))), len(findings), exempt))
    if findings:
        for code in sorted(counts):
            print("  %s: %d" % (code, counts[code]))
        print("HYGIENE_VERDICT=FAIL")
        return 1
    print("HYGIENE_VERDICT=PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
