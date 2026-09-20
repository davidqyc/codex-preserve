#!/usr/bin/env python3
"""codex-preserve export/verify core: Conversation Package v2.2.

Reads one already-persisted local Codex rollout JSONL and renders a concise
human-readable Conversation Export Markdown plus a machine receipt JSON inside
a stable, recognizable, file-bearing per-session package.

Boundaries this tool deliberately keeps:

- the rollout is read-only source evidence and is never mutated;
- no hook, telemetry exporter, App Server client, daemon or event database;
- no model/network call is made to select, summarize or paraphrase content;
- provider-visible reasoning summaries may be exported, opaque/encrypted
  reasoning is counted only and never decoded or reconstructed.

Everything the exporter cannot mechanically prove is reported as unknown in the
receipt rather than guessed.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import zipfile
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Tuple
from urllib.parse import quote

EXPORTER_VERSION = "2.2.1"
PACKAGE_SCHEMA_VERSION = "2.2"
# The only package schema versions this code can actually interpret: 2.2 is
# what it writes, and 2.1 is the historical shape the legacy preservation
# path below still understands. Nothing else is recognized.
RECOGNIZED_PACKAGE_SCHEMA_VERSIONS = ("2.1", "2.2")
ARTIFACT_PROVENANCE_VERSION = "turn-identity-a-plus-v1"
SELECTION_POLICY_VERSION = "structural-decision-point-v1"
FOLD_POLICY_VERSION = "structural-polls-only-v1"

DEFAULT_OUTPUT_DIR = "~/Desktop/Codex导出"
DISCOVERY_ROOTS = ("~/.codex/sessions", "~/.codex/archived_sessions")
DEFAULT_SESSION_INDEX = "~/.codex/session_index.jsonl"
# Colon-separated override for the two bounded discovery roots. The home
# directory is never scanned broadly, with or without this variable.
DISCOVERY_ROOTS_ENV = "CODEX_EXPORT_SESSION_ROOTS"
ROLLOUT_GLOB = "rollout-*.jsonl"

DEFAULT_ATTACHMENT_SNAPSHOT_BYTES = 64 * 1024
DEFAULT_MAX_PACKAGE_BYTES = 256 * 1024 * 1024
DEFAULT_COMMAND_CHARS = 200
DEFAULT_REASONING_CAP = 40
MAX_SINCE_HOURS = 24 * 365

CONVERSATION_FILENAME = "对话记录.md"
RECEIPT_FILENAME = "ConversationExport.receipt.json"
PACKAGE_MANIFEST_FILENAME = "package.manifest.json"
ATTACHMENTS_DIRNAME = "输入附件"
ARTIFACTS_DIRNAME = "输出文件"
OUTPUT_INDEX_MEMBER = ARTIFACTS_DIRNAME + "/输出索引.md"
HANDOFF_ZIP_FILENAME = "完整会话包.zip"

# Package v2 names remain readable only for bounded compatibility discovery
# and a later re-render. Repository tests never touch real Owner packages.
LEGACY_V2_CONVERSATION_FILENAME = "ConversationExport.md"
LEGACY_V2_ATTACHMENTS_DIRNAME = "attachments"
LEGACY_V2_ARTIFACTS_DIRNAME = "artifacts"
LEGACY_V2_HANDOFF_ZIP_FILENAME = "handoff.zip"

# Finder writes this file into any folder the Owner opens, including a
# published package and its payload directories. It is volatile display
# metadata rather than package payload: it is never attested by a manifest or
# receipt, and the accepted package contract already excludes it from
# integrity comparison.
VOLATILE_FINDER_METADATA_FILENAME = ".DS_Store"

UNCLASSIFIED_PROJECT_BUCKET = "未归类"
SYSTEM_PROJECT_BUCKET = "_系统任务"
THREAD_TITLE_BYTE_LIMIT = 120
RESERVED_PROJECT_BUCKET_RE = re.compile(r"^_v1_flat_archive_", re.I)

# One complete package is assembled here and only then published into its
# canonical directory. A publish attempt that fails before that commit
# therefore never creates or removes a directory entry inside the canonical
# package, so the package keeps its Finder modified time. Staging lives under
# the output root, so it is always on the package's own filesystem and the
# publishing rename stays atomic. safe_display_component strips leading dots,
# so this name can never collide with a package directory or project bucket.
STAGING_DIRNAME = ".codex-export-staging"


# --------------------------------------------------------------------------
# Export status
# --------------------------------------------------------------------------

STATUS_COMPLETE = "COMPLETE"
STATUS_PARTIAL = "PARTIAL_INTERRUPTED"
STATUS_DEGRADED = "DEGRADED_SCHEMA_DRIFT"
STATUS_BLOCKED_PACKAGE = "BLOCKED_PACKAGE_MIGRATION"
STATUS_BLOCKED_SOURCE = "BLOCKED_SOURCE_CHANGED"
STATUS_BLOCKED_SELECTION = "BLOCKED_SELECTION"
STATUS_BLOCKED_PRIVACY = "BLOCKED_PRIVACY"

# Most severe first. A blocked status never publishes a normal Markdown export.
STATUS_PRECEDENCE = (
    STATUS_BLOCKED_PACKAGE,
    STATUS_BLOCKED_SOURCE,
    STATUS_BLOCKED_PRIVACY,
    STATUS_BLOCKED_SELECTION,
    STATUS_DEGRADED,
    STATUS_PARTIAL,
    STATUS_COMPLETE,
)
BLOCKED_STATUSES = frozenset(
    (STATUS_BLOCKED_PACKAGE, STATUS_BLOCKED_SOURCE,
     STATUS_BLOCKED_PRIVACY, STATUS_BLOCKED_SELECTION)
)


class ExportBlocked(Exception):
    """Raised when the exporter must fail closed instead of publishing."""

    def __init__(self, status: str, detail: str, receipt: Optional[dict] = None):
        super().__init__("%s: %s" % (status, detail))
        self.status = status
        self.detail = detail
        self.receipt = receipt or {}


# --------------------------------------------------------------------------
# Schema registries. Anything outside these is reported, never silently kept.
# --------------------------------------------------------------------------

KNOWN_RECORD_TYPES = frozenset(
    (
        "session_meta",
        "turn_context",
        "world_state",
        "compacted",
        "response_item",
        "event_msg",
    )
)
KNOWN_RESPONSE_ITEM_TYPES = frozenset(
    (
        "message",
        "reasoning",
        "custom_tool_call",
        "custom_tool_call_output",
        "function_call",
        "function_call_output",
    )
)
KNOWN_EVENT_MSG_TYPES = frozenset(
    (
        "item_completed",
        "token_count",
        "task_started",
        "task_complete",
        "thread_settings_applied",
        "turn_aborted",
    )
)
KNOWN_ITEM_TYPES = frozenset(
    (
        "UserMessage",
        "AgentMessage",
        "Reasoning",
        "CommandExecution",
        "FileChange",
        "ContextCompaction",
        "McpToolCall",
    )
)
# Record classes that legitimately carry no payload `type` discriminator.
UNTYPED_PAYLOAD_RECORDS = frozenset(
    ("session_meta", "turn_context", "world_state", "compacted")
)

# Exact lifecycle wire vocabularies for the persisted rollout protocol.
MAX_LIFECYCLE_IDENTIFIER_CHARS = 512
MCP_TOOL_CALL_TERMINAL_STATUSES = frozenset(("completed", "failed"))
MCP_TOOL_CALL_NON_TERMINAL_STATUSES = frozenset(("inProgress",))
MCP_TOOL_CALL_PRIVATE_PAYLOAD_KEYS = (
    ("arguments", "mcp_tool_call_arguments"),
    ("result", "mcp_tool_call_result"),
    ("error", "mcp_tool_call_error"),
)
MCP_TOOL_CALL_CONNECTOR_METADATA_KEYS = frozenset((
    "pluginId", "plugin_id", "readOnlyHint", "read_only_hint",
    "connectorId", "connector_id", "appId", "app_id",
    "link", "resourceUri", "resource_uri", "resourceLink", "resource_link",
))
TURN_ABORT_REASONS = frozenset((
    "interrupted", "replaced", "review_ended", "budget_limited"
))
TURN_ABORT_TIMING_KEYS = ("started_at", "completed_at", "duration_ms")


# --------------------------------------------------------------------------
# Privacy: mandatory redaction
# --------------------------------------------------------------------------

# High-confidence credential shapes. These are also used for the post-render
# sanitization rescan, so anything matched here must be replaced during render.
CREDENTIAL_SHAPE_RULES: Tuple[Tuple[str, Any], ...] = (
    ("private_key_block", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.S,
    )),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}")),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("authorization_header", re.compile(
        r"(?i)\bauthorization\s*[:=]\s*[\"']?(?:(?:bearer|basic|token)\s+)?"
        r"[A-Za-z0-9._~+/=-]{16,}"
    )),
)

# Named-credential assignments. Deliberately key-name driven so that ordinary
# Owner prose and project identifiers (commit SHAs, file hashes, token counts)
# are never deleted by a broad pattern.
CREDENTIAL_KEY_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])"
    r"(secret|secrets|token|tokens|password|passwd|passphrase|cookie|api[_-]?key"
    r"|apikey|access[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token"
    r"|client[_-]?secret|auth[_-]?token|session[_-]?token|private[_-]?key"
    r"|credential|credentials|bearer)"
    r"(\"?\s*[:=]\s*\"?)"
    r"([^\s\"',;)}\]]{6,})"
)
CREDENTIAL_FLAG_RE = re.compile(
    r"(?i)(--(?:password|token|api-key|apikey|access-token|client-secret|secret)"
    r"(?:=|\s+))([^\s\"';|&]{6,})"
)
COOKIE_HEADER_RE = re.compile(r"(?i)(\bset-cookie\s*:\s*|\bcookie\s*:\s*)([^\r\n]{6,})")

# Values that look like a credential key assignment but are not secrets.
CREDENTIAL_VALUE_ALLOW_RE = re.compile(
    r"^(?:[0-9]+|null|true|false|none|undefined|<[A-Z_]+>|\$[A-Z_]+"
    r"|<REDACTED:[A-Za-z_]+>|REDACTED[A-Za-z_]*|\.\.\.)$",
    re.I,
)

# Account / billing identity that must be dropped everywhere it appears.
ACCOUNT_IDENTITY_KEYS = (
    "account_id",
    "account_email",
    "auth_mode",
    "chatgpt_account_id",
    "chatgpt_plan_type",
    "plan_type",
)

MAC_ADDRESS_RE = re.compile(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b")
PRIVATE_IPV4_RE = re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"
)
IOS_UDID_RE = re.compile(r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}\b")
SSH_ALIAS_REMOTE_RE = re.compile(
    r"(?:ssh://)?git@[A-Za-z0-9._-]+[:/]([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+?)(?:\.git)?"
    r"(?![A-Za-z0-9._-])"
)
HTTPS_REMOTE_RE = re.compile(
    r"https://(?:[^@/\s]+@)?(?:www\.)?github\.com/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+?)"
    r"(?:\.git)?(?![A-Za-z0-9._-])"
)
TMP_DIR_RE = re.compile(r"(?:/private)?/tmp/")
VAR_FOLDERS_RE = re.compile(
    r"(?:/private)?/var/folders/[A-Za-z0-9_+-]{2}/[A-Za-z0-9_+-]+/[A-Z]/"
)


class Privacy:
    """Deterministic redaction + human-facing path normalization.

    Redaction is always on. Normalization can be turned off; the receipt records
    the two states separately so `redactions=0` is never read as
    `normalization was disabled`.
    """

    def __init__(self, normalize: bool = True):
        self.normalize_enabled = normalize
        self.redactions: Counter = Counter()
        self.normalizations: Counter = Counter()
        self.drops: Counter = Counter()
        self._frozen = False
        self._literals: List[Tuple[str, str, str]] = []  # (name, literal, replacement)

    # -- configuration -----------------------------------------------------

    def add_literal(self, name: str, literal: str, replacement: str) -> None:
        literal = (literal or "").rstrip("/")
        if not literal or literal == "/":
            return
        for existing in self._literals:
            if existing[1] == literal:
                return
        self._literals.append((name, literal, replacement))
        # Longest literal first so nested roots normalize to the deepest label.
        self._literals.sort(key=lambda item: len(item[1]), reverse=True)

    def freeze(self) -> None:
        """Stop counting. Used for the second, byte-identical render pass."""
        self._frozen = True

    def _count(self, bucket: Counter, name: str, hits: int) -> None:
        if hits and not self._frozen:
            bucket[name] += hits

    def note_drop(self, category: str, count: int = 1) -> None:
        self._count(self.drops, category, count)

    # -- text pipeline -----------------------------------------------------

    def clean(self, text: Optional[str]) -> str:
        """Full pipeline for any provider text that reaches a generated artifact."""
        if not text:
            return ""
        return self.normalize_paths(self.redact(text))

    def clean_without_count(self, text: Optional[str]) -> str:
        """Apply the same policy for derived identity without receipt inflation."""
        frozen = self._frozen
        self._frozen = True
        try:
            return self.clean(text)
        finally:
            self._frozen = frozen

    def redact(self, text: str) -> str:
        if not text:
            return ""
        for name, pattern in CREDENTIAL_SHAPE_RULES:
            text, hits = pattern.subn("<REDACTED:%s>" % name, text)
            self._count(self.redactions, name, hits)

        def _assignment(match: "re.Match") -> str:
            value = match.group(3)
            if CREDENTIAL_VALUE_ALLOW_RE.match(value):
                return match.group(0)
            self._count(self.redactions, "assigned_credential", 1)
            return "%s%s<REDACTED:credential>" % (match.group(1), match.group(2))

        text = CREDENTIAL_KEY_RE.sub(_assignment, text)

        def _flag(match: "re.Match") -> str:
            self._count(self.redactions, "cli_credential_flag", 1)
            return "%s<REDACTED:credential>" % match.group(1)

        text = CREDENTIAL_FLAG_RE.sub(_flag, text)

        def _cookie(match: "re.Match") -> str:
            self._count(self.redactions, "cookie_header", 1)
            return "%s<REDACTED:cookie>" % match.group(1)

        text = COOKIE_HEADER_RE.sub(_cookie, text)
        return text

    def normalize_paths(self, text: str) -> str:
        if not text or not self.normalize_enabled:
            return text or ""
        for name, literal, replacement in self._literals:
            if literal in text:
                self._count(self.normalizations, name, text.count(literal))
                text = text.replace(literal, replacement)

        def _count(pattern, replacement: str, name: str, value: str) -> str:
            value, hits = pattern.subn(replacement, value)
            self._count(self.normalizations, name, hits)
            return value

        text = _count(VAR_FOLDERS_RE, "$TMP/", "tmp_dir", text)
        text = _count(TMP_DIR_RE, "$TMP/", "tmp_dir", text)
        text = _count(SSH_ALIAS_REMOTE_RE, r"\1/\2", "git_remote_identity", text)
        text = _count(HTTPS_REMOTE_RE, r"\1/\2", "git_remote_identity", text)
        text = _count(MAC_ADDRESS_RE, "<MAC_ADDRESS>", "device_identifier", text)
        text = _count(PRIVATE_IPV4_RE, "<PRIVATE_IPV4>", "device_identifier", text)
        text = _count(IOS_UDID_RE, "<DEVICE_UDID>", "device_identifier", text)
        return text

    def repo_identity(self, remote_url: Optional[str]) -> Optional[str]:
        """Reduce a remote URL (including custom SSH host aliases) to owner/repo."""
        if not remote_url:
            return None
        for pattern in (SSH_ALIAS_REMOTE_RE, HTTPS_REMOTE_RE):
            match = pattern.search(remote_url)
            if match:
                return "%s/%s" % (match.group(1), match.group(2))
        return None


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def source_identity(path: Path) -> Dict[str, Any]:
    stat = path.stat()
    return {
        "sha256": sha256_file(path),
        "bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def iso_utc(value: Any) -> Optional[str]:
    """Best-effort ISO-8601 UTC rendering for epoch seconds / ms / ISO strings."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e11:  # milliseconds
            seconds /= 1000.0
        return (
            datetime.fromtimestamp(seconds, tz=timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )
    return None


def compact_stamp(iso_text: Optional[str]) -> str:
    if not iso_text:
        return "unknown-time"
    cleaned = re.sub(r"[^0-9]", "", iso_text)
    if len(cleaned) >= 14:
        return "%s-%s" % (cleaned[:8], cleaned[8:14])
    return "unknown-time"


def safe_label(text: Optional[str], fallback: str = "codex-session") -> str:
    text = (text or "").strip()
    text = text.replace("/", "-")
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-._")
    return text or fallback


GENERIC_OWNER_REQUESTS = frozenset(
    (
        "run", "go", "execute", "review", "audit", "continue",
        "run it", "do it", "按附件执行", "执行", "跑", "跑吧", "审核",
        "审阅", "继续", "开始", "开始吧",
    )
)
SAFE_ARTIFACT_SUFFIXES = frozenset(
    (
        ".csv", ".diff", ".docx", ".gif", ".jpeg", ".jpg", ".json",
        ".md", ".patch", ".pdf", ".png", ".pptx", ".sha256", ".tar.gz",
        ".txt", ".webp", ".xlsx", ".zip",
    )
)
ABSOLUTE_PATH_TOKEN_RE = re.compile(
    r"(?:(?:file://)?/(?:[^\s`\"'<>|]+)|~/(?:[^\s`\"'<>|]+))"
)


def truncate_utf8(text: str, byte_limit: int) -> str:
    """Bound a filesystem component without splitting a Unicode sequence."""
    raw = text.encode("utf-8")
    if len(raw) <= byte_limit:
        return text
    return raw[:byte_limit].decode("utf-8", errors="ignore").rstrip()


def safe_display_component(text: Optional[str], fallback: str,
                           byte_limit: int = 120) -> str:
    """Unicode-preserving, traversal-safe Finder-visible component."""
    value = unicodedata.normalize("NFKC", (text or "")).strip()
    value = ABSOLUTE_PATH_TOKEN_RE.sub(" ", value)
    value = re.sub(r"<REDACTED:[^>]+>", " ", value, flags=re.I)
    value = re.sub(r"[\x00-\x1f\x7f/\\:]", " ", value)
    value = re.sub(r"[\s]+", " ", value)
    value = re.sub(r"[^\w\s.()\[\]{}+@#%&=,，。！？!?·｜—_-]", " ", value,
                   flags=re.UNICODE)
    value = re.sub(r"\s+", " ", value).strip(" ._-\t")
    while ".." in value:
        value = value.replace("..", ".")
    value = truncate_utf8(value, byte_limit).strip(" ._-")
    return value or fallback


def safe_member_filename(text: Optional[str], fallback: str = "file",
                         byte_limit: int = 160) -> str:
    """Safe single member name; useful CJK remains visible."""
    name = safe_display_component(os.path.basename(text or ""), fallback,
                                  byte_limit=byte_limit)
    if name in (".", ".."):
        return fallback
    return name


def safe_source_reference(path: str, privacy: "Privacy") -> str:
    """Normalized provenance only; never retain an unclassified absolute root."""
    cleaned = privacy.clean(path)
    if cleaned.startswith("file:///"):
        cleaned = cleaned[7:]
    if cleaned.startswith("/"):
        return "$ABSOLUTE/%s" % safe_member_filename(path, "file")
    return cleaned


# Owners acknowledge and steer work in short bursts: "审吧", "快审吧",
# "继续，不用停". None of these name a task, so none of them may rename a
# package. The vocabulary below is deliberately bounded and is matched against
# a *whole* clause, so any real object keeps the clause meaningful: "清理缓存"
# and "修复标题" are tasks and stay tasks.
_ACK_ADVERBS = "继续|赶紧|直接|尽快|马上|现在|快|再|就|先"
_ACK_VERBS = (
    "审阅|审核|审查|复审|检查|查看|运行|执行|开始|继续|确认"
    "|往下|接着|审|查|看|跑|做|来|走|上|干"
)
_ACK_PARTICLES = "一下|吧|呗|啦|咯|喽|哈|呀|啊|了|下"
# The verb may be reduplicated, which is how Chinese softens an imperative:
# "看看", "查查", "跑跑" are the same instruction as "看", "查", "跑".
_ACK_CLAUSE_RE = re.compile(
    r"(?:%s)*(?P<verb>%s)(?P=verb)?(?:%s)*"
    % (_ACK_ADVERBS, _ACK_VERBS, _ACK_PARTICLES)
)
_ACK_CONTINUATION_RE = re.compile(
    r"(?:不用|不要|别|勿|无需|不必)(?:停下来|停下|停|等|问|管|再问|确认)"
)
_ACK_ENGLISH_RE = re.compile(
    r"(?:please\s+)?"
    r"(?:go\s+ahead|keep\s+going|carry\s+on|looks?\s+good|lgtm|ship\s+it"
    r"|proceed|continue|review|approve|check|verify|run|go|execute|audit)"
    r"(?:\s+it|\s+them|\s+now|\s+please)?"
)


# A byte-exact rehearsal over the whole eligible package set found six
# packages whose newest completed Owner turn was an acknowledgement, an
# execution-control hold, or presence chatter. Such a turn reports the state of
# the run; it does not name the work, so it must never displace an earlier
# descriptive title. Upstream ``openai/codex#24289`` asks for the same
# discipline from the other direction: keep the existing title rather than
# force a vague prompt into it.
#
# Every pattern below is matched against a *whole* clause with ``fullmatch``,
# and that is the entire false-positive control. The vocabularies are bounded
# lists of adverbs, bare predicates and particles, so the moment a clause also
# carries an object or a second predicate the fullmatch fails and the clause
# stays a task: ``完成证书自动补齐修复``, ``登录后检查证书状态``,
# ``安全停下服务并修复重启问题`` and ``我刚回电脑后检查导出日志`` all keep the
# trigger token and all remain meaningful. Nothing here inspects message
# length, and nothing here guesses at a task object.

# 1. Status / completion acknowledgement: a bare completion predicate wrapped
#    in degree adverbs and aspect particles, naming nothing that was completed.
_STATUS_SUBJECTS = "我这边|我这里|这边|这里|我"
_STATUS_ADVERBS = "已经|已|都|全部|全|均|刚刚|刚|基本|大致|差不多|应该"
_STATUS_PREDICATES = (
    "完成|做完|跑完|处理完|弄完|执行完|搞定|弄好|办好|做好|准备好"
    "|登录|登陆|上线|好"
)
_STATUS_TAILS = "好|完|了|啦|咯|喽|哈|呀|啊|过|的|吧|哦|嗯"
_STATUS_CLAUSE_RE = re.compile(
    r"(?:%s)?(?:%s)*(?:%s)(?:%s)*"
    % (_STATUS_SUBJECTS, _STATUS_ADVERBS, _STATUS_PREDICATES, _STATUS_TAILS)
)
_STATUS_ENGLISH_RE = re.compile(
    r"(?:it'?s\s+|that'?s\s+|all\s+|already\s+)?"
    r"(?:logged\s+in|log\s*in\s+done|all\s+set|done|completed|complete"
    r"|finished|finish|ok|okay)"
    r"(?:\s+(?:now|already|thanks|thank\s+you))?"
)

# 2. Hold / wait execution control: an order to pause and an order to wait for
#    the Owner. Real Owner intent, but it identifies no work.
_HOLD_LEADS = "安全|先|请|麻烦|那|就|现在|立刻|马上|暂时|临时|你|你们"
_HOLD_STOPS = "停下来|停下|停住|停止|暂停|停"
_HOLD_STOP_OBJECTS = "工作|活儿|活|手上的活|手头的活|动作"
_HOLD_WAIT_OBJECTS = (
    "号令|命令|指令|指示|消息|通知|信号|回复|答复|确认|批准|授权"
    "|下一步|进一步指示|我的话|我的消息"
)
_HOLD_TAILS = "吧|了|啊|呀|哈|哦|一下|下|着|再说"
_HOLD_CLAUSE_RE = re.compile(
    r"(?:%s)*(?:"
    r"(?:%s)(?:%s)?"
    r"|等(?:一下|一会儿|一会|等)?(?:我|你|您|我们)?(?:的)?(?:%s)?"
    r")(?:%s)*"
    % (_HOLD_LEADS, _HOLD_STOPS, _HOLD_STOP_OBJECTS, _HOLD_WAIT_OBJECTS,
       _HOLD_TAILS)
)
_HOLD_ENGLISH_RE = re.compile(
    r"(?:please\s+|just\s+)?"
    r"(?:stand\s+by|hold\s+on|hold\s+off|stop|pause|hold|halt|wait)"
    r"(?:\s+(?:and|then)\s+(?:wait|hold|stand\s+by|stop))?"
    r"(?:\s+for\s+(?:my|your|the)\s+"
    r"(?:instruction|instructions|signal|word|message|command|go[-\s]?ahead))?"
    r"(?:\s+(?:now|here|please))?"
)

# 3. First-person context / status chatter: the Owner reporting their own
#    presence, an elapsed interval, or offering to help. None of the three
#    names a piece of work.
_CHATTER_TIMES = "刚刚|刚才|刚|方才|之前|先前|临时|一直"
_CHATTER_MANNERS = "不小心|一不小心|不留神|突然|临时|有点|稍微|大概|可能"
_CHATTER_AWAY = "切出去|切走|切开|离开|走开|出去|不在|离线|掉线|断开"
_CHATTER_BACK = (
    "回到电脑前|回电脑跟前|回电脑前|回到座位|回座位|回电脑|回来|上线|回归"
)
_CHATTER_TAILS = "一下|一会儿|一会|一趟|了|啦|哈|呀|啊|的|吧|哦"
_CHATTER_PRESENCE_RE = re.compile(
    r"(?:我们|我这边|我这里|我|人)?(?:%s)*(?:%s)*(?:%s|%s)(?:%s)*"
    % (_CHATTER_TIMES, _CHATTER_MANNERS, _CHATTER_AWAY, _CHATTER_BACK,
       _CHATTER_TAILS)
)
_CHATTER_DURATION_RE = re.compile(
    r"(?:大概|大约|差不多|估计|可能|也就|就|前后)*(?:有|花了|用了|过了)?"
    r"(?:[一二两三四五六七八九十半几数十百]|\d)+"
    r"(?:分钟|分|秒钟|秒|个小时|小时|钟头|天|周)"
    r"(?:左右|上下|不到|多)?(?:吧|了|啊|呀|哈|的)*"
)
_CHATTER_OFFER_ACTIONS = (
    "确认|操作|处理|做|执行|介入|跟进|配合|回复|批准|授权|决定|选择|拍板"
)
_CHATTER_OFFER_RE = re.compile(
    r"(?:那|嗯|好|请问|问下|问一下)*(?:现在|目前|这边|接下来)?(?:还)?"
    r"(?:有没有|有什么|有啥|有哪些|需要)"
    r"(?:事情|事儿|事|东西|内容|地方|步骤)*(?:是)?"
    r"(?:需要|要)?(?:我|你|您|我们)"
    r"(?:直接|马上|立刻|先|亲自|手动|额外)*(?:去|来)?"
    r"(?:%s)(?:一下|下)?"
    r"(?:(?:或者|或|和|与|、|以及|还是)(?:%s)(?:一下|下)?)*"
    r"(?:的)?(?:吗|呢|么|不|嘛)*"
    % (_CHATTER_OFFER_ACTIONS, _CHATTER_OFFER_ACTIONS)
)


def _status_or_chatter_clause(clause: str) -> bool:
    """Is this whole clause only status, hold/wait control, or chatter?

    These are the three semantic classes that rehearsal proved can
    currently rename a package. Each is a whole-clause match, so a clause
    that also names work falls through and stays a task.
    """
    return bool(
        _STATUS_CLAUSE_RE.fullmatch(clause)
        or _HOLD_CLAUSE_RE.fullmatch(clause)
        or _CHATTER_PRESENCE_RE.fullmatch(clause)
        or _CHATTER_DURATION_RE.fullmatch(clause)
        or _CHATTER_OFFER_RE.fullmatch(clause)
    )


def _status_or_chatter_request(text: str) -> bool:
    """Whole-request English forms, which clause splitting would break apart.

    ``_generic_request`` splits on whitespace, so ``stop and wait`` only ever
    survives as a whole-string test, exactly like the existing English
    acknowledgement rule.
    """
    return bool(
        _STATUS_ENGLISH_RE.fullmatch(text)
        or _HOLD_ENGLISH_RE.fullmatch(text)
    )


def _acknowledgement_clause(clause: str) -> bool:
    """Is this whole clause only an acknowledgement or a steering phrase?"""
    return bool(
        clause in GENERIC_OWNER_REQUESTS
        or _ACK_CLAUSE_RE.fullmatch(clause)
        or _ACK_CONTINUATION_RE.fullmatch(clause)
        or _ACK_ENGLISH_RE.fullmatch(clause)
        or _status_or_chatter_clause(clause)
    )


# Instructions about *how* to carry the work out. They are real Owner intent
# but they do not identify the work, so they must never become a folder name.
EXECUTION_CONTROL_MARKERS = (
    "不要合并", "不用合并", "别合并", "不要提交", "不要推送",
    "不要新建", "不要创建", "不要修改", "不要动",
    "只更新", "仅更新", "只改", "仅改", "只修改", "仅修改",
    "只做", "仅做", "只跑", "仅跑",
    "自己完成", "自行完成", "自己搞定", "自己做",
    "不要停", "不用停", "别停", "不要等", "不用等",
    "do not merge", "don't merge", "do not commit", "don't commit",
    "do not push", "don't push", "update only", "only update",
)


def _execution_control_clause(text: str) -> bool:
    normalized = unicodedata.normalize("NFKC", text or "").strip().lower()
    return any(marker in normalized for marker in EXECUTION_CONTROL_MARKERS)


_SINGLE_ZIP_RELAY_RE = re.compile(
    r"(?:请\s*)?(?:只|仅)(?:上传|使用)\s*(?:这|此)(?:一|1)个\s*zip"
    r"\s*[,;。.]?\s*(?:然后|再)\s*执行\s*zip\s*内(?:的)?\s*run_this_prompt\.md"
    r"|(?:please\s+)?(?:use|upload)\s+only\s+this\s+zip"
    r"\s*[,;。.]?\s*(?:then|and\s+then)\s+(?:execute|run)"
    r"\s+run_this_prompt\.md\s+inside\s+it",
    re.I,
)


def _single_zip_relay_request(text: str) -> bool:
    """A whole dispatch wrapper naming only this ZIP and its entrypoint.

    Fullmatch preserves requests that also name work. Normalize only the
    entrypoint's Markdown spelling, not arbitrary attachment names/content.
    """
    value = unicodedata.normalize("NFKC", text or "").strip()
    value = value.replace(r"\_", "_")
    value = re.sub(r"`(zip|run_this_prompt\.md)`", r"\1", value, flags=re.I)
    value = re.sub(r"\s+", " ", value).rstrip(" .。!！")
    return bool(_SINGLE_ZIP_RELAY_RE.fullmatch(value))


def _generic_request(text: str) -> bool:
    """Does this text fail to name any work?

    The exact-phrase set stays authoritative so multi-word entries such as
    ``run it`` keep matching. Anything else is judged clause by clause, and
    every clause must be an acknowledgement for the whole request to be one.
    """
    if _single_zip_relay_request(text):
        return True
    normalized = unicodedata.normalize("NFKC", text or "").strip().lower()
    # NFKC has already folded the fullwidth forms, so the halfwidth comma and
    # semicolon must be listed too or "继续，不用停" stays one unsplittable
    # clause.
    normalized = re.sub(r"[\s，。！？!?、:：,;；.]+", " ", normalized).strip()
    if normalized in GENERIC_OWNER_REQUESTS \
            or known_thread_title_placeholder(text):
        return True
    # English acknowledgements are phrases rather than single tokens, so they
    # are matched before the text is split into clauses. The status and
    # hold/wait forms ("stop and wait for my instruction") are multi-word for
    # the same reason and are tested the same way.
    if _ACK_ENGLISH_RE.fullmatch(normalized) \
            or _status_or_chatter_request(normalized):
        return True
    clauses = [clause for clause in normalized.split(" ") if clause]
    return bool(clauses) and all(
        _acknowledgement_clause(clause) for clause in clauses
    )


_TASK_SEGMENT_SPLIT_RE = re.compile(r"[。．；;！!？?\n]+|\.(?:\s+|$)")


# How many leading meaningful request lines the title may be drawn from.
# Owners routinely put the wrapper on line one and the task on line two, so
# line one alone is not enough; the bound keeps this a fixed lookahead rather
# than a summary of an arbitrary message body.
TITLE_SCAN_LINE_LIMIT = 8


def _request_title_lines(text: str) -> List[str]:
    """The first few non-empty request lines, without list/heading marks."""
    lines = []
    for raw in (text or "").splitlines():
        line = re.sub(r"^[#>*+\-\d.)\s]+", "", raw).strip()
        if not line:
            continue
        lines.append(line)
        if len(lines) >= TITLE_SCAN_LINE_LIMIT:
            break
    return lines


def _request_task_segments(text: str) -> List[str]:
    """Every task segment in the bounded head of a request, in order."""
    segments = []
    for line in _request_title_lines(text):
        segments.extend(_task_title_segments(line))
    return segments


def _task_title_segments(text: str) -> List[str]:
    """Split one request line into sentence-like task segments.

    A full stop separates only when it ends a word, so a filename such as
    ``RUN_THIS_PROMPT.md`` is never split in half.
    """
    return [
        part.strip()
        for part in _TASK_SEGMENT_SPLIT_RE.split(text or "")
        if part and part.strip()
    ]


_BRACKETED_TASK_RE = re.compile(
    r"^[【\[〖]\s*[^】\]〗]{1,40}\s*[】\]〗]\s*(?P<rest>\S.*)$"
)


def _bracketed_task_segment(segment: str) -> bool:
    """A ``【机房】储存清理 Pass 2`` style label with real work after it."""
    match = _BRACKETED_TASK_RE.match(segment or "")
    return bool(match and not _generic_request(match.group("rest")))


def _owner_task_title(message: str) -> Tuple[Optional[str], Optional[str]]:
    """Reduce one Owner message to the task it actually names.

    Within a single turn the precedence is: an explicit bracketed task label,
    then the attachment when the request opens as a generic execute wrapper,
    then ordinary prose that names the work. Execution-control clauses such as
    ``只更新现有 PR，不要合并`` are never promoted; they say how to do the
    work, not what it is.
    """
    envelope = parse_attachment_envelope(message)
    request = envelope["owner_request_text"] if envelope else message
    stem = _first_attachment_stem(message)

    # A machine-generated wrapper names nothing, and reading past its first
    # line would expose the payload it wraps. Only its attachment, if any,
    # identifies the work.
    if known_thread_title_placeholder(request):
        return (stem, "latest_attachment_filename_stem") if stem \
            else (None, None)

    if _single_zip_relay_request(request):
        return (stem, "single_zip_relay_attachment_fallback") if stem \
            else (None, None)

    segments = _request_task_segments(request)

    for segment in segments:
        if _bracketed_task_segment(segment):
            return segment, "latest_meaningful_owner_request"

    if any(_single_zip_relay_request(segment) for segment in segments) and all(
            _generic_request(segment) or _execution_control_clause(segment)
            for segment in segments):
        return (stem, "single_zip_relay_attachment_fallback") if stem \
            else (None, None)

    # "按附件执行；..." is about the attached file. Whatever follows the
    # wrapper is execution detail, so the attachment is the better identity.
    if stem and segments and _generic_request(segments[0]) \
            and not _single_zip_relay_request(segments[0]):
        return stem, "latest_attachment_filename_stem"

    for segment in segments:
        if not _generic_request(segment) \
                and not _execution_control_clause(segment):
            return segment, "latest_meaningful_owner_request"

    if stem:
        return stem, "latest_attachment_filename_stem"
    return None, None


def _latest_owner_task_title(messages: Sequence[str]
                             ) -> Tuple[Optional[str], Optional[str]]:
    """The most recent Owner turn that names a task.

    A continued conversation is usually continued for new work, so the newest
    task is what makes the package findable. Older turns remain the answer
    only while no newer turn names anything.
    """
    relay_fallback = (None, None)
    for message in reversed(list(messages or [])):
        title, source = _owner_task_title(message or "")
        if source == "single_zip_relay_attachment_fallback":
            if not relay_fallback[0]:
                relay_fallback = (title, source)
            continue
        if title:
            return title, source
    return relay_fallback


def _first_attachment_stem(owner_message: str) -> Optional[str]:
    if FILES_MANIFEST_HEADER not in owner_message:
        return None
    block = owner_message.split(FILES_MANIFEST_HEADER, 1)[1]
    block = re.split(r"^##\s+My request:", block, 1, re.M)[0]
    match = MANIFEST_ENTRY_RE.search(block)
    if not match:
        return None
    filename = match.group("name").strip() or os.path.basename(match.group("path"))
    return os.path.splitext(filename)[0]


THREAD_PLACEHOLDER_PATTERNS = (
    re.compile(r"^codex_delegation$", re.I),
    re.compile(r"^files pasted by the user$", re.I),
    re.compile(r"^execute\s+run_this_prompt\.md\s+from\b", re.I),
    re.compile(r"^execute\s+.+\s+from\s+the\s+attached\b", re.I),
)


def known_thread_title_placeholder(text: Optional[str]) -> bool:
    """Recognize only frozen provider/internal wrapper title classes.

    Deliberately do not treat short ordinary names such as ``run`` as
    placeholders: provider-native thread names are authoritative unless exact
    wrapper evidence says otherwise.
    """
    value = unicodedata.normalize("NFKC", text or "").strip()
    if value.lower().startswith("<codex_delegation>"):
        return True
    return any(pattern.search(value) for pattern in THREAD_PLACEHOLDER_PATTERNS)


def resolve_session_thread_names(
        session_ids: Optional[Sequence[str]] = None,
        index_path: Optional[Path] = None) -> Dict[str, str]:
    """Read the provider-native append-only thread index exactly once.

    Exact canonical UUID matches are retained and later matching rows replace
    earlier ones. Missing/unreadable files and malformed unrelated rows are
    intentionally non-fatal. The index is never opened for write.
    """
    wanted = None
    if session_ids is not None:
        wanted = {
            canonical for canonical in (
                canonical_session_uuid(str(value)) for value in session_ids
            ) if canonical is not None
        }
    path = (index_path or Path(DEFAULT_SESSION_INDEX)).expanduser()
    resolved: Dict[str, str] = {}
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                try:
                    row = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(row, dict):
                    continue
                session_id = canonical_session_uuid(row.get("id"))
                thread_name = row.get("thread_name")
                if session_id is None or not isinstance(thread_name, str):
                    continue
                if wanted is not None and session_id not in wanted:
                    continue
                resolved[session_id] = thread_name
    except OSError:
        return {}
    return resolved


def _repository_basename(meta: Dict[str, Any], privacy: "Privacy") \
        -> Optional[str]:
    """Return only a mechanically persisted repository basename."""
    git = as_dict(meta.get("git"))
    candidates = [
        git.get("repository_url"), git.get("remote_url"),
        git.get("repository"), git.get("repo"), git.get("repo_name"),
        meta.get("repository_url"), meta.get("repository"),
    ]
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        repo = privacy.repo_identity(candidate)
        if repo:
            basename = repo.rsplit("/", 1)[-1]
        else:
            # Parse common persisted URL/scp/owner-repo shapes without exposing
            # hostnames, usernames, query strings or credentials.
            value = candidate.strip().split("#", 1)[0].split("?", 1)[0]
            value = value.rstrip("/")
            if "://" in value:
                value = value.split("://", 1)[1].split("/", 1)[-1]
            elif ":" in value and "/" in value.split(":", 1)[-1]:
                value = value.split(":", 1)[-1]
            basename = value.rsplit("/", 1)[-1]
            if basename.endswith(".git"):
                basename = basename[:-4]
        safe = safe_display_component(
            privacy.clean_without_count(basename), "", byte_limit=100
        )
        if safe and not RESERVED_PROJECT_BUCKET_RE.match(basename) \
                and safe not in (
                UNCLASSIFIED_PROJECT_BUCKET, SYSTEM_PROJECT_BUCKET) \
                and not RESERVED_PROJECT_BUCKET_RE.match(safe):
            return safe
    return None


def _generic_workspace(cwd: Optional[str]) -> bool:
    value = unicodedata.normalize("NFKC", cwd or "").strip().rstrip("/")
    if not value or value in ("~", ".", "/"):
        return True
    lowered = value.lower()
    if lowered == "/root" or re.fullmatch(r"/(?:users|home)/[^/]+", lowered):
        return True
    if re.match(r"^(?:/private)?/tmp(?:/|$)", lowered) \
            or re.match(r"^(?:/private)?/var/folders(?:/|$)", lowered):
        return True
    parts = [part for part in value.replace("\\", "/").split("/") if part]
    if any(part in (".", "..") for part in parts):
        return True
    basename = parts[-1].lower() if parts else ""
    if basename in {
        ".claude", ".codex", "desktop", "downloads", "documents",
        "home", "users", "root", "tmp", "private", "workspace",
    }:
        return True
    return False


def _internal_session(meta: Dict[str, Any], owner_messages: Sequence[str],
                      thread_name: Optional[str]) -> bool:
    for key in ("thread_source", "source", "originator"):
        value = unicodedata.normalize(
            "NFKC", str(meta.get(key) or "")
        ).strip().lower()
        if value in {
            "subagent", "delegated", "codex_delegation", "internal_canary",
            "canary",
        }:
            return True
    if known_thread_title_placeholder(thread_name) and \
            unicodedata.normalize("NFKC", thread_name or "").strip().lower() \
            == "codex_delegation":
        return True
    first = owner_messages[0].lstrip() if owner_messages else ""
    return first.startswith("<codex_delegation>")


def derive_project_bucket(meta: Dict[str, Any], owner_messages: Sequence[str],
                          privacy: "Privacy",
                          thread_name: Optional[str] = None
                          ) -> Tuple[str, str]:
    if _internal_session(meta, owner_messages, thread_name):
        return SYSTEM_PROJECT_BUCKET, "persisted_internal_session"
    repo = _repository_basename(meta, privacy)
    if repo:
        return repo, "persisted_repository_identity"
    cwd = meta.get("cwd") if isinstance(meta.get("cwd"), str) else None
    if cwd and not _generic_workspace(cwd):
        basename = cwd.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        safe = safe_display_component(
            privacy.clean_without_count(basename), "", byte_limit=100
        )
        if safe and not RESERVED_PROJECT_BUCKET_RE.match(basename) \
                and safe not in (
                UNCLASSIFIED_PROJECT_BUCKET, SYSTEM_PROJECT_BUCKET) \
                and not RESERVED_PROJECT_BUCKET_RE.match(safe):
            return safe, "persisted_cwd_basename"
    return UNCLASSIFIED_PROJECT_BUCKET, "unclassified_persisted_evidence"


def derive_display_title(meta: Dict[str, Any], owner_messages: Sequence[str],
                         privacy: "Privacy", label: Optional[str] = None,
                         thread_name: Optional[str] = None,
                         completed_owner_messages: Optional[
                             Sequence[str]] = None,
                         ) -> Tuple[str, str]:
    """Deterministic title naming the Owner's current task.

    Precedence is explicit label, then the latest completed meaningful Owner
    task, then the provider thread name, then the bounded fallbacks. The
    provider thread name deliberately ranks below the Owner's own latest
    request: upstream ``openai/codex#22452`` documents valid rollouts whose
    ``session_index.jsonl`` thread name stays stale or vague while the thread
    itself keeps moving, so it cannot be the sole display authority.
    A single-ZIP relay names no task: its attachment is only a fallback after
    a valid provider name, and cannot hide an earlier substantive Owner task.
    """
    if label:
        cleaned = safe_display_component(
            privacy.clean_without_count(label), "codex-session"
        )
        return cleaned, "explicit_label"

    considered = (completed_owner_messages
                  if completed_owner_messages is not None else owner_messages)
    owner_title, owner_source = _latest_owner_task_title(considered)
    if owner_title and owner_source != "single_zip_relay_attachment_fallback":
        cleaned = safe_display_component(
            privacy.clean_without_count(owner_title), ""
        )
        if cleaned:
            return cleaned, owner_source

    if thread_name and not known_thread_title_placeholder(thread_name) \
            and not _single_zip_relay_request(thread_name):
        cleaned = safe_display_component(
            privacy.clean_without_count(thread_name), "",
            byte_limit=THREAD_TITLE_BYTE_LIMIT,
        )
        if cleaned:
            return cleaned, "latest_session_index_thread_name"

    if owner_title and owner_source == "single_zip_relay_attachment_fallback":
        cleaned = safe_display_component(
            privacy.clean_without_count(owner_title), ""
        )
        if cleaned:
            return cleaned, "latest_attachment_filename_stem"

    git = as_dict(meta.get("git"))
    repo = privacy.repo_identity(git.get("repository_url"))
    fallback = (repo or "").rsplit("/", 1)[-1] or os.path.basename(
        str(meta.get("cwd") or "").rstrip("/")
    ) or "codex-session"
    return safe_display_component(
        privacy.clean_without_count(fallback), "codex-session"
    ), \
        ("repo_fallback" if repo else "cwd_fallback"
         if meta.get("cwd") else "codex_session_fallback")


def compact_minute_stamp(iso_text: Optional[str]) -> str:
    stamp = compact_stamp(iso_text)
    return stamp[:13] if stamp != "unknown-time" else stamp


def stable_package_dirname(meta: Dict[str, Any], owner_messages: Sequence[str],
                           privacy: "Privacy",
                           rollout_coordinate_sha256: str,
                           label: Optional[str] = None,
                           thread_name: Optional[str] = None,
                           completed_owner_messages: Optional[
                               Sequence[str]] = None,
                           ) -> Dict[str, str]:
    if not re.fullmatch(r"[0-9a-f]{64}", rollout_coordinate_sha256):
        raise ValueError("package identity requires a rollout coordinate sha256")
    # Only the title tracks the latest turn. The project bucket deliberately
    # keeps reading the whole conversation, so a new turn renames a package at
    # most once and never regroups it.
    title, source = derive_display_title(
        meta, owner_messages, privacy, label, thread_name,
        completed_owner_messages=completed_owner_messages,
    )
    project_bucket, project_source = derive_project_bucket(
        meta, owner_messages, privacy, thread_name
    )
    session_id = str(meta.get("session_id") or meta.get("id") or "")
    session8 = safe_label(session_id[:8], "session")
    dirname = "%s__%s__%s__%s" % (
        truncate_utf8(title, 120),
        compact_minute_stamp(meta.get("timestamp")),
        session8,
        rollout_coordinate_sha256[:8],
    )
    dirname = safe_display_component(
        dirname,
        "codex-session__unknown-time__session__rollout",
        byte_limit=220,
    )
    return {
        "display_title": title,
        "display_title_source": source,
        "project_bucket": project_bucket,
        "project_bucket_source": project_source,
        "rollout_coordinate_sha256": rollout_coordinate_sha256,
        "package_dirname": dirname,
        "package_relative_path": "%s/%s" % (project_bucket, dirname),
    }


def stable_rollout_identity(meta: Dict[str, Any], source_path: Path) -> Dict[str, str]:
    """Return an immutable coordinate for one logical rollout.

    Codex normally preserves a rollout filename while moving it from the
    active sessions tree to archived_sessions. Combining that filename with
    the persisted start time and exact session UUID keeps later turns on one
    output pair while disambiguating mechanically distinct rollout files that
    happen to claim the same session UUID.
    """
    session_id = str(meta.get("session_id") or meta.get("id") or
                     "unknown-session")
    started_at = str(meta.get("timestamp") or "unknown-time")
    filename = source_path.name
    coordinate = json.dumps(
        [session_id, started_at, filename],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    coordinate_sha256 = sha256_text(coordinate)
    return {
        "session_id": session_id,
        "started_at": started_at,
        "rollout_filename": filename,
        "coordinate_sha256": coordinate_sha256,
        "logical_rollout_id": "%s__%s" % (
            safe_label(session_id, "unknown-session"),
            coordinate_sha256[:12],
        ),
    }


def stable_output_basename(meta: Dict[str, Any], source_path: Path,
                           label: Optional[str] = None) -> str:
    """Stable basename independent of last activity or completion time."""
    identity = stable_rollout_identity(meta, source_path)
    if not label:
        privacy = Privacy(normalize=True)
        repo = privacy.repo_identity(as_dict(meta.get("git")).get("repository_url"))
        label = repo or os.path.basename((meta.get("cwd") or "").rstrip("/")) \
            or "codex-session"
    return "%s__%s__%s__%s__CodexConversationExport" % (
        safe_label(label),
        compact_stamp(identity["started_at"]),
        safe_label(identity["session_id"][:8], "session"),
        identity["coordinate_sha256"][:8],
    )


def truncate(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def joined_text(blocks: Any) -> str:
    """Concatenate provider content blocks without inventing separators."""
    out: List[str] = []
    for block in as_list(blocks):
        if isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str):
                out.append(text)
        elif isinstance(block, str):
            out.append(block)
    return "".join(out)


def exact_wire(value: Any, vocabulary: FrozenSet[str]) -> bool:
    return isinstance(value, str) and value in vocabulary


def bounded_identifier(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= MAX_LIFECYCLE_IDENTIFIER_CHARS


def turn_id_of(payload: Dict[str, Any]) -> Optional[str]:
    """Turn binding, strongest mechanical evidence first."""
    for key in ("turn_id",):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    passthrough = as_dict(payload.get("internal_chat_message_metadata_passthrough"))
    value = passthrough.get("turn_id")
    if isinstance(value, str) and value:
        return value
    return None


# --------------------------------------------------------------------------
# Rollout discovery and fail-closed selection
# --------------------------------------------------------------------------


def discovery_roots() -> List[Path]:
    configured = os.environ.get(DISCOVERY_ROOTS_ENV)
    candidates = configured.split(":") if configured else list(DISCOVERY_ROOTS)
    roots = []
    for raw in candidates:
        path = Path(raw).expanduser()
        if path.is_dir():
            roots.append(path)
    return roots


def source_class_of(path: Path) -> str:
    text = str(path)
    if "/archived_sessions/" in text:
        return "archived_sessions"
    if "/sessions/" in text:
        return "sessions"
    return "explicit_path"


def iter_rollouts(since_hours: Optional[int] = None) -> List[Path]:
    """Bounded enumeration. Only the two Codex session roots are ever scanned."""
    cutoff = None
    if since_hours is not None:
        cutoff = time.time() - (since_hours * 3600)
    found: List[Path] = []
    for root in discovery_roots():
        for path in root.rglob(ROLLOUT_GLOB):
            if not path.is_file():
                continue
            if cutoff is not None and path.stat().st_mtime < cutoff:
                continue
            found.append(path)
    found.sort()
    return found


SESSION_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def canonical_session_uuid(value: Optional[str]) -> Optional[str]:
    """Canonical lowercase session UUID, or None when the syntax is not exact.

    A prefix is not a session id. `--session-id` means the whole UUID.
    """
    text = (value or "").strip().lower()
    return text if SESSION_UUID_RE.match(text) else None


def read_session_meta(path: Path) -> Optional[Dict[str, Any]]:
    """First `session_meta` payload of a rollout. Identity only, never the body."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line or '"session_meta"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("type") == "session_meta":
                    return as_dict(record.get("payload"))
    except OSError:
        return None
    return None


def session_meta_identity(path: Path) -> Optional[str]:
    """The session UUID the rollout itself claims, or None when unusable."""
    meta = read_session_meta(path)
    if meta is None:
        return None
    claimed = meta.get("session_id") or meta.get("id")
    if not isinstance(claimed, str):
        return None
    return canonical_session_uuid(claimed)


def rollouts_for_session(session_id: str) -> Tuple[List[Path], Dict[str, Any]]:
    """Rollouts whose own `session_meta` claims this exact UUID.

    The filename is a bounded discovery hint only. Identity is decided by the
    recorded `session_meta.session_id`, so a file named after session B can
    never be exported as session A, and a prefix never resolves at all.
    """
    wanted = canonical_session_uuid(session_id)
    diagnostics: Dict[str, Any] = {
        "requested_session_id_is_canonical_uuid": bool(wanted),
        "identity_source": "session_meta.session_id",
    }
    if not wanted:
        diagnostics["discovery_mode"] = "rejected_before_discovery"
        return [], diagnostics
    everything = iter_rollouts()
    hinted = [path for path in everything if wanted in path.name.lower()]
    pool = hinted or everything
    diagnostics["discovery_mode"] = (
        "filename_hint_then_session_meta_crosscheck" if hinted
        else "session_meta_scan"
    )
    diagnostics["candidates_examined"] = len(pool)
    matches: List[Path] = []
    name_mismatch = 0
    meta_unreadable = 0
    for path in pool:
        identity = session_meta_identity(path)
        if identity == wanted:
            matches.append(path)
        elif wanted in path.name.lower():
            if identity is None:
                meta_unreadable += 1
            else:
                name_mismatch += 1
    diagnostics["filename_claimed_but_session_meta_differs"] = name_mismatch
    diagnostics["filename_claimed_but_session_meta_unreadable"] = meta_unreadable
    return matches, diagnostics


def selection_privacy() -> "Privacy":
    """Normalizer for selection-time text that reaches a generated artifact.

    Selection runs before any rollout is parsed, so it cannot know the session
    workspace; it still removes the home identity, the Codex state directory
    and the attachment roots, which is what selection strings actually carry.
    """
    privacy = Privacy(normalize=True)
    home = str(Path.home())
    for name in ATTACHMENT_ROOTS:
        privacy.add_literal("attachment_root", os.path.join(home, name),
                            "$ATTACHMENT" if name == "Downloads"
                            else "$%s" % name.upper())
    privacy.add_literal("codex_state", os.path.join(home, ".codex"), "$CODEX_STATE")
    privacy.add_literal("home", home, "~")
    return privacy


def output_path_privacy() -> "Privacy":
    """Narrow normalizer for the Owner's own output paths.

    Only the private identity is removed; `~/...` stays usable as a path, which
    `$ATTACHMENT`/`$WORKSPACE` substitution would not.
    """
    privacy = Privacy(normalize=True)
    privacy.add_literal("home", str(Path.home()), "~")
    return privacy


def scrub_structure(value: Any, privacy: "Privacy") -> Any:
    """Apply the generated-artifact privacy pipeline to every string in a tree."""
    if isinstance(value, str):
        return privacy.clean(value)
    if isinstance(value, dict):
        return {key: scrub_structure(item, privacy)
                for key, item in value.items()}
    if isinstance(value, list):
        return [scrub_structure(item, privacy) for item in value]
    return value


def scan_candidate(path: Path) -> Dict[str, Any]:
    """Sanitized candidate metadata only. Never the conversation body."""
    privacy = Privacy(normalize=True)
    home = str(Path.home())
    privacy.add_literal("home", home, "~")
    meta: Dict[str, Any] = {}
    turns = 0
    completed = 0
    first_owner_preview = None
    unparsable = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            if '"session_meta"' not in line and '"task_' not in line \
                    and '"UserMessage"' not in line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                unparsable += 1
                continue
            payload = as_dict(record.get("payload"))
            kind = record.get("type")
            if kind == "session_meta" and not meta:
                meta = payload
            elif kind == "event_msg":
                ptype = payload.get("type")
                if ptype == "task_started":
                    turns += 1
                elif ptype == "task_complete":
                    completed += 1
                elif ptype == "item_completed" and first_owner_preview is None:
                    item = as_dict(payload.get("item"))
                    if item.get("type") == "UserMessage":
                        text = joined_text(item.get("content"))
                        first_owner_preview = truncate(privacy.clean(text), 120)
    git = as_dict(meta.get("git"))
    cwd = meta.get("cwd")
    if isinstance(cwd, str) and cwd:
        privacy.add_literal("workspace", cwd, "$WORKSPACE")
    return {
        # `_selection` is the internal raw coordinate. It is stripped by
        # public_candidate() and never reaches a generated artifact; identity
        # and filtering must not be decided on normalized display text.
        "_selection": {"rollout_path": str(path), "cwd": cwd},
        "rollout_path_normalized": privacy.normalize_paths(str(path)),
        "source_class": source_class_of(path),
        "session_id": meta.get("session_id") or meta.get("id"),
        "short_session": (meta.get("session_id") or meta.get("id") or "")[:8] or None,
        "started_at": meta.get("timestamp"),
        "completed_at": iso_utc(path.stat().st_mtime),
        "workspace": privacy.normalize_paths(cwd) if isinstance(cwd, str) else None,
        "repo": privacy.repo_identity(git.get("repository_url")),
        "branch": git.get("branch"),
        "turn_count": turns,
        "completed_turn_count": completed,
        "first_owner_message_preview": first_owner_preview,
        "bytes": path.stat().st_size,
        "unparsable_lines": unparsable,
    }


def public_candidate(row: Dict[str, Any]) -> Dict[str, Any]:
    """Candidate row without the internal raw coordinate."""
    return {key: value for key, value in row.items() if not key.startswith("_")}


def same_workspace(candidate_cwd: Optional[str], wanted: Optional[Path]) -> bool:
    """Raw-coordinate workspace identity. Never compares normalized display text."""
    if wanted is None:
        return True
    if not candidate_cwd:
        return False
    try:
        actual = Path(candidate_cwd).expanduser().resolve()
    except (OSError, ValueError, RuntimeError):
        return False
    if actual == wanted:
        return True
    try:
        actual.relative_to(wanted)
    except ValueError:
        return False
    return True


def list_candidates(workspace: Optional[str],
                    since_hours: Optional[int]) -> List[dict]:
    """Sanitized candidate rows. Filtering uses raw paths, output does not."""
    wanted = None
    if workspace:
        wanted = Path(workspace).expanduser().resolve()
    rows = []
    for path in iter_rollouts(since_hours=since_hours):
        row = scan_candidate(path)
        if not same_workspace(row["_selection"]["cwd"], wanted):
            continue
        rows.append(row)
    rows.sort(key=lambda item: (item.get("started_at") or "",
                                item["_selection"]["rollout_path"]))
    return [public_candidate(row) for row in rows]


def _blocked_selection(detail: str, receipt: Dict[str, Any]) -> "ExportBlocked":
    """Selection failure whose receipt is already privacy-normalized."""
    privacy = selection_privacy()
    receipt = dict(receipt)
    receipt["selection_status"] = receipt.get("selection_status",
                                              "BLOCKED_AMBIGUOUS_OR_MISSING")
    return ExportBlocked(STATUS_BLOCKED_SELECTION,
                         privacy.clean(detail),
                         scrub_structure(receipt, privacy))


def resolve_source(
    rollout: Optional[str], session_id: Optional[str]
) -> Path:
    """Exact selection only. Ambiguity, prefixes and identity drift fail closed."""
    requested = None
    if session_id is not None:
        requested = canonical_session_uuid(session_id)
        if requested is None:
            raise _blocked_selection(
                "--session-id must be an exact canonical session UUID; "
                "%r is not one (a prefix is not a session id)" % session_id,
                {"selection_status": "BLOCKED_MALFORMED_SESSION_ID",
                 "requested_session_id_is_canonical_uuid": False},
            )

    if rollout:
        path = Path(rollout).expanduser()
        if not path.is_file():
            raise _blocked_selection(
                "rollout path is not an existing regular file: %s" % rollout,
                {"selection_status": "BLOCKED_AMBIGUOUS_OR_MISSING"},
            )
        if requested is not None:
            identity = session_meta_identity(path)
            if identity != requested:
                raise _blocked_selection(
                    "rollout %s records session_meta.session_id %s, which is not "
                    "the requested %s" % (path, identity or "<unreadable>",
                                          requested),
                    {"selection_status": "BLOCKED_SESSION_IDENTITY_MISMATCH",
                     "requested_session_id": requested,
                     "session_meta_session_id": identity,
                     "rollout": str(path)},
                )
        return path

    if requested is not None:
        matches, diagnostics = rollouts_for_session(requested)
        if len(matches) == 1:
            return matches[0]
        reason = ("BLOCKED_AMBIGUOUS_SESSION_ID" if len(matches) > 1
                  else "BLOCKED_NO_SESSION_META_MATCH")
        receipt = {"selection_status": reason,
                   "requested_session_id": requested,
                   "matched_rollouts": [str(item) for item in matches]}
        receipt.update(diagnostics)
        raise _blocked_selection(
            "session id %s resolved to %d rollouts by session_meta identity; "
            "refusing to guess" % (requested, len(matches)),
            receipt,
        )

    raise _blocked_selection(
        "no exact selector supplied; use --rollout or --session-id",
        {"selection_status": "BLOCKED_AMBIGUOUS_OR_MISSING"},
    )


# --------------------------------------------------------------------------
# Deterministic tool-invocation decoding
# --------------------------------------------------------------------------

JS_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f",
              "\\": "\\", '"': '"', "'": "'", "/": "/", "\n": ""}


def decode_js_string(literal: str) -> Optional[str]:
    """Decode a single-quoted/double-quoted JS string literal, or None."""
    if len(literal) < 2 or literal[0] not in "\"'" or literal[-1] != literal[0]:
        return None
    body = literal[1:-1]
    out: List[str] = []
    index = 0
    while index < len(body):
        char = body[index]
        if char != "\\":
            out.append(char)
            index += 1
            continue
        index += 1
        if index >= len(body):
            return None
        marker = body[index]
        if marker == "u":
            hexpart = body[index + 1:index + 5]
            try:
                out.append(chr(int(hexpart, 16)))
            except ValueError:
                return None
            index += 5
            continue
        if marker == "x":
            hexpart = body[index + 1:index + 3]
            try:
                out.append(chr(int(hexpart, 16)))
            except ValueError:
                return None
            index += 3
            continue
        out.append(JS_ESCAPES.get(marker, marker))
        index += 1
    return "".join(out)


def _read_balanced(text: str, start: int) -> Optional[Tuple[str, int]]:
    """Read one balanced call argument list starting just after '('."""
    depth = 1
    index = start
    quote = None
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in "\"'`":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                return text[start:index], index
        index += 1
    return None


class _JsLiteralError(ValueError):
    """The recorded invocation is not a bounded JS object/array/string literal."""


class _JsLiteralReader:
    """Minimal reader for the JS object literals Codex records as tool input.

    Codex writes tool programs as real JavaScript, so the argument may use
    unquoted keys and backtick template literals in addition to JSON. This
    reader accepts exactly that bounded subset and refuses anything else, so an
    unrecognized program shape becomes a reported `unknown` rather than a guess.
    """

    def __init__(self, text: str):
        self.text = text
        self.pos = 0
        self.template_substitution = False

    def _skip(self) -> None:
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if char in " \t\r\n,":
                self.pos += 1
            elif self.text.startswith("//", self.pos):
                end = self.text.find("\n", self.pos)
                self.pos = len(self.text) if end < 0 else end + 1
            elif self.text.startswith("/*", self.pos):
                end = self.text.find("*/", self.pos)
                if end < 0:
                    raise _JsLiteralError("unterminated comment")
                self.pos = end + 2
            else:
                return

    def read_value(self) -> Any:
        self._skip()
        if self.pos >= len(self.text):
            raise _JsLiteralError("unexpected end of literal")
        char = self.text[self.pos]
        if char == "{":
            return self._read_object()
        if char == "[":
            return self._read_array()
        if char in "\"'":
            return self._read_quoted(char)
        if char == "`":
            return self._read_template()
        return self._read_bare()

    def _read_object(self) -> Dict[str, Any]:
        self.pos += 1
        out: Dict[str, Any] = {}
        while True:
            self._skip()
            if self.pos >= len(self.text):
                raise _JsLiteralError("unterminated object")
            if self.text[self.pos] == "}":
                self.pos += 1
                return out
            key = self._read_key()
            self._skip()
            if self.pos >= len(self.text) or self.text[self.pos] != ":":
                raise _JsLiteralError("expected ':' after key %r" % key)
            self.pos += 1
            out[key] = self.read_value()

    def _read_array(self) -> List[Any]:
        self.pos += 1
        out: List[Any] = []
        while True:
            self._skip()
            if self.pos >= len(self.text):
                raise _JsLiteralError("unterminated array")
            if self.text[self.pos] == "]":
                self.pos += 1
                return out
            out.append(self.read_value())

    def _read_key(self) -> str:
        char = self.text[self.pos]
        if char in "\"'":
            return self._read_quoted(char)
        match = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*").match(self.text, self.pos)
        if not match:
            raise _JsLiteralError("unrecognized object key")
        self.pos = match.end()
        return match.group(0)

    def _read_quoted(self, quote: str) -> str:
        end = self.pos + 1
        while end < len(self.text):
            if self.text[end] == "\\":
                end += 2
                continue
            if self.text[end] == quote:
                literal = self.text[self.pos:end + 1]
                self.pos = end + 1
                decoded = decode_js_string(literal)
                if decoded is None:
                    raise _JsLiteralError("undecodable string literal")
                return decoded
            end += 1
        raise _JsLiteralError("unterminated string")

    def _read_template(self) -> str:
        end = self.pos + 1
        out: List[str] = []
        while end < len(self.text):
            char = self.text[end]
            if char == "\\":
                nxt = self.text[end + 1:end + 2]
                out.append(JS_ESCAPES.get(nxt, nxt))
                end += 2
                continue
            if char == "`":
                self.pos = end + 1
                return "".join(out)
            if char == "$" and self.text[end + 1:end + 2] == "{":
                self.template_substitution = True
            out.append(char)
            end += 1
        raise _JsLiteralError("unterminated template literal")

    def _read_bare(self) -> Any:
        match = re.compile(r"-?[0-9][0-9_]*(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?"
                           r"|true|false|null|undefined").match(self.text, self.pos)
        if not match:
            raise _JsLiteralError("unrecognized literal value")
        token = match.group(0)
        self.pos = match.end()
        if token == "true":
            return True
        if token == "false":
            return False
        if token in ("null", "undefined"):
            return None
        try:
            return int(token.replace("_", ""))
        except ValueError:
            return float(token.replace("_", ""))


def parse_js_literal(text: str) -> Tuple[Any, bool]:
    """Parse one JS literal. Returns (value, template_substitution_present)."""
    reader = _JsLiteralReader(text)
    value = reader.read_value()
    reader._skip()
    if reader.pos != len(text):
        raise _JsLiteralError("trailing content after literal")
    return value, reader.template_substitution


def parse_tool_invocation(text: str) -> Dict[str, Any]:
    """Decode `const r = await tools.X({...})` style tool programs.

    Returns a dict with `tool`, `args` (dict when the argument was a JSON
    object), `literal` (decoded string argument) and `decoded` (bool). This is
    structural decoding of the recorded invocation, never interpretation.
    """
    result: Dict[str, Any] = {"tool": None, "args": None, "literal": None,
                              "decoded": False, "template_substitution": False}
    if not text:
        return result
    match = re.search(r"tools\.([A-Za-z_][A-Za-z0-9_]*)\s*\(", text)
    if not match:
        return result
    result["tool"] = match.group(1)
    balanced = _read_balanced(text, match.end())
    if balanced is None:
        return result
    raw = balanced[0].strip()
    if raw.startswith("{"):
        try:
            parsed, substitution = parse_js_literal(raw)
        except (_JsLiteralError, ValueError, RecursionError):
            return result
        if isinstance(parsed, dict):
            result["args"] = parsed
            result["decoded"] = True
            result["template_substitution"] = substitution
        return result
    if raw[:1] in "\"'`":
        try:
            literal, substitution = parse_js_literal(raw)
        except (_JsLiteralError, ValueError):
            literal, substitution = None, False
        if isinstance(literal, str):
            result["literal"] = literal
            result["decoded"] = True
            result["template_substitution"] = substitution
        return result
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", raw):
        assign = re.search(
            r"(?:const|let|var)\s+%s\s*=\s*(\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')"
            % re.escape(raw),
            text,
            re.S,
        )
        if assign:
            literal = decode_js_string(assign.group(1))
            if literal is not None:
                result["literal"] = literal
                result["decoded"] = True
    return result


STDIN_PREVIEW_CHARS = 60


def decode_stdin_chars(args: Dict[str, Any],
                       template_substitution: bool) -> "tuple":
    """What a `write_stdin` actually sent, and whether that is proven.

    Only a decoded string proves what was typed into the running process.
    A missing field, a non-string, or an object assembled by template
    substitution is unproven — and unproven stdin is never presented as an
    empty poll, because a hidden `y`, `git commit -m x` or Ctrl-C is a real
    action the timeline would be failing to report.

    Returns `(chars, state)` where state is EMPTY, NON_EMPTY or UNKNOWN.
    """
    if template_substitution or "chars" not in args:
        return None, "UNKNOWN"
    value = args.get("chars")
    if not isinstance(value, str):
        return None, "UNKNOWN"
    return value, "EMPTY" if value == "" else "NON_EMPTY"


def stdin_preview(chars: str, privacy: "Privacy") -> str:
    """One-line, redacted, escaped, bounded rendering of sent input.

    Redaction runs first, so a credential typed into a prompt is handled by the
    same rules as everywhere else. Control characters are then escaped, which
    both keeps a Ctrl-C visible as `\\x03` and stops a raw newline from
    breaking the one-line timeline row.
    """
    escaped = privacy.clean(chars).encode("unicode_escape").decode("ascii")
    return truncate(escaped.replace('"', '\\"'), STDIN_PREVIEW_CHARS)


PATCH_OP_RE = re.compile(
    r"^\*\*\* (Add File|Update File|Delete File|Move to): (.+)$", re.M
)


def parse_patch_ops(patch_text: str) -> List[Dict[str, str]]:
    ops = []
    for kind, target in PATCH_OP_RE.findall(patch_text or ""):
        ops.append({"op": kind.replace(" File", "").lower(), "path": target.strip()})
    return ops


OUTPUT_HEADER_RE = re.compile(
    r"^(?P<status>[A-Za-z][A-Za-z ]*)\nWall time (?P<wall>[0-9.]+) seconds\nOutput:\n?\Z"
)


def split_tool_output(output: Any) -> Dict[str, Any]:
    """Separate the recorded harness header from the actual tool output body."""
    result = {"harness_status": None, "wall_time_seconds": None, "body": "",
              "header_present": False}
    if isinstance(output, str):
        result["body"] = output
        return result
    blocks = as_list(output)
    texts = [block.get("text", "") if isinstance(block, dict) else str(block)
             for block in blocks]
    if texts:
        header = OUTPUT_HEADER_RE.match(texts[0])
        if header:
            result["harness_status"] = header.group("status").strip()
            try:
                result["wall_time_seconds"] = float(header.group("wall"))
            except ValueError:
                result["wall_time_seconds"] = None
            result["header_present"] = True
            result["body"] = "".join(texts[1:])
            return result
    result["body"] = "".join(texts)
    return result


# --------------------------------------------------------------------------
# Command shape analysis (faithful-read detection only)
# --------------------------------------------------------------------------

SHELL_META_RE = re.compile(r"[|><;&`$(){}*?\[\]]")

READ_PROGRAMS = frozenset(
    ("cat", "sed", "head", "tail", "nl", "bat", "less", "more", "od", "xxd")
)


def _tokens(segment: str) -> List[str]:
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


# Nothing above decides folding. v1 folds no shell command at all, so there is
# no safe-read allowlist, no mutation blacklist and no shell grammar to keep
# correct. `SHELL_META_RE`, `READ_PROGRAMS` and `_tokens` exist only for the
# attachment faithful-read feature below, which recovers the text of a file the
# session actually printed — a separate question from how a row is presented.


def faithful_read_target(command: str) -> Optional[str]:
    """Path of a whole-file/whole-range read whose output is the file text.

    Returns None whenever the recorded program does anything else (pipes,
    redirection, filters, multiple operands), so recovered attachment text is
    never confused with filtered output.
    """
    lines = [line for line in (command or "").splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    line = lines[0].strip()
    if SHELL_META_RE.search(line):
        return None
    tokens = _tokens(line)
    if not tokens:
        return None
    if os.path.basename(tokens[0]) not in READ_PROGRAMS:
        return None
    operands = []
    skip_next = False
    for token in tokens[1:]:
        if skip_next:
            skip_next = False
            continue
        if token.startswith("-"):
            # `sed -n` style flags take no operand; `head -n 5` does.
            if re.fullmatch(r"-[nc]", token):
                skip_next = os.path.basename(tokens[0]) in ("head", "tail")
            continue
        operands.append(token)
    # `sed -n '1,260p' FILE` keeps the script as the first operand.
    if os.path.basename(tokens[0]) == "sed" and len(operands) == 2:
        if re.fullmatch(r"[0-9,$]+[a-z]", operands[0]):
            operands = operands[1:]
    if len(operands) != 1:
        return None
    return operands[0]


# --------------------------------------------------------------------------
# Rollout model
# --------------------------------------------------------------------------


class RolloutModel:
    """Everything mechanically derivable from one rollout file.

    The model separates what the provider actually persisted from what the
    exporter chose to render. Nothing here paraphrases, summarizes or infers.
    """

    def __init__(self, privacy: Privacy):
        self.privacy = privacy
        self.session_meta: Dict[str, Any] = {}
        self.turn_contexts: "OrderedDict[str, dict]" = OrderedDict()
        self.turn_order: List[str] = []
        self.turn_started: Dict[str, dict] = {}
        self.turn_completed: Dict[str, dict] = {}
        self.turn_aborted: Dict[str, dict] = {}
        self.unbound_turn_aborts: List[dict] = []
        self.turn_outcome_conflicts: Dict[str, dict] = {}
        self.message_turn_bindings: Dict[str, set] = {}
        self.thread_settings: Dict[str, Any] = {}

        self.owner_messages: List[dict] = []
        self.attachment_wrapper_contexts: List[dict] = []
        self.attachment_envelopes_separated = 0
        self.attachment_wrappers_unparsed = 0
        self.persisted_user_context: List[dict] = []
        self.developer_messages: List[dict] = []
        self.assistant_messages: List[dict] = []
        self.reasoning_records: List[dict] = []
        self.tool_actions: List[dict] = []
        self.file_changes: List[dict] = []
        self.context_compactions: List[dict] = []
        self.command_executions: List[dict] = []
        self.mcp_tool_calls_recognized = 0

        self.token_usage: Dict[str, Any] = {}
        self.record_type_counts: Counter = Counter()
        self.payload_type_counts: Counter = Counter()
        self.item_type_counts: Counter = Counter()
        self.unknown_record_type_counts: Counter = Counter()
        self.unknown_payload_type_counts: Counter = Counter()
        self.unknown_item_type_counts: Counter = Counter()
        self.duplicate_breakdown: Counter = Counter()
        self.unparsable_lines = 0
        self.unbound_records = 0
        self.total_lines = 0
        self.warnings: List[str] = []

        self._seen_message_text: Dict[Tuple[Optional[str], str], str] = {}
        self._seen_message_texts: Dict[str, str] = {}

    # -- turn bookkeeping --------------------------------------------------

    def _touch_turn(self, turn_id: Optional[str]) -> Optional[str]:
        if not isinstance(turn_id, str) or not turn_id:
            self.unbound_records += 1
            return None
        if turn_id not in self.turn_order:
            self.turn_order.append(turn_id)
        return turn_id

    def turn_index(self, turn_id: Optional[str]) -> Optional[int]:
        if turn_id in self.turn_order:
            return self.turn_order.index(turn_id) + 1
        return None

    # -- ingestion ---------------------------------------------------------

    def load(self, path: Path) -> None:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for index, raw in enumerate(handle):
                stripped = raw.strip()
                if not stripped:
                    continue
                self.total_lines += 1
                try:
                    record = json.loads(stripped)
                except ValueError:
                    self.unparsable_lines += 1
                    continue
                if not isinstance(record, dict):
                    self.unparsable_lines += 1
                    continue
                self._ingest(index, record)
        self._finalize()

    def _ingest(self, seq: int, record: dict) -> None:
        kind = record.get("type")
        payload = as_dict(record.get("payload"))
        raw_turn_id = turn_id_of(payload)
        if isinstance(raw_turn_id, str) and raw_turn_id:
            # Identity chronology survives unknown/aborted/partial payloads;
            # this does not claim support for their content schemas.
            self._touch_turn(raw_turn_id)
        self.record_type_counts[str(kind)] += 1
        if kind not in KNOWN_RECORD_TYPES:
            self.unknown_record_type_counts[str(kind)] += 1
            return

        ptype = payload.get("type")
        if kind in UNTYPED_PAYLOAD_RECORDS:
            self.payload_type_counts["%s/-" % kind] += 1
        else:
            self.payload_type_counts["%s/%s" % (kind, ptype)] += 1

        handler = {
            "session_meta": self._on_session_meta,
            "turn_context": self._on_turn_context,
            "world_state": self._on_world_state,
            "compacted": self._on_compacted,
            "response_item": self._on_response_item,
            "event_msg": self._on_event_msg,
        }[kind]
        handler(seq, record, payload)

    def _on_session_meta(self, seq: int, record: dict, payload: dict) -> None:
        if not self.session_meta:
            self.session_meta = payload
        base = as_dict(payload.get("base_instructions"))
        if base.get("text"):
            self.privacy.note_drop("base_instructions_text")
        if payload.get("dynamic_tools"):
            self.privacy.note_drop(
                "dynamic_tool_schemas", len(as_list(payload.get("dynamic_tools")))
            )

    def _on_turn_context(self, seq: int, record: dict, payload: dict) -> None:
        turn_id = self._touch_turn(payload.get("turn_id"))
        if turn_id:
            self.turn_contexts[turn_id] = payload

    def _on_world_state(self, seq: int, record: dict, payload: dict) -> None:
        state = as_dict(payload.get("state"))
        for key in ("agents_md", "host_skills", "environments", "collaboration_mode"):
            if key in state:
                self.privacy.note_drop("world_state_%s" % key)

    def _on_compacted(self, seq: int, record: dict, payload: dict) -> None:
        self.context_compactions.append(
            {
                "seq": seq,
                "timestamp": record.get("timestamp"),
                "window_number": payload.get("window_number"),
                "window_id": payload.get("window_id"),
                "replacement_history_count": len(
                    as_list(payload.get("replacement_history"))
                ),
            }
        )
        for entry in as_list(payload.get("replacement_history")):
            entry = as_dict(entry)
            role = entry.get("role")
            if role not in ("user", "developer"):
                continue
            text = joined_text(entry.get("content"))
            if not text:
                continue
            if text in self._seen_message_texts:
                self.duplicate_breakdown["compaction_exact_message_snapshot"] += 1
            else:
                self.duplicate_breakdown["compaction_message_snapshot_unmatched"] += 1

    # -- response items ----------------------------------------------------

    def _on_response_item(self, seq: int, record: dict, payload: dict) -> None:
        ptype = payload.get("type")
        if ptype not in KNOWN_RESPONSE_ITEM_TYPES:
            self.unknown_payload_type_counts["response_item/%s" % ptype] += 1
            return
        turn_id = self._touch_turn(turn_id_of(payload))
        timestamp = record.get("timestamp")
        if ptype == "message":
            self._on_message(seq, timestamp, turn_id, payload)
        elif ptype == "reasoning":
            self._on_reasoning(seq, timestamp, turn_id, payload)
        elif ptype in ("custom_tool_call", "function_call"):
            self._on_tool_call(seq, timestamp, turn_id, payload, ptype)
        elif ptype in ("custom_tool_call_output", "function_call_output"):
            self._on_tool_output(seq, timestamp, turn_id, payload, ptype)

    def _on_message(self, seq: int, timestamp, turn_id, payload: dict) -> None:
        role = payload.get("role")
        text = joined_text(payload.get("content"))
        entry = {
            "seq": seq,
            "timestamp": timestamp,
            "turn_id": turn_id,
            "id": payload.get("id"),
            "role": role,
            "phase": payload.get("phase"),
            "text": text,
            "blocks": len(as_list(payload.get("content"))),
            "bytes": len(text.encode("utf-8")),
        }
        if role == "assistant":
            # Provider IDs are global evidence: a later missing-turn final
            # must see conflicts from direct responses as well as mirrors.
            if isinstance(entry["id"], str) and turn_id:
                self.message_turn_bindings.setdefault(entry["id"], set()).add(turn_id)
            self.assistant_messages.append(entry)
            self._seen_message_text[(turn_id, text)] = "assistant"
            self._seen_message_texts.setdefault(text, "assistant")
        elif role == "user":
            # Ownership is decided later, by exact mirror against typed
            # UserMessage items. Until then this is only persisted user-role text.
            self.persisted_user_context.append(entry)
        elif role == "developer":
            self.developer_messages.append(entry)
            self._seen_message_text[(turn_id, text)] = "developer"
            self._seen_message_texts.setdefault(text, "developer")
            self.privacy.note_drop("developer_message")
        else:
            self.unknown_payload_type_counts["response_item/message#role=%s" % role] += 1

    def _on_reasoning(self, seq: int, timestamp, turn_id, payload: dict) -> None:
        summary = [
            block.get("text", "")
            for block in as_list(payload.get("summary"))
            if isinstance(block, dict) and block.get("text")
        ]
        self.reasoning_records.append(
            {
                "seq": seq,
                "timestamp": timestamp,
                "turn_id": turn_id,
                "id": payload.get("id"),
                "summary": summary,
                "opaque": bool(payload.get("encrypted_content")),
                "source": "response_item",
            }
        )

    def _on_tool_call(self, seq: int, timestamp, turn_id, payload: dict,
                      ptype: str) -> None:
        name = payload.get("name")
        raw_program = payload.get("input")
        if raw_program is None and payload.get("arguments") is not None:
            raw_program = payload.get("arguments")
        action: Dict[str, Any] = {
            "seq": seq,
            "timestamp": timestamp,
            "turn_id": turn_id,
            "call_id": payload.get("call_id"),
            "tool_name": name,
            "record_type": ptype,
            "kind": "tool",
            "command": None,
            "workdir": None,
            "patch_ops": [],
            "stdin_session": None,
            "stdin_chars": None,
            "stdin_chars_state": "UNKNOWN",
            "decoded": False,
            "invocation_template_substitution": False,
            "wall_time_seconds": None,
            "harness_status": None,
            "exit_code": None,
            "exec_status": None,
            "exit_source": "unresolved",
            "output_body": "",
            "output_present": False,
        }
        text = raw_program if isinstance(raw_program, str) else ""
        parsed = parse_tool_invocation(text)
        if parsed["tool"]:
            action["decoded"] = parsed["decoded"]
            action["invocation_template_substitution"] = \
                parsed["template_substitution"]
            action["inner_tool"] = parsed["tool"]
            args = as_dict(parsed["args"])
            if parsed["tool"] == "exec_command" and args.get("cmd") is not None:
                action["command"] = str(args.get("cmd"))
                action["workdir"] = args.get("workdir")
                # A JS template substitution is invocation provenance, not
                # proof of the concrete command the provider eventually ran.
                # Keep it visible, but never pair it to execution metadata.
                action["kind"] = (
                    "command_invocation"
                    if parsed["template_substitution"] else "command"
                )
            elif parsed["tool"] == "write_stdin":
                action["kind"] = "stdin"
                action["stdin_session"] = args.get("session_id")
                chars, state = decode_stdin_chars(
                    args, parsed["template_substitution"])
                action["stdin_chars"] = chars
                action["stdin_chars_state"] = state
            elif parsed["tool"] == "apply_patch" and parsed["literal"]:
                action["kind"] = "patch"
                action["patch_ops"] = parse_patch_ops(parsed["literal"])
            elif args.get("cmd") is not None:
                action["kind"] = "command"
                action["command"] = str(args.get("cmd"))
        elif text.strip().startswith("{"):
            try:
                args = json.loads(text)
            except ValueError:
                args = {}
            if isinstance(args, dict):
                action["decoded"] = True
                action["call_args"] = {
                    key: value for key, value in args.items()
                    if isinstance(value, (str, int, float, bool))
                }
                if isinstance(args.get("cmd"), str):
                    action["kind"] = "command"
                    action["command"] = args["cmd"]
        if name == "wait" and action["kind"] == "tool":
            action["kind"] = "wait"
        self.tool_actions.append(action)

    def _on_tool_output(self, seq: int, timestamp, turn_id, payload: dict,
                        ptype: str) -> None:
        call_id = payload.get("call_id")
        parsed = split_tool_output(payload.get("output"))
        for action in reversed(self.tool_actions):
            if action["call_id"] == call_id and not action["output_present"]:
                action["output_present"] = True
                action["output_body"] = parsed["body"]
                action["harness_status"] = parsed["harness_status"]
                action["wall_time_seconds"] = parsed["wall_time_seconds"]
                action["output_seq"] = seq
                return
        self.unbound_records += 1
        self.warnings.append("tool output %s has no matching call" % call_id)

    # -- events ------------------------------------------------------------

    def _on_event_msg(self, seq: int, record: dict, payload: dict) -> None:
        ptype = payload.get("type")
        if ptype not in KNOWN_EVENT_MSG_TYPES:
            self.unknown_payload_type_counts["event_msg/%s" % ptype] += 1
            return
        timestamp = record.get("timestamp")
        if ptype == "task_started":
            turn_id = self._touch_turn(payload.get("turn_id"))
            if turn_id:
                self.turn_started[turn_id] = {"seq": seq, "timestamp": timestamp,
                                              "started_at": payload.get("started_at")}
        elif ptype == "task_complete":
            turn_id = self._touch_turn(payload.get("turn_id"))
            if turn_id:
                self.turn_completed[turn_id] = {
                    "seq": seq,
                    "timestamp": timestamp,
                    "completed_at": payload.get("completed_at"),
                    "duration_ms": payload.get("duration_ms"),
                    "time_to_first_token_ms": payload.get("time_to_first_token_ms"),
                }
        elif ptype == "token_count":
            info = as_dict(payload.get("info"))
            total = as_dict(info.get("total_token_usage"))
            if total:
                self.token_usage = {
                    "input_tokens": total.get("input_tokens"),
                    "cached_input_tokens": total.get("cached_input_tokens"),
                    "output_tokens": total.get("output_tokens"),
                    "reasoning_output_tokens": total.get("reasoning_output_tokens"),
                    "total_tokens": total.get("total_tokens"),
                    "model_context_window": info.get("model_context_window"),
                }
            if payload.get("rate_limits") is not None:
                self.privacy.note_drop("rate_limit_and_credit_telemetry")
            for key in ACCOUNT_IDENTITY_KEYS:
                if key in payload:
                    self.privacy.note_drop("account_identity")
        elif ptype == "thread_settings_applied":
            settings = as_dict(payload.get("thread_settings"))
            self.thread_settings = {
                "model": settings.get("model"),
                "reasoning_effort": settings.get("reasoning_effort"),
                "reasoning_summary": settings.get("reasoning_summary"),
                "approval_policy": settings.get("approval_policy"),
                "personality": settings.get("personality"),
            }
        elif ptype == "item_completed":
            self._on_item_completed(seq, timestamp, payload)
        elif ptype == "turn_aborted":
            self._on_turn_aborted(seq, timestamp, payload)

    def _on_turn_aborted(self, seq: int, timestamp, payload: dict) -> None:
        raw_turn_id = payload.get("turn_id")
        turn_id_present = raw_turn_id is not None
        turn_id_valid = not turn_id_present or bounded_identifier(raw_turn_id)
        timing_valid = all(
            payload.get(key) is None
            or (type(payload.get(key)) is int and 0 <= payload[key] < 2 ** 63)
            for key in TURN_ABORT_TIMING_KEYS
        )
        if not turn_id_valid or not timing_valid:
            self.unknown_payload_type_counts["event_msg/turn_aborted#invalid"] += 1
            return
        reason = payload.get("reason")
        if not exact_wire(reason, TURN_ABORT_REASONS):
            self.unknown_payload_type_counts["event_msg/turn_aborted#unsupported_reason"] += 1
            return
        detail = {"seq": seq, "timestamp": timestamp, "reason": reason}
        if not turn_id_present:
            self.unbound_turn_aborts.append(detail)
            self.unbound_records += 1
            self.warnings.append("turn_aborted carries no turn id; the abort is counted but bound to no turn")
            return
        self.turn_aborted.setdefault(self._touch_turn(raw_turn_id), detail)

    def _on_item_completed(self, seq: int, timestamp, payload: dict) -> None:
        item = as_dict(payload.get("item"))
        itype = item.get("type")
        turn_id = self._touch_turn(payload.get("turn_id"))
        self.item_type_counts[str(itype)] += 1
        if itype not in KNOWN_ITEM_TYPES:
            self.unknown_item_type_counts[str(itype)] += 1
            self.unknown_payload_type_counts[
                "event_msg/item_completed/%s" % itype
            ] += 1
            return
        if itype == "McpToolCall":
            self._on_mcp_tool_call(item)
            return
        started = payload.get("started_at_ms")
        completed = payload.get("completed_at_ms")
        if itype == "UserMessage":
            text = joined_text(item.get("content"))
            self.owner_messages.append(
                {
                    "seq": seq,
                    "timestamp": timestamp,
                    "turn_id": turn_id,
                    "id": item.get("id"),
                    "text": text,
                    "bytes": len(text.encode("utf-8")),
                }
            )
            self._seen_message_text[(turn_id, text)] = "owner"
            self._seen_message_texts.setdefault(text, "owner")
        elif itype == "AgentMessage":
            if isinstance(item.get("id"), str) and turn_id:
                self.message_turn_bindings.setdefault(item["id"], set()).add(turn_id)
            self.duplicate_breakdown["assistant_event_response_id_match"] += 1
        elif itype == "Reasoning":
            self.duplicate_breakdown["reasoning_event_response_id_match"] += 1
        elif itype == "CommandExecution":
            command = item.get("command")
            command_text = command[-1] if isinstance(command, list) and command else ""
            duration = as_dict(item.get("duration"))
            self.command_executions.append(
                {
                    "seq": seq,
                    "timestamp": timestamp,
                    "turn_id": turn_id,
                    "id": item.get("id"),
                    "command": command_text,
                    "cwd": item.get("cwd"),
                    "status": item.get("status"),
                    "exit_code": item.get("exit_code"),
                    "duration_seconds": duration.get("secs"),
                    "stderr": item.get("stderr") or "",
                    "consumed": False,
                    "started_at_ms": started,
                    "completed_at_ms": completed,
                }
            )
        elif itype == "FileChange":
            changes = as_dict(item.get("changes"))
            entries = []
            for path, change in sorted(changes.items()):
                change = as_dict(change)
                entries.append({"path": path, "change_type": change.get("type")})
                if change.get("content") is not None:
                    self.privacy.note_drop("file_change_content")
                if change.get("unified_diff") is not None:
                    self.privacy.note_drop("file_change_unified_diff")
            self.file_changes.append(
                {
                    "seq": seq,
                    "timestamp": timestamp,
                    "turn_id": turn_id,
                    "id": item.get("id"),
                    "status": item.get("status"),
                    "changes": entries,
                }
            )
        elif itype == "ContextCompaction":
            self.context_compactions.append(
                {"seq": seq, "timestamp": timestamp, "turn_id": turn_id,
                 "window_number": None, "window_id": item.get("id"),
                 "replacement_history_count": None}
            )

    def _on_mcp_tool_call(self, item: dict) -> None:
        for key, category in MCP_TOOL_CALL_PRIVATE_PAYLOAD_KEYS:
            if item.get(key) is not None:
                self.privacy.note_drop(category)
        if any(key in item for key in MCP_TOOL_CALL_CONNECTOR_METADATA_KEYS):
            self.privacy.note_drop("mcp_tool_call_connector_metadata")
        if not all(bounded_identifier(item.get(key)) for key in ("id", "server", "tool")):
            self.unknown_payload_type_counts["event_msg/item_completed/McpToolCall#invalid"] += 1
            return
        status = item.get("status")
        if exact_wire(status, MCP_TOOL_CALL_NON_TERMINAL_STATUSES):
            self.unknown_payload_type_counts["event_msg/item_completed/McpToolCall#non_terminal_status"] += 1
            return
        if not exact_wire(status, MCP_TOOL_CALL_TERMINAL_STATUSES):
            self.unknown_payload_type_counts["event_msg/item_completed/McpToolCall#unsupported_status"] += 1
            return
        self.mcp_tool_calls_recognized += 1

    # -- post processing ---------------------------------------------------

    def _finalize(self) -> None:
        self._resolve_turn_terminal_outcomes()
        self._classify_user_records()
        self._separate_attachment_envelopes()
        self._pair_command_executions()
        self._pair_file_changes()

    def _resolve_turn_terminal_outcomes(self) -> None:
        for turn_id in self.turn_order:
            if turn_id not in self.turn_aborted or turn_id not in self.turn_completed:
                continue
            del self.turn_completed[turn_id]
            self.turn_outcome_conflicts[turn_id] = {
                "turn_id": turn_id,
                "resolved_outcome": "aborted",
                "abort_reason": self.turn_aborted[turn_id]["reason"],
            }
            self.warnings.append(
                "turn %s records both task_complete and a valid turn_aborted; the abort is the terminal outcome, so the turn is not counted as completed" % turn_id
            )

    def _separate_attachment_envelopes(self) -> None:
        """Keep Codex-composed attachment wrappers out of the Owner's words.

        Runs after duplicate classification so the response_item mirror of a
        wrapper stays a duplicate rather than becoming a second inventory row.
        """
        kept: List[dict] = []
        for message in self.owner_messages:
            envelope = parse_attachment_envelope(message["text"])
            if envelope is not None:
                message["owner_text"] = envelope["owner_request_text"]
                message["attachment_envelope"] = {
                    "manifest_entry_count": envelope["manifest_entry_count"],
                    "envelope_bytes": envelope["envelope_bytes"],
                    "owner_request_bytes": envelope["owner_request_bytes"],
                }
                self.attachment_envelopes_separated += 1
                self.privacy.note_drop("codex_composed_attachment_envelope")
                kept.append(message)
                continue
            if message["text"].lstrip().startswith(FILES_MANIFEST_HEADER):
                # Wrapper-shaped but not mechanically separable. Refuse to
                # claim any part of it was typed by the Owner.
                message["classification"] = \
                    "persisted_user_context_unparsed_attachment_wrapper"
                message["envelope_tags"] = sorted(set(
                    re.findall(r"<([a-z_]+)>", message["text"][:4000])
                ))
                self.attachment_wrappers_unparsed += 1
                self.attachment_wrapper_contexts.append(message)
                self.persisted_user_context_visible.append(message)
                self.privacy.note_drop("unparsed_attachment_wrapper")
                continue
            message["owner_text"] = message["text"]
            message["attachment_envelope"] = None
            kept.append(message)
        self.owner_messages = kept

    def _classify_user_records(self) -> None:
        """Decide, mechanically, which persisted user-role text the Owner typed."""
        owner_index: Dict[Tuple[Optional[str], str], int] = {}
        for message in self.owner_messages:
            owner_index[(message["turn_id"], message["text"])] = 1
        remaining = []
        for entry in self.persisted_user_context:
            key = (entry["turn_id"], entry["text"])
            if key in owner_index:
                entry["classification"] = "owner_message_duplicate_representation"
                self.duplicate_breakdown["user_event_exact_text_mirror"] += 1
            else:
                entry["classification"] = "persisted_user_context"
                entry["envelope_tags"] = sorted(
                    set(re.findall(r"<([a-z_]+)>", entry["text"][:4000]))
                )
                self.privacy.note_drop("injected_user_context_envelope")
                remaining.append(entry)
        self.persisted_user_context_visible = remaining

    def _pair_file_changes(self) -> None:
        """Exact path-set pairing between apply_patch calls and FileChange items."""
        buckets: Dict[Tuple[Optional[str], Tuple[str, ...]], List[dict]] = {}
        for change in self.file_changes:
            key = (change["turn_id"],
                   tuple(sorted(entry["path"] for entry in change["changes"])))
            buckets.setdefault(key, []).append(change)
        for action in self.tool_actions:
            if action["kind"] != "patch" or not action["patch_ops"]:
                continue
            key = (action["turn_id"],
                   tuple(sorted(op["path"] for op in action["patch_ops"])))
            bucket = buckets.get(key)
            match = None
            for change in bucket or []:
                if not change.get("consumed"):
                    match = change
                    break
            if match is None:
                continue
            match["consumed"] = True
            action["exec_status"] = match["status"]
            action["exit_source"] = "file_change_exact_path_match"

    def _pair_command_executions(self) -> None:
        """Exact command-string pairing inside the same turn. Never fuzzy."""
        buckets: Dict[Tuple[Optional[str], str], List[dict]] = {}
        for execution in self.command_executions:
            buckets.setdefault((execution["turn_id"], execution["command"]), []) \
                .append(execution)
        for action in self.tool_actions:
            if action["kind"] != "command" or not action["command"]:
                continue
            bucket = buckets.get((action["turn_id"], action["command"]))
            match = None
            if bucket:
                for execution in bucket:
                    if not execution["consumed"]:
                        match = execution
                        break
            if match is None:
                action["exit_source"] = "unresolved"
                continue
            match["consumed"] = True
            action["exit_code"] = match["exit_code"]
            action["exec_status"] = match["status"]
            action["exit_source"] = "command_execution_exact_match"
            action["cwd"] = match["cwd"]
            action["stderr"] = match["stderr"]
            action["duration_seconds"] = match["duration_seconds"]


# --------------------------------------------------------------------------
# Visible-reasoning selection (deterministic, structural, no model involved)
# --------------------------------------------------------------------------

REASONING_ANCHORS = (
    "turn_start",
    "owner_message",
    "tool_failure",
    "final_answer",
)


def select_visible_reasoning(model: RolloutModel, cap: int) -> Dict[str, Any]:
    """Keep provider-visible summaries around mechanically observable decision points.

    Anchors are structural facts already present in the rollout: turn starts,
    Owner message boundaries, failed tool actions and final answers. Selection
    never reads meaning out of the summary text.
    """
    visible = [record for record in model.reasoning_records if record["summary"]]
    opaque_only = [record for record in model.reasoning_records
                   if not record["summary"] and record["opaque"]]
    source_blocks = sum(len(record["summary"]) for record in visible)

    # Every anchor carries the turn it belongs to. Selection is turn-local:
    # an anchor in turn N may only select reasoning recorded in turn N, so a
    # summary never inherits the provenance of a different turn's event.
    anchors: List[Tuple[str, int, Optional[str]]] = []
    for turn_id in model.turn_order:
        started = model.turn_started.get(turn_id)
        if started:
            anchors.append(("turn_start", started["seq"], turn_id))
    for message in model.owner_messages:
        anchors.append(("owner_message", message["seq"], message["turn_id"]))
    for action in model.tool_actions:
        failed = (
            (action.get("exit_code") is not None and action.get("exit_code") != 0)
            or action.get("exec_status") == "failed"
            or (action.get("harness_status")
                and action["harness_status"].lower().startswith("script failed"))
        )
        if failed:
            anchors.append(("tool_failure", action["seq"], action["turn_id"]))
    for message in model.assistant_messages:
        if message.get("phase") == "final_answer":
            anchors.append(("final_answer", message["seq"], message["turn_id"]))

    positions_by_turn: Dict[Optional[str], List[int]] = {}
    for record in visible:
        positions_by_turn.setdefault(record["turn_id"], []).append(record["seq"])
    reasons: Dict[int, set] = {}
    cross_turn_anchors_dropped = 0

    def mark(seq: Optional[int], why: str) -> None:
        if seq is None:
            return
        reasons.setdefault(seq, set()).add(why)

    def first_after(seq: int, turn_id: Optional[str]) -> Optional[int]:
        for position in positions_by_turn.get(turn_id, ()):
            if position > seq:
                return position
        return None

    def last_before(seq: int, turn_id: Optional[str]) -> Optional[int]:
        chosen = None
        for position in positions_by_turn.get(turn_id, ()):
            if position < seq:
                chosen = position
            else:
                break
        return chosen

    for kind, seq, turn_id in anchors:
        if turn_id not in positions_by_turn:
            # No visible reasoning in this anchor's own turn. Reaching into a
            # neighbouring turn would attach false provenance, so nothing is
            # selected for this anchor at all.
            cross_turn_anchors_dropped += 1
            continue
        if kind == "turn_start":
            mark(first_after(seq, turn_id), "turn_start")
        elif kind == "owner_message":
            mark(first_after(seq, turn_id), "owner_message_boundary")
        elif kind == "tool_failure":
            mark(last_before(seq, turn_id), "pre_failure_decision")
            mark(first_after(seq, turn_id), "post_failure_recovery")
        elif kind == "final_answer":
            mark(last_before(seq, turn_id), "pre_final_decision")

    selected = []
    suppressed_repeats = 0
    previous_texts: List[str] = []
    for record in visible:
        if record["seq"] not in reasons:
            continue
        texts = []
        for text in record["summary"]:
            if previous_texts and text == previous_texts[-1]:
                suppressed_repeats += 1
                continue
            texts.append(text)
            previous_texts.append(text)
        if not texts:
            continue
        selected.append(
            {
                "seq": record["seq"],
                "timestamp": record["timestamp"],
                "turn_id": record["turn_id"],
                "id": record["id"],
                "reasons": sorted(reasons[record["seq"]]),
                "texts": texts,
            }
        )

    capped_out = 0
    if cap and len(selected) > cap:
        capped_out = len(selected) - cap
        selected = selected[:cap]

    selected_blocks = sum(len(item["texts"]) for item in selected)
    return {
        "selected": selected,
        "counts": {
            "reasoning_records_source": len(model.reasoning_records),
            "visible_reasoning_records_source": len(visible),
            "visible_reasoning_blocks_source": source_blocks,
            "visible_reasoning_records_selected": len(selected),
            "visible_reasoning_blocks_selected": selected_blocks,
            "visible_reasoning_records_omitted": len(visible) - len(selected),
            "visible_reasoning_blocks_omitted": source_blocks - selected_blocks,
            "consecutive_exact_repeat_blocks_suppressed": suppressed_repeats,
            "records_dropped_by_cap": capped_out,
            "opaque_reasoning_count": len(opaque_only),
            "selection_policy_version": SELECTION_POLICY_VERSION,
            "selection_scope": "turn_local",
            "selection_cap": cap,
            "anchors_without_same_turn_reasoning": cross_turn_anchors_dropped,
            "anchor_counts": dict(Counter(kind for kind, _, _ in anchors)),
        },
    }


# --------------------------------------------------------------------------
# Logical tool timeline
# --------------------------------------------------------------------------


FOLD_WINDOW_MIN_ROWS = 3


def build_timeline(model: RolloutModel, privacy: Privacy,
                   command_chars: int) -> Dict[str, Any]:
    """Build one persisted-order tool and CommandExecution sequence.

    Wrapper boilerplate (the recorded JS program shell) is dropped, command
    semantics are kept, and consecutive structural polls may collapse into one
    row. Every durable CommandExecution is either represented by its exact-pair
    tool action or emitted as an explicit unpaired execution row.
    """
    rows: List[Dict[str, Any]] = []
    for action in model.tool_actions:
        kind = action["kind"]
        exit_code = action.get("exit_code")
        failed = (
            (exit_code is not None and exit_code != 0)
            or action.get("exec_status") == "failed"
            or bool((action.get("stderr") or "").strip())
        )
        if kind == "command":
            # v1 never folds a shell command. Four rounds of trying to prove
            # which commands are safe to collapse each closed their own matrix
            # and each left another shape in, and the real fixture showed the
            # compression was worth ~15 rows out of 271. One explicit row per
            # recorded command costs a reader a line and needs no classifier.
            label = privacy.clean(action["command"] or "")
            family = "command"
            blocked = True
        elif kind == "command_invocation":
            # Template substitution proves useful invocation provenance but
            # does not prove the concrete command. The durable execution rows
            # below carry execution truth instead.
            label = "exec_command invocation (template unresolved): %s" % \
                privacy.clean(action["command"] or "")
            family = "command invocation"
            blocked = True
        elif kind == "patch":
            paths = [privacy.clean(op["path"]) for op in action["patch_ops"]]
            ops = sorted(set(op["op"] for op in action["patch_ops"]))
            label = "apply_patch [%s] %s" % ("+".join(ops) or "patch",
                                             " ".join(paths))
            family = "apply_patch"
            blocked = True
        elif kind == "stdin":
            session = action.get("stdin_session")
            state = action.get("stdin_chars_state", "UNKNOWN")
            if state == "EMPTY":
                # A proven empty write is a poll: it sends nothing and only
                # waits for output, which is the noise worth collapsing.
                label = "write_stdin session=%s poll (empty input)" % session
                family = "write_stdin poll"
                blocked = False
            elif state == "NON_EMPTY":
                chars = action.get("stdin_chars") or ""
                label = 'write_stdin session=%s input="%s" (%d chars)' % (
                    session, stdin_preview(chars, privacy), len(chars))
                family = "write_stdin"
                blocked = True
            else:
                label = ("write_stdin session=%s input not mechanically "
                         "decoded" % session)
                family = "write_stdin"
                blocked = True
        elif kind == "wait":
            label = "wait"
            family = "wait"
            blocked = False
        else:
            label = "tool %s" % (action.get("tool_name") or "unknown")
            family = "tool:%s" % (action.get("tool_name") or "unknown")
            blocked = True
        rows.append(
            {
                "kind": kind,
                "origin": "tool_action",
                "seq": action["seq"],
                "timestamp": action["timestamp"],
                "turn_id": action["turn_id"],
                "label": truncate(label, command_chars),
                "family": family,
                "fold_blocked": blocked or failed,
                "exit_code": exit_code,
                "exit_source": action.get("exit_source", "unresolved"),
                "exec_status": action.get("exec_status"),
                "wall_time_seconds": (
                    None if kind == "command_invocation"
                    else action.get("wall_time_seconds")
                ),
                "failed": failed,
                "stderr": truncate(privacy.clean(action.get("stderr") or ""), 200),
                "cwd": None,
                "source_marker": (
                    "tool invocation / template-unresolved"
                    if kind == "command_invocation" else None
                ),
                "patch_ops": [
                    {"op": op["op"], "path": privacy.clean(op["path"])}
                    for op in action["patch_ops"]
                ],
            }
        )

    unpaired_executions = [
        execution for execution in model.command_executions
        if not execution["consumed"]
    ]
    for execution in unpaired_executions:
        exit_code = execution.get("exit_code")
        failed = (
            (exit_code is not None and exit_code != 0)
            or execution.get("status") == "failed"
            or bool((execution.get("stderr") or "").strip())
        )
        rows.append(
            {
                "kind": "command_execution_unpaired",
                "origin": "command_execution",
                "seq": execution["seq"],
                "timestamp": execution["timestamp"],
                "turn_id": execution["turn_id"],
                "label": truncate(
                    privacy.clean(execution.get("command") or ""),
                    command_chars,
                ),
                "family": "CommandExecution / unpaired",
                "fold_blocked": True,
                "exit_code": exit_code,
                "exit_source": "command_execution_unpaired",
                "exec_status": execution.get("status"),
                "wall_time_seconds": execution.get("duration_seconds"),
                "failed": failed,
                "stderr": truncate(
                    privacy.clean(execution.get("stderr") or ""), 200
                ),
                "cwd": truncate(
                    privacy.clean(execution.get("cwd") or ""), command_chars
                ),
                "source_marker": "CommandExecution / unpaired",
                "patch_ops": [],
            }
        )

    # Source line index is the common durable order coordinate for invocation
    # and execution records. Python's stable sort preserves ingestion order for
    # the defensive (and currently unobserved) equal-sequence case.
    rows.sort(key=lambda row: row["seq"])

    exported: List[Dict[str, Any]] = []
    folded_away = 0
    index = 0
    while index < len(rows):
        if rows[index]["fold_blocked"]:
            exported.append(rows[index])
            index += 1
            continue
        end_index = index
        while (
            end_index < len(rows)
            and not rows[end_index]["fold_blocked"]
            and rows[end_index]["turn_id"] == rows[index]["turn_id"]
        ):
            end_index += 1
        window = rows[index:end_index]
        if len(window) < FOLD_WINDOW_MIN_ROWS:
            exported.extend(window)
        else:
            families: "OrderedDict[str, dict]" = OrderedDict()
            for row in window:
                entry = families.setdefault(
                    row["family"],
                    {"family": row["family"], "count": 0, "label": row["label"]},
                )
                entry["count"] += 1
            wall = [row["wall_time_seconds"] for row in window
                    if row["wall_time_seconds"] is not None]
            exported.append(
                {
                    "kind": "folded_window",
                    "seq": window[0]["seq"],
                    "turn_id": window[0]["turn_id"],
                    "timestamp": window[0]["timestamp"],
                    "timestamp_end": window[-1]["timestamp"],
                    "count": len(window),
                    "families": list(families.values()),
                    "wall_time_seconds": round(sum(wall), 1) if wall else None,
                    "exit_source": "folded",
                    "failed": False,
                }
            )
            folded_away += len(window)
        index = end_index

    resolvable = [
        row for row in rows
        if row["origin"] == "tool_action" and row["kind"] == "command"
    ]
    resolved = [row for row in resolvable if row["exit_source"] != "unresolved"]
    source_executions = len(model.command_executions)
    paired_executions = sum(
        1 for execution in model.command_executions if execution["consumed"]
    )
    unpaired_count = len(unpaired_executions)
    rendered_unpaired = sum(
        1 for row in exported if row["kind"] == "command_execution_unpaired"
    )
    unpaired_failed = sum(
        1 for row in rows
        if row["kind"] == "command_execution_unpaired" and row["failed"]
    )
    return {
        "rows": exported,
        "counts": {
            "tool_calls_source": len(model.tool_actions),
            "command_execution_records_source": source_executions,
            "command_execution_records_paired": paired_executions,
            "command_execution_records_unpaired": unpaired_count,
            "command_execution_records_unpaired_failed": unpaired_failed,
            "command_execution_records_rendered_unpaired": rendered_unpaired,
            "command_execution_accounting_closed":
                source_executions == paired_executions + unpaired_count,
            "command_execution_rendering_closed":
                rendered_unpaired == unpaired_count,
            "logical_tool_rows_exported": sum(
                1 for row in exported
                if row["kind"] != "command_execution_unpaired"
            ),
            "timeline_rows_exported": len(exported),
            "tool_rows_folded": folded_away,
            "fold_windows": sum(1 for row in exported
                                if row["kind"] == "folded_window"),
            "failed_actions": sum(1 for row in rows if row["failed"]),
            "failed_tool_actions": sum(
                1 for row in rows
                if row["origin"] == "tool_action" and row["failed"]
            ),
            "exit_status_resolved": "%d/%d" % (len(resolved), len(resolvable)),
            "exit_status_unresolved": len(resolvable) - len(resolved),
            "fold_policy_version": FOLD_POLICY_VERSION,
            "fold_window_min_rows": FOLD_WINDOW_MIN_ROWS,
            "command_char_limit": command_chars,
            # The fold contract, stated so a reader never has to infer it.
            "shell_command_folding": False,
            "empty_stdin_poll_folding": True,
            "wait_folding": True,
            "stdin_empty_polls": sum(
                1 for action in model.tool_actions
                if action["kind"] == "stdin"
                and action.get("stdin_chars_state") == "EMPTY"),
            "stdin_non_empty_inputs": sum(
                1 for action in model.tool_actions
                if action["kind"] == "stdin"
                and action.get("stdin_chars_state") == "NON_EMPTY"),
            "stdin_unproven_inputs": sum(
                1 for action in model.tool_actions
                if action["kind"] == "stdin"
                and action.get("stdin_chars_state") == "UNKNOWN"),
            "wait_actions": sum(1 for action in model.tool_actions
                                if action["kind"] == "wait"),
        },
    }


# --------------------------------------------------------------------------
# Input attachments and their task-definition text
# --------------------------------------------------------------------------

FILES_MANIFEST_HEADER = "# Files mentioned by the user:"
MANIFEST_ENTRY_RE = re.compile(r"^##\s+(?P<name>[^\n:]+):\s+(?P<path>/[^\n]+?)\s*$",
                               re.M)
MY_REQUEST_DELIMITER_RE = re.compile(r"^##[ \t]+My request:[ \t]*$", re.M)
ATTACHMENT_ROOTS = ("Downloads", "Desktop")


def parse_attachment_envelope(text: str) -> Optional[Dict[str, Any]]:
    """Split a canonical Codex attachment wrapper from the Owner's own request.

    Codex composes this wrapper around what the Owner actually typed, so the
    preamble is attachment provenance, not Owner prose. Recognition is purely
    structural and every condition must hold:

      * the message begins with the canonical files-manifest header;
      * the manifest block lists at least one `## <name>: /<path>` entry;
      * exactly one canonical `## My request:` delimiter line follows it;
      * the text after that delimiter is non-empty.

    Anything else returns None. No broad regex ever strips Owner text.
    """
    if not text:
        return None
    if not text.lstrip().startswith(FILES_MANIFEST_HEADER):
        return None
    delimiters = list(MY_REQUEST_DELIMITER_RE.finditer(text))
    if len(delimiters) != 1:
        return None
    delimiter = delimiters[0]
    envelope = text[:delimiter.start()]
    request = text[delimiter.end():]
    entries = MANIFEST_ENTRY_RE.findall(envelope)
    if not entries or not request.strip():
        return None
    return {
        "manifest_entry_count": len(entries),
        "envelope_bytes": len(envelope.encode("utf-8")),
        "owner_request_text": request.strip("\n"),
        "owner_request_bytes": len(request.strip("\n").encode("utf-8")),
    }


def _attachment_path_pattern() -> "re.Pattern":
    home = re.escape(str(Path.home()))
    roots = "|".join(re.escape(name) for name in ATTACHMENT_ROOTS)
    return re.compile(r"%s/(?:%s)/[^\s'\"`,;)\]]+" % (home, roots))


def discover_attachments(model: RolloutModel) -> "OrderedDict[str, dict]":
    """Attachment paths the Owner actually referenced, by mechanical evidence."""
    pattern = _attachment_path_pattern()
    found: "OrderedDict[str, dict]" = OrderedDict()

    def remember(path: str, provenance: str, seq: int, name: Optional[str] = None):
        path = path.strip().rstrip(".,;")
        if not path.startswith("/"):
            return
        entry = found.get(path)
        if entry is None:
            found[path] = {
                "path": path,
                "filename": name or os.path.basename(path),
                "provenance": provenance,
                "first_seen_seq": seq,
            }

    sources = list(model.owner_messages) + list(
        getattr(model, "attachment_wrapper_contexts", [])
    )
    for message in sources:
        text = message["text"]
        if FILES_MANIFEST_HEADER in text:
            block = text.split(FILES_MANIFEST_HEADER, 1)[1]
            block = re.split(r"^##\s+My request:", block, 1, re.M)[0]
            for match in MANIFEST_ENTRY_RE.finditer(block):
                remember(match.group("path"), "owner_message_file_manifest",
                         message["seq"], match.group("name").strip())
        for match in pattern.finditer(text):
            remember(match.group(0), "owner_message_path_reference",
                     message["seq"])
    return found


SED_RANGE_RE = re.compile(r"^(?P<start>\d+),(?P<end>\d+|\$)p$")


def read_start_line(command: str) -> Optional[int]:
    """First file line the recorded read command emits, when provable.

    Returns 1 for whole-file readers. Returns None when the range cannot be
    derived, in which case the caller falls back to ordered concatenation.
    """
    tokens = _tokens(_first_line(command))
    if not tokens:
        return None
    program = os.path.basename(tokens[0])
    if program in ("cat", "nl", "bat", "more", "less"):
        return 1
    if program == "sed":
        for token in tokens[1:]:
            match = SED_RANGE_RE.match(token)
            if match:
                return int(match.group("start"))
        return None
    if program == "head":
        return 1
    if program == "tail":
        for index, token in enumerate(tokens):
            if token == "-n" and index + 1 < len(tokens):
                value = tokens[index + 1]
                if value.startswith("+") and value[1:].isdigit():
                    return int(value[1:])
        return None
    return None


def _first_line(command: str) -> str:
    for line in (command or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


SNAPSHOT_COMPLETE = "true"
SNAPSHOT_INCOMPLETE = "false"
SNAPSHOT_UNPROVEN = "unproven"
CURRENT_FILE_PRESENT_READABLE = "present_and_readable"
CURRENT_FILE_ABSENT = "absent"
CURRENT_FILE_PRESENT_UNREADABLE = "present_but_unreadable"
CURRENT_FILE_ACCESS_UNAVAILABLE = "access_unavailable"


def whole_file_read(command: str) -> bool:
    """True when the recorded reader provably emits the file from start to end."""
    tokens = _tokens(_first_line(command))
    if not tokens:
        return False
    program = os.path.basename(tokens[0])
    if program == "cat":
        # Only plain cat is byte-faithful enough to materialize without a
        # current-file hash. `cat -v`, `nl`, `bat`, pagers and hex viewers may
        # transform display bytes even when they appear to cover the file.
        return not any(token.startswith("-") for token in tokens[1:])
    # A full-range sed read is useful snapshot evidence, but without a current
    # file hash it does not prove final-newline byte identity.
    return False


def count_file_lines(path: Path) -> Optional[int]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return None


def current_attachment_evidence(candidate: Path,
                                record: Dict[str, Any]
                                ) -> Tuple[Optional[int], Optional[str],
                                           Optional[int]]:
    """Best-effort export-time corroboration for one attachment path.

    Persisted rollout evidence remains authoritative. A current file can help
    corroborate it, but an inaccessible file must never abort the export or be
    mislabeled as absent. Raw exception text is deliberately discarded.
    """
    try:
        candidate_stat = candidate.stat()
    except FileNotFoundError:
        return None, None, None
    except OSError:
        record["current_file_access"] = CURRENT_FILE_ACCESS_UNAVAILABLE
        return None, None, None
    if not stat.S_ISREG(candidate_stat.st_mode):
        return None, None, None

    record["current_file_present"] = True
    record["current_file_bytes"] = candidate_stat.st_size
    try:
        current_sha = sha256_file(candidate)
        current_lines = count_file_lines(candidate)
        if current_lines is None:
            record["current_file_access"] = CURRENT_FILE_PRESENT_UNREADABLE
            return candidate_stat.st_size, None, None
    except OSError:
        record["current_file_access"] = CURRENT_FILE_PRESENT_UNREADABLE
        return candidate_stat.st_size, None, None

    record["current_file_access"] = CURRENT_FILE_PRESENT_READABLE
    record["current_file_sha256"] = current_sha
    return candidate_stat.st_size, current_sha, current_lines


def recover_attachment_text(model: RolloutModel, path: str) -> Dict[str, Any]:
    """Rebuild attachment text from persisted faithful reads of that exact path.

    Overlapping reads are reassembled by recorded line range rather than
    concatenated, so a re-read never duplicates a region of the file.
    """
    segments: List[Dict[str, Any]] = []
    seen_commands = set()
    for action in model.tool_actions:
        if action["kind"] != "command" or not action.get("output_present"):
            continue
        command = action["command"] or ""
        if faithful_read_target(command) != path:
            continue
        if action.get("exit_code") not in (0, None):
            continue
        normalized = " ".join(command.split())
        if normalized in seen_commands:
            continue
        seen_commands.add(normalized)
        segments.append(
            {
                "command": normalized,
                "text": action["output_body"],
                "start_line": read_start_line(command),
                "whole_file": whole_file_read(command),
            }
        )
    if not segments:
        return {"text": None, "segments": [], "assembly": "none", "gaps": 0,
                "first_line": None, "recovered_line_count": 0,
                "whole_file_read_persisted": False}

    whole = any(segment["whole_file"] for segment in segments)
    if all(segment["start_line"] for segment in segments):
        lines: Dict[int, str] = {}
        for segment in segments:
            start = segment["start_line"]
            for offset, line in enumerate(segment["text"].splitlines(True)):
                lines.setdefault(start + offset, line)
        ordered = sorted(lines)
        gaps = sum(
            1 for previous, current in zip(ordered, ordered[1:])
            if current != previous + 1
        )
        text = "".join(lines[number] for number in ordered)
        assembly = "line_range_reassembly"
        first_line = ordered[0] if ordered else None
        recovered_lines = len(ordered)
    else:
        text = "".join(segment["text"] for segment in segments)
        gaps = 0
        assembly = "ordered_concatenation"
        first_line = None
        recovered_lines = len(text.splitlines())
    return {
        "text": text,
        "segments": [segment["command"] for segment in segments],
        "assembly": assembly,
        "gaps": gaps,
        "first_line": first_line,
        "recovered_line_count": recovered_lines,
        "whole_file_read_persisted": whole,
    }


def build_attachments(model: RolloutModel, privacy: Privacy,
                      max_bytes: int) -> List[dict]:
    results = []
    for path, entry in discover_attachments(model).items():
        record: Dict[str, Any] = {
            "filename": entry["filename"],
            "path_normalized": safe_source_reference(path, privacy),
            "provenance": entry["provenance"],
            "snapshot_source": "unavailable",
            "snapshot_bytes": 0,
            "snapshot_sha256": None,
            "snapshot_truncated": False,
            "snapshot_segments": [],
            "snapshot_complete": SNAPSHOT_UNPROVEN,
            "snapshot_completeness_basis": "no_snapshot_recovered",
            "text": None,
            "current_file_present": False,
            "current_file_access": CURRENT_FILE_ABSENT,
            "materialization_status": "unavailable",
            "materialization_source": None,
            "materialized_member": None,
            "materialized_bytes": None,
            "materialized_sha256": None,
            "payload_complete": False,
        }
        candidate = Path(path)
        current_bytes, current_sha, current_lines = \
            current_attachment_evidence(candidate, record)
        recovered = recover_attachment_text(model, path)
        text = recovered["text"]
        source = "persisted_tool_output"
        if text is None \
                and record["current_file_access"] == \
                CURRENT_FILE_PRESENT_READABLE \
                and (current_bytes or 0) <= max_bytes:
            try:
                text = candidate.read_text(encoding="utf-8", errors="strict")
                source = "current_file_at_export_time"
            except UnicodeDecodeError:
                # Binary attachments are still eligible for exact byte copying;
                # they simply do not acquire a misleading text snapshot.
                text = None
            except OSError:
                record["current_file_access"] = \
                    CURRENT_FILE_PRESENT_UNREADABLE
                record.pop("current_file_sha256", None)
                current_sha = None
                current_lines = None
                text = None
        raw_snapshot = None
        if text is not None:
            raw_snapshot = text.encode("utf-8")
            if len(raw_snapshot) > max_bytes:
                text = raw_snapshot[:max_bytes].decode("utf-8", errors="ignore")
                raw_snapshot = text.encode("utf-8")
                record["snapshot_truncated"] = True
            record["snapshot_source"] = source
            record["snapshot_segments"] = [privacy.clean(item)
                                           for item in recovered["segments"]]
            record["snapshot_assembly"] = recovered["assembly"] if source == \
                "persisted_tool_output" else "current_file_read"
            record["snapshot_line_gaps"] = recovered["gaps"]
            record["snapshot_bytes"] = len(raw_snapshot)
            record["snapshot_sha256"] = hashlib.sha256(raw_snapshot).hexdigest()
            record["text"] = privacy.clean(text)
            if current_sha and not record["snapshot_truncated"]:
                record["snapshot_matches_current_file"] = (
                    record["snapshot_sha256"] == current_sha
                )
            verdict, basis = snapshot_completeness(
                record, recovered, current_sha, current_lines
            )
            record["snapshot_complete"] = verdict
            record["snapshot_completeness_basis"] = basis

        safe_name = safe_member_filename(entry["filename"], "attachment")
        if raw_snapshot is not None \
                and record["snapshot_source"] == "persisted_tool_output" \
                and record["snapshot_complete"] == SNAPSHOT_COMPLETE:
            if sanitization_rescan(raw_snapshot.decode("utf-8", errors="strict")):
                record["materialization_status"] = "blocked_privacy"
                record["materialization_source"] = \
                    "persisted_faithful_snapshot"
            else:
                record["_materialized_bytes"] = raw_snapshot
                record["_preferred_member"] = "%s/%s" % (
                    ATTACHMENTS_DIRNAME, safe_name
                )
                record["materialization_status"] = "ready"
                record["materialization_source"] = \
                    "persisted_faithful_snapshot"
                record["payload_complete"] = True
        elif record["current_file_access"] == CURRENT_FILE_PRESENT_READABLE:
            record["_candidate_path"] = str(candidate)
            record["_candidate_sha256"] = current_sha
            record["_candidate_bytes"] = current_bytes
            record["_preferred_member"] = "%s/%s" % (
                ATTACHMENTS_DIRNAME, safe_name
            )
            record["materialization_status"] = "ready"
            record["materialization_source"] = "current_exact_file"
            record["payload_complete"] = True
        elif raw_snapshot is not None:
            partial = privacy.clean(text or "").encode("utf-8")
            if partial:
                if sanitization_rescan(partial.decode("utf-8")):
                    record["materialization_status"] = "blocked_privacy"
                    record["materialization_source"] = \
                        "persisted_partial_snapshot"
                else:
                    record["_materialized_bytes"] = partial
                    record["_preferred_member"] = (
                        "%s/_partial/%s.partial.txt"
                        % (ATTACHMENTS_DIRNAME, safe_name)
                    )
                    record["materialization_status"] = "partial"
                    record["materialization_source"] = \
                        "persisted_partial_snapshot"
            record["payload_complete"] = False
        results.append(record)
    return results


def snapshot_completeness(record: Dict[str, Any], recovered: Dict[str, Any],
                          current_sha: Optional[str],
                          current_lines: Optional[int]
                          ) -> Tuple[str, str]:
    """Decide, mechanically, whether the whole attachment was recovered.

    `true` is only ever returned when coverage is proven — never inferred from
    a snapshot merely looking plausible, and never claimed for an export-time
    re-read, which says nothing about the bytes the Owner actually sent.
    """
    if record["snapshot_source"] == "current_file_at_export_time":
        return (SNAPSHOT_UNPROVEN,
                "read from the export-time file; no persisted digest proves "
                "this is what was sent")
    if record["snapshot_truncated"]:
        return (SNAPSHOT_INCOMPLETE, "snapshot truncated at the byte cap")
    if recovered.get("gaps"):
        return (SNAPSHOT_INCOMPLETE,
                "%d gap(s) between persisted line ranges" % recovered["gaps"])
    first_line = recovered.get("first_line")
    if first_line is not None and first_line > 1:
        return (SNAPSHOT_INCOMPLETE,
                "persisted reads start at line %d, not line 1" % first_line)
    if current_sha and record["snapshot_sha256"] == current_sha:
        return (SNAPSHOT_COMPLETE,
                "recovered text is byte-identical to the export-time file")
    if current_lines is not None \
            and recovered.get("recovered_line_count", 0) < current_lines:
        return (SNAPSHOT_INCOMPLETE,
                "recovered %d of %d line(s) present in the export-time file"
                % (recovered.get("recovered_line_count", 0), current_lines))
    if recovered.get("whole_file_read_persisted"):
        return (SNAPSHOT_COMPLETE,
                "a persisted whole-file read covers the attachment end to end")
    if first_line == 1:
        return (SNAPSHOT_UNPROVEN,
                "contiguous from line 1, but nothing proves the last line was "
                "reached")
    return (SNAPSHOT_UNPROVEN, "persisted coverage could not be bounded")


# --------------------------------------------------------------------------
# Output artifacts
# --------------------------------------------------------------------------


FINAL_ARTIFACT_READY = "READY"
FINAL_ARTIFACT_PENDING_NOT_FOUND = "PENDING_NOT_FOUND"
FINAL_ARTIFACT_PENDING_ACCESS_UNAVAILABLE = "PENDING_ACCESS_UNAVAILABLE"
FINAL_ARTIFACT_PENDING_MTIME_TOO_OLD = "PENDING_MTIME_TOO_OLD"
FINAL_ARTIFACT_REJECT_NON_REGULAR = "REJECT_NON_REGULAR"
FINAL_ARTIFACT_REJECT_MTIME_UNAVAILABLE = "REJECT_MTIME_UNAVAILABLE"
FINAL_ARTIFACT_REJECT_STAT_ERROR = "REJECT_STAT_ERROR"
FINAL_ARTIFACT_MATERIALIZATION_MTIME_TOO_OLD = "mtime_too_old"
FINAL_ARTIFACT_MATERIALIZATION_REJECTED_SYMLINK = "rejected_symlink"
FINAL_ARTIFACT_MATERIALIZATION_REJECTED_NON_REGULAR = \
    "rejected_non_regular"
FINAL_ARTIFACT_PENDING_STATES = frozenset((
    FINAL_ARTIFACT_PENDING_NOT_FOUND,
    FINAL_ARTIFACT_PENDING_ACCESS_UNAVAILABLE,
    FINAL_ARTIFACT_PENDING_MTIME_TOO_OLD,
))
FINAL_ARTIFACT_RETRYABLE_MATERIALIZATION_STATES = frozenset((
    "unavailable",
    "source_changed",
    FINAL_ARTIFACT_MATERIALIZATION_MTIME_TOO_OLD,
))


MARKDOWN_PATH_RE = re.compile(
    r"\]\((?P<path>(?:<(?:file://)?/[^>\n]+>|(?:file://)?/[^)\n]+))\)"
)
BACKTICK_PATH_RE = re.compile(r"`(?P<path>(?:file://)?/[^`\n]+)`")


def _path_from_final_text(value: str) -> Optional[Path]:
    value = value.strip().strip("<>")
    if value.startswith("file://"):
        value = value[7:]
    # Codex local-file links may carry a final `:line` coordinate.
    match = re.match(r"^(.*):([0-9]+)$", value)
    if match and _safe_deliverable_type(Path(match.group(1))):
        value = match.group(1)
    return Path(value)


def _safe_deliverable_type(path: Path) -> bool:
    lower = path.name.lower()
    return any(lower.endswith(suffix) for suffix in SAFE_ARTIFACT_SUFFIXES)


def _iso_epoch(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def final_answer_path_references(model: RolloutModel) -> Dict[str, List[dict]]:
    """Keep source-message ownership when deduplicating exact final paths.

    A path mentioned by multiple turns is ambiguous: current bytes cannot
    prove which version was produced by which turn. Never choose the latest.
    An exact provider message-ID mirror can supply a missing turn ID.
    """
    candidates: Dict[str, List[dict]] = {}
    for message in model.assistant_messages:
        if message.get("phase") != "final_answer":
            continue
        text = message.get("text") or ""
        raw_candidates = [
            match.group("path") for match in MARKDOWN_PATH_RE.finditer(text)
        ]
        raw_candidates.extend(
            match.group("path") for match in BACKTICK_PATH_RE.finditer(text)
        )
        for raw in raw_candidates:
            candidate = _path_from_final_text(raw)
            if candidate is None or not candidate.is_absolute() \
                    or not _safe_deliverable_type(candidate):
                continue
            turn_ids = set(model.message_turn_bindings.get(message.get("id"), ()))
            if message.get("turn_id"):
                turn_ids.add(message["turn_id"])
            reference = {
                "message_id": message.get("id"),
                "source_sequence": message["seq"],
                "turn_ids": sorted(turn_ids),
                "timestamp": message.get("timestamp"),
                "binding_source": ("conflicted_provider_message_id" if len(turn_ids) > 1
                                   else "response_item_turn_id" if message.get("turn_id")
                                   else "exact_message_id_mirror" if turn_ids
                                   else "missing_turn_id"),
            }
            references = candidates.setdefault(str(candidate), [])
            if reference not in references:
                references.append(reference)
    return candidates


def final_answer_path_candidates(model: RolloutModel) -> List[Path]:
    """Return only exact, safe absolute paths named in persisted final text."""
    return [Path(path) for path in final_answer_path_references(model)]


def _classify_final_answer_artifact_candidate(
        candidate: Path, started_epoch: Optional[float]
        ) -> Tuple[str, Optional[os.stat_result]]:
    """Classify one structurally valid exact path with bounded metadata probes.

    Parsing and suffix checks happen before this function. The classifier is
    shared by publication and retry discovery so capture and pending state
    cannot silently apply different filesystem or mtime rules. This preliminary
    pathname observation never authorizes bytes: materialization revalidates
    and reads one actually opened object without following a final symlink.
    """
    try:
        candidate_lstat = candidate.lstat()
    except FileNotFoundError:
        return FINAL_ARTIFACT_PENDING_NOT_FOUND, None
    except PermissionError:
        return FINAL_ARTIFACT_PENDING_ACCESS_UNAVAILABLE, None
    except OSError as error:
        if error.errno in (errno.EACCES, errno.EPERM):
            return FINAL_ARTIFACT_PENDING_ACCESS_UNAVAILABLE, None
        # Do not relabel an arbitrary stat failure as an access denial. It is
        # retained as incomplete evidence by discovery, but is not perpetual
        # pending work without a known retryable class.
        return FINAL_ARTIFACT_REJECT_STAT_ERROR, None
    if not stat.S_ISREG(candidate_lstat.st_mode):
        return FINAL_ARTIFACT_REJECT_NON_REGULAR, candidate_lstat
    try:
        candidate_stat = candidate.stat()
    except FileNotFoundError:
        return FINAL_ARTIFACT_PENDING_NOT_FOUND, None
    except PermissionError:
        return FINAL_ARTIFACT_PENDING_ACCESS_UNAVAILABLE, None
    except OSError as error:
        if error.errno in (errno.EACCES, errno.EPERM):
            return FINAL_ARTIFACT_PENDING_ACCESS_UNAVAILABLE, None
        return FINAL_ARTIFACT_REJECT_STAT_ERROR, None
    if not stat.S_ISREG(candidate_stat.st_mode):
        return FINAL_ARTIFACT_REJECT_NON_REGULAR, candidate_stat
    if started_epoch is None:
        return FINAL_ARTIFACT_REJECT_MTIME_UNAVAILABLE, candidate_stat
    if candidate_stat.st_mtime < started_epoch - 1:
        return FINAL_ARTIFACT_PENDING_MTIME_TOO_OLD, candidate_stat
    return FINAL_ARTIFACT_READY, candidate_stat


def classify_final_answer_artifact_candidates(
        model: RolloutModel) -> List[Dict[str, Any]]:
    """Classify exact final paths without directory enumeration or expansion."""
    started_epoch = _iso_epoch(model.session_meta.get("timestamp"))
    classified: List[Dict[str, Any]] = []
    for path, references in final_answer_path_references(model).items():
        candidate = Path(path)
        state, candidate_stat = _classify_final_answer_artifact_candidate(
            candidate, started_epoch
        )
        classified.append({
            "candidate": candidate,
            "state": state,
            "stat": candidate_stat,
            "started_epoch": started_epoch,
            "references": references,
        })
    return classified


def pending_final_answer_artifact_count(
        classified: Sequence[Dict[str, Any]]) -> int:
    return sum(
        1 for row in classified if row.get("state") in FINAL_ARTIFACT_PENDING_STATES
    )


def retryable_final_answer_artifact_count(
        classified: Sequence[Dict[str, Any]],
        artifacts: Sequence[Dict[str, Any]]) -> int:
    """Count unique exact final paths that still need bounded re-evaluation.

    Classification supplies the pre-materialization pending states. A READY
    candidate can become retryable later when its exact payload cannot be read
    or changes between the bounded stat and read. Explicit permanent rejects
    remain non-retryable.
    """
    classified_by_path = {
        str(row["candidate"]): row.get("state")
        for row in classified
        if row.get("candidate") is not None
    }
    retryable_paths = {
        path for path, state in classified_by_path.items()
        if state in FINAL_ARTIFACT_PENDING_STATES
    }
    materialized_paths = set()
    for artifact in artifacts:
        candidate_path = artifact.get("_exact_final_candidate_path") \
            or artifact.get("_candidate_path")
        if not candidate_path:
            continue
        candidate_path = str(candidate_path)
        if artifact.get("materialization_status") == "materialized" \
                and artifact.get("payload_complete") is True:
            materialized_paths.add(candidate_path)
            continue
        if classified_by_path.get(candidate_path) != FINAL_ARTIFACT_READY:
            continue
        if artifact.get("materialization_status") in \
                FINAL_ARTIFACT_RETRYABLE_MATERIALIZATION_STATES \
                and artifact.get("payload_complete") is not True:
            retryable_paths.add(candidate_path)
    return len(retryable_paths - materialized_paths)


def missing_final_answer_artifact_count(model: RolloutModel) -> int:
    """Compatibility count for all retryable exact final-path candidates."""
    return pending_final_answer_artifact_count(
        classify_final_answer_artifact_candidates(model)
    )


def discover_final_answer_artifacts(model: RolloutModel,
                                    privacy: Privacy,
                                    classified_candidates: Optional[
                                        Sequence[Dict[str, Any]]
                                    ] = None) -> List[dict]:
    """Exact final-answer paths with conservative type/time ownership proof."""
    rows: List[dict] = []
    classified = list(
        classified_candidates
        if classified_candidates is not None
        else classify_final_answer_artifact_candidates(model)
    )
    for classified_row in classified:
        candidate = classified_row["candidate"]
        state = classified_row["state"]
        candidate_stat = classified_row.get("stat")
        started_epoch = classified_row.get("started_epoch")
        if state in (
                FINAL_ARTIFACT_PENDING_NOT_FOUND,
                FINAL_ARTIFACT_PENDING_MTIME_TOO_OLD,
                FINAL_ARTIFACT_REJECT_NON_REGULAR,
                FINAL_ARTIFACT_REJECT_MTIME_UNAVAILABLE):
            continue
        if state in (
                FINAL_ARTIFACT_PENDING_ACCESS_UNAVAILABLE,
                FINAL_ARTIFACT_REJECT_STAT_ERROR):
            # The persisted final answer is exact evidence that a safe
            # deliverable was surfaced. If the filesystem denies the one
            # bounded proof attempt (or stat fails for another classified
            # reason), retain that fact without the raw path or OS error and
            # fail the package-completeness claim closed.
            rows.append(
                {
                    "role": "auto_final_deliverable",
                    "filename": candidate.name,
                    "path_normalized": safe_source_reference(
                        str(candidate), privacy
                    ),
                    "provenance": (
                        "assistant_final_exact_path_access_unavailable"
                        if state == FINAL_ARTIFACT_PENDING_ACCESS_UNAVAILABLE
                        else "assistant_final_exact_path_stat_unavailable"
                    ),
                    "materialization_status": "unavailable",
                    "materialization_source": None,
                    "materialized_member": None,
                    "materialized_bytes": None,
                    "materialized_sha256": None,
                    "payload_complete": False,
                    "_exact_final_candidate_path": str(candidate),
                }
            )
            continue
        if state != FINAL_ARTIFACT_READY or candidate_stat is None:
            continue
        rows.append(
            {
                "role": "auto_final_deliverable",
                "filename": candidate.name,
                "path_normalized": safe_source_reference(
                    str(candidate), privacy
                ),
                "provenance": "assistant_final_exact_path_with_mtime",
                "materialization_status": "ready",
                "materialization_source": "current_exact_file",
                "materialized_member": None,
                "materialized_bytes": None,
                "materialized_sha256": None,
                "payload_complete": True,
                "_candidate_path": str(candidate),
                "_candidate_bytes": candidate_stat.st_size,
                "_exact_final_candidate_path": str(candidate),
                "_final_artifact_started_epoch": started_epoch,
                "_preferred_member": "%s/%s" % (
                    ARTIFACTS_DIRNAME,
                    safe_member_filename(candidate.name, "artifact"),
                ),
            }
        )
    return rows


def build_artifacts(model: RolloutModel, privacy: Privacy,
                    review_bundle: Optional[str],
                    artifacts: Sequence[str],
                    classified_final_candidates: Optional[
                        Sequence[Dict[str, Any]]
                    ] = None) -> Tuple[List[dict], bool]:
    """Exact supplied paths plus conservative persisted final-answer paths."""
    rows: List[dict] = []
    review_present = False
    entries: List[Tuple[str, str]] = []
    if review_bundle:
        entries.append(("review_bundle", review_bundle))
    for item in artifacts:
        if "=" not in item:
            raise ExportBlocked(
                STATUS_BLOCKED_SELECTION,
                "--artifact must be ROLE=PATH, got %r" % item,
            )
        role, _, raw_path = item.partition("=")
        entries.append((role.strip() or "artifact", raw_path))
    for role, raw_path in entries:
        path = Path(raw_path).expanduser()
        if not path.is_file():
            raise ExportBlocked(
                STATUS_BLOCKED_SELECTION,
                "artifact %s is not an existing regular file: %s" % (role, raw_path),
            )
        try:
            path_stat = path.stat()
        except OSError:
            path_stat = None
        row = {
            "role": role,
            "filename": path.name,
            "path_normalized": safe_source_reference(str(path), privacy),
            "provenance": "explicit_argument",
            "materialization_status": "ready" if path_stat else "unavailable",
            "materialization_source": "current_exact_file" if path_stat else None,
            "materialized_member": None,
            "materialized_bytes": None,
            "materialized_sha256": None,
            "payload_complete": bool(path_stat),
        }
        if path_stat:
            row.update({
                "source_mtime_ns": path_stat.st_mtime_ns,
                "_candidate_path": str(path),
                "_candidate_bytes": path_stat.st_size,
                "_preferred_member": "%s/%s" % (
                    ARTIFACTS_DIRNAME,
                    safe_member_filename(path.name, "artifact"),
                ),
            })
        rows.append(row)
        if role == "review_bundle":
            review_present = True
    explicit_paths = {
        row.get("_candidate_path") for row in rows
        if row.get("_candidate_path")
    }
    for row in discover_final_answer_artifacts(
            model, privacy, classified_final_candidates):
        candidate_path = row.get("_candidate_path") \
            or row.get("_exact_final_candidate_path")
        if candidate_path not in explicit_paths:
            rows.append(row)
    return rows, review_present


def _provenance_time(value: Any) -> Optional[str]:
    """Normalize provider time only; never fall back to the publication clock."""
    try:
        value = iso_utc(value)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (AttributeError, TypeError, ValueError, OverflowError, OSError):
        return None


def artifact_turn_provenance(model: RolloutModel, artifacts: List[dict],
                             classified: Sequence[dict]) -> List[dict]:
    """Plan names before availability filtering, using only source references.

    Full turn IDs and exact-path digests are machine identity. Ordinals follow
    all raw turns, including partial turns. The short directory token is a
    deterministic digest of the full ID, not a title or a filesystem time.
    """
    plans = {}
    for candidate in classified:
        path = str(candidate["candidate"])
        plans[path] = {
            "references": candidate.get("references", []),
            "candidate_state": candidate["state"],
        }
    for row in artifacts:
        path = row.get("_exact_final_candidate_path") or row.get("_candidate_path")
        # Legacy carry-forward has no raw path authority. Its attested source
        # reference is usable for namespacing, never for assigning a turn.
        key = str(path) if path else "legacy:" + str(row.get("path_normalized"))
        if row.get("legacy_carry_forward"):
            key = "legacy:" + json.dumps(row["legacy_carry_forward"], sort_keys=True)
        row["_artifact_plan_key"] = key
        plans.setdefault(key, {"references": [], "candidate_state": "explicit_or_legacy"})

    for path, plan in plans.items():
        refs = plan.pop("references")
        owners = {turn for ref in refs for turn in ref["turn_ids"]}
        complete_binding = bool(refs) and all(len(ref["turn_ids"]) == 1 for ref in refs)
        owner = next(iter(owners)) if complete_binding and len(owners) == 1 else None
        ordinal = model.turn_index(owner)
        owner = owner if ordinal is not None else None
        if owner:
            reason = None
        elif not refs:
            reason = "no_persisted_final_turn_reference"
        elif len(owners) > 1:
            reason = "multiple_or_conflicting_source_turns"
        else:
            reason = "missing_or_unbound_source_turn"
        completed = model.turn_completed.get(owner, {})
        completion = _provenance_time(completed.get("completed_at")) or \
            _provenance_time(completed.get("timestamp"))
        reference_time = next((stamp for stamp in (
            _provenance_time(ref.get("timestamp")) for ref in refs
        ) if stamp), None)
        visible_dir = ("T%02d__%s" % (ordinal, sha256_text(owner)[:12])
                       if owner else "UNATTRIBUTED")
        source_id = sha256_text(path)
        plan.update({
            "artifact_provenance_version": ARTIFACT_PROVENANCE_VERSION,
            "source_artifact_id": source_id,
            "turn_id": owner,
            "source_turn_ordinal": ordinal if owner else None,
            "visible_turn_dir": visible_dir,
            "attribution_status": "attributed" if owner else "unattributed",
            "attribution_source": ("persisted_final_exact_path_and_turn_binding"
                                   if owner else "unproven"),
            "attribution_reason": reason,
            "turn_completed_at": completion,
            "source_reference_at": reference_time,
            "source_final_references": refs,
            "turn_label": "第 %02d 轮" % ordinal if owner else "归属未确定",
            "turn_label_source": "neutral_source_ordinal",
        })
        # Source paths supply filenames, not ownership. A legacy row may
        # carry a different original filename, so prefer the attested field.
        matching = [row for row in artifacts if row["_artifact_plan_key"] == path]
        if matching and matching[0].get("legacy_carry_forward"):
            plan["attribution_reason"] = "legacy_v2_1_turn_unproven"
        plan["original_filename"] = (matching[0]["filename"] if matching else Path(path).name)

    groups: Dict[Tuple[str, str], set] = {}
    for plan in plans.values():
        key = (plan["visible_turn_dir"],
               unicodedata.normalize("NFC", plan["original_filename"]).casefold())
        groups.setdefault(key, set()).add(plan["source_artifact_id"])
    for plan in plans.values():
        filename = plan["original_filename"]
        key = (plan["visible_turn_dir"], unicodedata.normalize("NFC", filename).casefold())
        parent = ARTIFACTS_DIRNAME + "/" + plan["visible_turn_dir"]
        if plan["attribution_status"] == "unattributed":
            parent += "/" + plan["source_artifact_id"]
        elif len(groups[key]) > 1 or filename.casefold() == "by_source":
            parent += "/BY_SOURCE/" + plan["source_artifact_id"]
        plan["planned_member"] = parent + "/" + filename
    for row in artifacts:
        plan = plans[row["_artifact_plan_key"]]
        row.update(plan)
        row["_preferred_member"] = plan["planned_member"]
        # Preserve exact names or refuse materialization; never silently
        # truncate, normalize or add a suffix to an artifact filename.
        if not _safe_package_member(plan["planned_member"]) or any(
                ord(char) < 32 or ord(char) == 127 for char in row["filename"]):
            row["materialization_status"] = "blocked_unrepresentable_filename"
            row["payload_complete"] = False
    return [dict(plan) for plan in plans.values()]


def render_output_index(candidates: Sequence[dict], artifacts: Sequence[dict],
                        privacy: Privacy) -> str:
    """Derived navigation only. Machine authority remains manifest/receipt."""
    entries = {row["source_artifact_id"]: dict(row) for row in candidates}
    for row in artifacts:
        entries[row["source_artifact_id"]].update(public_payload_row(row))
    lines = ["# 输出索引", "", "归属、完整性与哈希以 manifest / receipt 为准。", ""]
    ordered = sorted(entries.values(), key=lambda row: (
        row["source_turn_ordinal"] or float("inf"), row["source_artifact_id"]
    ))
    previous = None
    for row in ordered:
        directory = row["visible_turn_dir"]
        if directory != previous:
            lines.extend([privacy.clean("## %s · %s" % (directory, row["turn_label"])), "",
                          privacy.clean("完整 turn ID：%s" % (row["turn_id"] or "未知")),
                          privacy.clean("完成时间：%s" % (row["turn_completed_at"] or "未知 / 未完成")), ""])
            previous = directory
        status = row.get("materialization_status") or row["candidate_state"]
        filename = privacy.clean(row["original_filename"])
        label = filename.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
        member = row.get("materialized_member")
        # Check the real, decoded member before URL encoding. Redacting an
        # assembled href creates a link to a file that was never published.
        suppressed = bool(member and privacy.clean_without_count(member) != member)
        link = ("[%s](%s)" % (label, quote(member.split("/", 1)[1], safe="/"))
                if member and not suppressed else label)
        explanation = "；路径因隐私规则隐藏，未提供链接" if suppressed else ""
        lines.append("- %s — %s%s" % (
            link, privacy.clean(status), explanation +
            ("；原因：" + privacy.clean(row["attribution_reason"]) if row["attribution_reason"] else "")))
    if not ordered:
        lines.append("没有输出文件。")
    return "\n".join(lines) + "\n"


def _deduplicated_member(preferred: str, identity: str,
                         used: set) -> str:
    member = preferred
    if member not in used:
        used.add(member)
        return member
    parent, _, filename = preferred.rpartition("/")
    stem, suffix = os.path.splitext(filename)
    member = "%s/%s__%s%s" % (
        parent, stem, sha256_text(identity)[:8], suffix
    )
    counter = 2
    while member in used:
        member = "%s/%s__%s_%d%s" % (
            parent, stem, sha256_text(identity)[:8], counter, suffix
        )
        counter += 1
    used.add(member)
    return member


def _opened_object_identity(value: os.stat_result) -> Tuple[int, ...]:
    """Fields whose change invalidates one opened-object materialization."""
    mtime_ns = getattr(
        value, "st_mtime_ns", int(value.st_mtime * 1000000000)
    )
    try:
        ctime_ns = value.st_ctime_ns
    except AttributeError as error:
        raise ExportBlocked(
            STATUS_BLOCKED_PACKAGE,
            "object-bound final artifact identity requires exact st_ctime_ns",
        ) from error
    if not isinstance(ctime_ns, int):
        raise ExportBlocked(
            STATUS_BLOCKED_PACKAGE,
            "object-bound final artifact identity requires integer st_ctime_ns",
        )
    return (
        value.st_dev,
        value.st_ino,
        stat.S_IFMT(value.st_mode),
        value.st_size,
        mtime_ns,
        ctime_ns,
    )


def _read_opened_final_artifact(
        candidate_path: str, started_epoch: Optional[float],
        expected_bytes: int, max_bytes: int,
        observed: Optional[Dict[str, Any]] = None,
        ) -> Tuple[str, Optional[bytes]]:
    """Validate and read one exact final artifact through the same open fd.

    ``O_NOFOLLOW`` is required at the actual open boundary; a pathname check is
    not accepted as a substitute. Nonblocking open prevents a regular-file
    replacement race from hanging on a FIFO or device before ``fstat`` can
    reject it. The read is bounded by the remaining package cap.
    """
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise ExportBlocked(
            STATUS_BLOCKED_PACKAGE,
            "object-bound final artifact open requires O_NOFOLLOW",
        )
    flags = os.O_RDONLY | nofollow
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(candidate_path, flags)
    except OSError as error:
        if error.errno == errno.ELOOP:
            return FINAL_ARTIFACT_MATERIALIZATION_REJECTED_SYMLINK, None
        return "unavailable", None

    try:
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                return FINAL_ARTIFACT_MATERIALIZATION_REJECTED_NON_REGULAR, None
            if started_epoch is None:
                return FINAL_ARTIFACT_MATERIALIZATION_REJECTED_NON_REGULAR, None
            if before.st_mtime < started_epoch - 1:
                return FINAL_ARTIFACT_MATERIALIZATION_MTIME_TOO_OLD, None
            if before.st_size != expected_bytes:
                return "source_changed", None
            if before.st_size > max_bytes:
                return "skipped_package_cap", None

            chunks: List[bytes] = []
            remaining = max_bytes + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            after = os.fstat(descriptor)
        except OSError:
            return "unavailable", None
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass

    if len(data) > max_bytes:
        return "source_changed", None
    if not stat.S_ISREG(after.st_mode) \
            or _opened_object_identity(before) != _opened_object_identity(after) \
            or len(data) != before.st_size:
        return "source_changed", None
    if observed is not None:
        observed["source_mtime_ns"] = before.st_mtime_ns
    return "ready", data


def prepare_package_payloads(attachments: List[dict], artifacts: List[dict],
                             max_package_bytes: int) -> Dict[str, Any]:
    """Read only exact attributed payloads, enforce one bounded package cap."""
    used = set()
    materialized_artifacts: Dict[str, dict] = {}
    consumed = 0
    rows = [("attachment", row) for row in attachments]
    rows.extend(("artifact", row) for row in artifacts)
    for kind, row in rows:
        if row.get("materialization_status") not in ("ready", "partial"):
            continue
        prior = (materialized_artifacts.get(row.get("_preferred_member"))
                 if kind == "artifact" else None)
        available = max_package_bytes - consumed + (prior["materialized_bytes"] if prior else 0)
        data = row.get("_materialized_bytes")
        size = len(data) if isinstance(data, bytes) else row.get("_candidate_bytes")
        if not isinstance(size, int) or size < 0:
            row["materialization_status"] = "unavailable"
            row["payload_complete"] = False
            continue
        if size > available:
            row["materialization_status"] = "skipped_package_cap"
            row["payload_complete"] = False
            row.pop("_materialized_bytes", None)
            continue
        if data is None:
            exact_final_path = row.get("_exact_final_candidate_path")
            if exact_final_path:
                status, data = _read_opened_final_artifact(
                    str(exact_final_path),
                    row.get("_final_artifact_started_epoch"),
                    size,
                    available,
                    observed=row,
                )
                if status != "ready" or data is None:
                    row["materialization_status"] = status
                    row["payload_complete"] = False
                    continue
            else:
                try:
                    data = Path(row["_candidate_path"]).read_bytes()
                except (KeyError, OSError):
                    row["materialization_status"] = "unavailable"
                    row["payload_complete"] = False
                    continue
            digest = hashlib.sha256(data).hexdigest()
            expected_sha = row.get("_candidate_sha256")
            expected_bytes = row.get("_candidate_bytes")
            if (expected_sha and digest != expected_sha) or \
                    (expected_bytes is not None and len(data) != expected_bytes):
                row["materialization_status"] = "source_changed"
                row["payload_complete"] = False
                continue
        digest = hashlib.sha256(data).hexdigest()
        if kind == "artifact":
            # The plan already namespaces exact source identities, including
            # not-yet-readable paths. Duplicate declarations share one file.
            member = row["_preferred_member"]
            if prior and prior.get("materialized_sha256") != digest:
                raise ExportBlocked(STATUS_BLOCKED_PACKAGE,
                                    "one artifact identity yielded conflicting bytes")
            stamp = row.get("turn_completed_at") or row.get("source_reference_at")
            epoch = _iso_epoch(stamp)
            row["payload_mtime_ns"] = (int(epoch * 1000000000) if epoch is not None
                                       else row.get("source_mtime_ns", 0))
            row["payload_time_source"] = (
                "turn_completed_at" if row.get("turn_completed_at") else
                "source_reference_at" if row.get("source_reference_at") else
                "source_file_mtime" if row.get("source_mtime_ns") is not None else
                "unavailable_fixed_epoch")
            if row.get("legacy_carry_forward"):
                row["payload_mtime_ns"] = row["legacy_carry_forward"]["archived_payload_mtime_ns"]
                row["payload_time_source"] = "legacy_archived_file_mtime"
            used.add(member)
        else:
            member = _deduplicated_member(
                row["_preferred_member"],
                "%s:%s:%s" % (kind, row.get("path_normalized"), row.get("role")),
                used,
            )
        row["_materialized_bytes"] = data
        row["materialized_member"] = member
        row["materialized_bytes"] = len(data)
        row["materialized_sha256"] = digest
        if kind == "artifact":
            # Retain the v1 receipt fields while adding package semantics.
            row["bytes"] = len(data)
            row["sha256"] = digest
        if row["materialization_status"] == "ready":
            row["materialization_status"] = "materialized"
        if not prior:
            consumed += len(data)
        if kind == "artifact":
            materialized_artifacts[member] = row
    package_complete = all(
        row.get("materialization_status") == "materialized"
        and row.get("payload_complete") is True
        for _, row in rows
    )
    return {
        "package_complete": package_complete,
        "materialized_payload_bytes": consumed,
        "max_package_bytes": max_package_bytes,
    }


def public_payload_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value for key, value in row.items()
        if key != "text" and not key.startswith("_")
    }


def _payload_evidence_key(kind: str, row: Dict[str, Any]
                          ) -> Tuple[Any, ...]:
    return (
        kind,
        row.get("filename"),
        row.get("path_normalized"),
        row.get("role") if kind == "artifact" else None,
    )


def _replace_payload_materialization(current: Dict[str, Any],
                                     carried: Dict[str, Any]) -> None:
    """Replace payload bytes/provenance without rewriting snapshot evidence."""
    payload_keys = (
        "materialization_status", "materialization_source",
        "materialized_member", "materialized_bytes", "materialized_sha256",
        "payload_complete", "bytes", "sha256", "_materialized_bytes",
        "_preferred_member", "_managed_v2_carry_forward",
        "_legacy_member_path", "_candidate_path", "_candidate_sha256",
        "_candidate_bytes",
    )
    for name in payload_keys:
        current.pop(name, None)
    for name in payload_keys:
        if name in carried:
            current[name] = carried[name]


def apply_managed_v2_payload_carry_forward(
        attachments: List[dict], artifacts: List[dict],
        carry_forward: Sequence[Dict[str, Any]]) -> None:
    """Use already-verified carry-forward payloads when current evidence is
    absent.

    This function never discovers or validates those payloads itself. Any
    caller supplying them must already have checked receipt, manifest,
    package integrity and immutable identity; a caller that cannot is
    expected to supply nothing. v2.0 requires an unchanged source and
    prefers current bytes. v2.1 historical versions remain separate from
    current source evidence.
    """
    collections = {"attachment": attachments, "artifact": artifacts}
    for supplied in carry_forward:
        kind = supplied.get("kind")
        if kind not in collections:
            raise ExportBlocked(
                STATUS_BLOCKED_PACKAGE,
                "managed v2 carry-forward contained an invalid payload kind",
            )
        row = dict(supplied)
        row.pop("kind", None)
        if row.get("legacy_carry_forward"):
            # Keep the attested historical version even when a live source
            # at the same pathname now holds different bytes. It has no
            # producing-turn proof and must not inherit a current binding.
            collections[kind].append(row)
            continue
        key = _payload_evidence_key(kind, row)
        matches = [
            current for current in collections[kind]
            if _payload_evidence_key(kind, current) == key
        ]
        if len(matches) > 1:
            raise ExportBlocked(
                STATUS_BLOCKED_PACKAGE,
                "managed v2 carry-forward payload identity was ambiguous",
            )
        if matches:
            current = matches[0]
            current_status = current.get("materialization_status")
            if current_status == "blocked_privacy":
                raise ExportBlocked(
                    STATUS_BLOCKED_PRIVACY,
                    "current attachment privacy refusal forbids legacy fallback",
                )
            if isinstance(current_status, str) \
                    and current_status.startswith("blocked_"):
                raise ExportBlocked(
                    STATUS_BLOCKED_PACKAGE,
                    "current payload safety refusal forbids legacy fallback",
                )
            current_has_bytes = (
                isinstance(current.get("_materialized_bytes"), bytes)
                or bool(current.get("_candidate_path"))
            )
            if current_status in ("ready", "materialized") \
                    and current.get("payload_complete") is True \
                    and current_has_bytes:
                continue
            if current_status == "partial" and current_has_bytes:
                if row.get("payload_complete") is True:
                    _replace_payload_materialization(current, row)
                continue
            current.update(row)
        else:
            collections[kind].append(row)


# --------------------------------------------------------------------------
# Best-effort export-time Git provenance (read-only)
# --------------------------------------------------------------------------

GIT_READ_ONLY_COMMANDS = (
    ("toplevel", ("rev-parse", "--show-toplevel")),
    ("head", ("rev-parse", "HEAD")),
    ("branch", ("branch", "--show-current")),
    ("remote_url", ("remote", "get-url", "origin")),
    ("default_branch_head", ("rev-parse", "origin/HEAD")),
    ("baseline_main", ("rev-parse", "origin/main")),
    ("merge_base_with_main", ("merge-base", "origin/main", "HEAD")),
)


def git_provenance(workspace: Optional[str], privacy: Privacy,
                   enabled: bool) -> Dict[str, Any]:
    result: Dict[str, Any] = {"probed": False, "evidence_time": "export_time"}
    if not enabled or not workspace:
        result["reason"] = "disabled" if not enabled else "no_recorded_workspace"
        return result
    root = Path(workspace).expanduser()
    if not root.is_dir():
        result["reason"] = "recorded_workspace_absent_at_export_time"
        return result
    result["probed"] = True
    for name, args in GIT_READ_ONLY_COMMANDS:
        try:
            completed = subprocess.run(
                ["git", "-C", str(root)] + list(args),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            result[name] = None
            continue
        if completed.returncode != 0:
            result[name] = None
            continue
        value = completed.stdout.decode("utf-8", errors="replace").strip()
        result[name] = value or None
    remote = result.get("remote_url")
    result["repo_identity"] = privacy.repo_identity(remote)
    if result.get("toplevel"):
        result["toplevel"] = privacy.clean(result["toplevel"])
    result.pop("remote_url", None)
    return result


# --------------------------------------------------------------------------
# Markdown rendering
# --------------------------------------------------------------------------

BODY_MARKER = "# Codex Conversation Export"


def _fence(text: str) -> str:
    """Fence long enough to contain text that itself contains code fences."""
    longest = max((len(run) for run in re.findall(r"`{3,}", text or "")), default=0)
    return "`" * max(3, longest + 1)


def _quote(text: str) -> str:
    """Render exact provider text as a blockquote without altering the text."""
    lines = (text or "").rstrip("\n").split("\n")
    return "\n".join("> " + line if line else ">" for line in lines)


def _time_only(iso_text: Optional[str]) -> str:
    if not iso_text or len(iso_text) < 19:
        return iso_text or "?"
    return iso_text[11:19]


def render_markdown(context: Dict[str, Any]) -> str:
    model: RolloutModel = context["model"]
    privacy: Privacy = context["privacy"]
    receipt = context["receipt"]
    session = receipt["session"]
    out: List[str] = []
    add = out.append

    add("<!-- codex-conversation-export exporter_version=%s -->" % EXPORTER_VERSION)
    add("<!-- generated_at=%s (excluded from markdown_body_sha256) -->"
        % context["generated_at"])
    add(BODY_MARKER)
    add("")
    add("Export status: `%s`" % receipt["export_status"])
    add("")

    # -- Session -----------------------------------------------------------
    add("## Session")
    add("")
    rows = [
        ("session id", session.get("session_id")),
        ("source rollout", receipt["source"]["rollout_tilde_normalized"]),
        ("source class", receipt["source"]["source_class"]),
        ("Codex CLI version", session.get("cli_version")),
        ("originator / source", "%s / %s" % (session.get("originator"),
                                             session.get("source"))),
        ("model", session.get("model")),
        ("reasoning effort", session.get("reasoning_effort")),
        ("reasoning summary mode", session.get("reasoning_summary")),
        ("workspace", session.get("workspace")),
        ("repo (session time)", session.get("repo")),
        ("branch (session time)", session.get("branch")),
        ("HEAD (session time)", session.get("head")),
        ("turns", "%d recorded / %d completed"
         % (session.get("turn_count") or 0, session.get("completed_turn_count") or 0)),
        ("session window", "%s → %s" % (session.get("started_at"),
                                        session.get("last_activity_at"))),
    ]
    for label, value in rows:
        if value not in (None, "", "None / None"):
            add("- **%s**: %s" % (label, value))
    git = receipt.get("git_provenance") or {}
    if git.get("probed"):
        add("- **export-time Git evidence**: repo `%s`, branch `%s`, HEAD `%s`"
            % (git.get("repo_identity"), git.get("branch"), git.get("head")))
        if git.get("merge_base_with_main"):
            add("  - baseline `origin/main` %s, merge-base %s"
                % (git.get("baseline_main"), git.get("merge_base_with_main")))
        if session.get("head") and git.get("head") \
                and git.get("head") != session.get("head"):
            add("  - export-time HEAD differs from the session-time HEAD; the "
                "session-time values above remain the recorded truth")
    elif git.get("reason"):
        add("- **export-time Git evidence**: not available (%s)" % git["reason"])
    add("")

    for turn_index, turn_id in enumerate(model.turn_order, start=1):
        completed = turn_id in model.turn_completed
        detail = model.turn_completed.get(turn_id) or {}
        duration = detail.get("duration_ms")
        abort = model.turn_aborted.get(turn_id)
        add("- Turn %d `%s` — completed=%s%s%s"
            % (turn_index, turn_id[:8], "true" if completed else "false",
               ", duration=%.1fs" % (duration / 1000.0) if duration else "",
               ", aborted=%s" % abort["reason"] if abort else ""))
    add("")

    # -- Owner messages ----------------------------------------------------
    add("## Owner messages")
    add("")
    if not model.owner_messages:
        add("_No mechanically typed Owner message is present in this rollout._")
        add("")
    for message in model.owner_messages:
        add("### Turn %s · %s" % (model.turn_index(message["turn_id"]) or "?",
                                  message["timestamp"]))
        add("")
        envelope = message.get("attachment_envelope")
        if envelope:
            add("_Codex composed an attachment wrapper around this request "
                "(%d manifest entry/entries, %d bytes of envelope). The wrapper "
                "is recorded under file / artifact provenance; only the Owner's "
                "own request text is quoted here._"
                % (envelope["manifest_entry_count"], envelope["envelope_bytes"]))
            add("")
        add(_quote(privacy.clean(message.get("owner_text", message["text"]))))
        add("")
    omitted = getattr(model, "persisted_user_context_visible", [])
    if omitted:
        add("_Persisted user-role context not authored by the Owner "
            "(injected envelopes; omitted from this export):_")
        for entry in omitted:
            tags = ", ".join(entry.get("envelope_tags") or []) or "untagged"
            add("- turn %s · %d bytes · %s"
                % (model.turn_index(entry["turn_id"]) or "?", entry["bytes"], tags))
        add("")

    # -- Progress & final --------------------------------------------------
    add("## Progress & final")
    add("")
    for message in model.assistant_messages:
        phase = message.get("phase") or "message"
        heading = "final answer" if phase == "final_answer" else "progress"
        add("### Turn %s · %s · %s"
            % (model.turn_index(message["turn_id"]) or "?",
               _time_only(message["timestamp"]), heading))
        add("")
        add(_quote(privacy.clean(message["text"])))
        add("")

    # -- Visible reasoning -------------------------------------------------
    reasoning = context["reasoning"]
    add("## Selected visible reasoning")
    add("")
    counts = reasoning["counts"]
    add("Provider-visible reasoning summaries only, selected at structural "
        "decision points by policy `%s`. Exact text, no paraphrase. "
        "%d of %d visible records kept (%d of %d summary blocks). "
        "%d reasoning records exposed no summary text and are counted only."
        % (counts["selection_policy_version"],
           counts["visible_reasoning_records_selected"],
           counts["visible_reasoning_records_source"],
           counts["visible_reasoning_blocks_selected"],
           counts["visible_reasoning_blocks_source"],
           counts["opaque_reasoning_count"]))
    add("")
    for item in reasoning["selected"]:
        add("- **Turn %s · %s · %s**"
            % (model.turn_index(item["turn_id"]) or "?",
               _time_only(item["timestamp"]), "/".join(item["reasons"])))
        for text in item["texts"]:
            add("  - %s" % privacy.clean(text).replace("\n", " ").strip())
    add("")

    # -- Tool timeline -----------------------------------------------------
    timeline = context["timeline"]
    add("## Tool / command timeline")
    add("")
    tcounts = timeline["counts"]
    add("%d recorded tool calls → %d logical invocation rows "
        "(%d folded into %d structural-poll windows)."
        % (tcounts["tool_calls_source"], tcounts["logical_tool_rows_exported"],
           tcounts["tool_rows_folded"], tcounts["fold_windows"]))
    add("Durable CommandExecution records: %d = %d paired + %d unpaired; "
        "all %d unpaired records are rendered explicitly."
        % (tcounts["command_execution_records_source"],
           tcounts["command_execution_records_paired"],
           tcounts["command_execution_records_unpaired"],
           tcounts["command_execution_records_rendered_unpaired"]))
    add("Decoded command-action exit status: %s resolved. Visible failures: "
        "%d total = %d tool-action + %d unpaired CommandExecution."
        % (tcounts["exit_status_resolved"], tcounts["failed_actions"],
           tcounts["failed_tool_actions"],
           tcounts["command_execution_records_unpaired_failed"]))
    add("")
    current_turn = None
    for row in timeline["rows"]:
        turn_index = model.turn_index(row["turn_id"])
        if turn_index != current_turn:
            current_turn = turn_index
            add("")
            add("**Turn %s**" % (turn_index or "?"))
            add("")
        if row["kind"] == "folded_window":
            families = ", ".join(
                "%s ×%d" % (item["family"], item["count"])
                for item in row["families"]
            )
            add("- `%s–%s` collapsed ×%d — %s"
                % (_time_only(row["timestamp"]), _time_only(row["timestamp_end"]),
                   row["count"], families))
            continue
        marks = []
        if row["kind"] == "command":
            if row["exit_source"] == "unresolved":
                marks.append("exit=unknown")
            elif row["exit_code"] is not None:
                marks.append("exit=%s" % row["exit_code"])
        elif row["kind"] == "command_invocation":
            marks.append("source=tool invocation / template-unresolved")
        elif row["kind"] == "command_execution_unpaired":
            marks.append("source=CommandExecution / unpaired")
            if row.get("cwd"):
                marks.append("cwd=%s" % row["cwd"])
            if row["exit_code"] is not None:
                marks.append("exit=%s" % row["exit_code"])
            if row.get("exec_status"):
                marks.append("status=%s" % row["exec_status"])
        elif row.get("exec_status"):
            marks.append(row["exec_status"])
        if row["wall_time_seconds"] is not None:
            marks.append("%.1fs" % row["wall_time_seconds"])
        suffix = " _(%s)_" % ", ".join(marks) if marks else ""
        flag = "**FAILED** " if row["failed"] else ""
        add("- `%s` %s`%s`%s"
            % (_time_only(row["timestamp"]), flag, row["label"], suffix))
        if row["failed"] and row["stderr"]:
            add("  - stderr: `%s`" % row["stderr"])
    add("")

    # -- File / artifact provenance ---------------------------------------
    add("## File / artifact provenance")
    add("")
    if model.file_changes:
        add("### Recorded file changes")
        add("")
        add("V1 guarantees path and change type. Content-level provenance is not "
            "promised by the rollout and is not reconstructed here.")
        add("")
        for change in model.file_changes:
            for entry in change["changes"]:
                add("- `%s` — %s (turn %s, %s)"
                    % (privacy.clean(entry["path"]), entry["change_type"],
                       model.turn_index(change["turn_id"]) or "?",
                       _time_only(change["timestamp"])))
        add("")
    attachments = context["attachments"]
    if attachments:
        add("### Input attachments")
        add("")
        for item in attachments:
            add("- `%s` — provenance `%s`, snapshot source `%s`%s"
                % (item["path_normalized"], item["provenance"],
                   item["snapshot_source"],
                   ", %d bytes" % item["snapshot_bytes"]
                   if item["snapshot_bytes"] else ""))
            if item.get("current_file_access") == CURRENT_FILE_PRESENT_READABLE:
                add("  - export-time file: %d bytes, sha256 `%s`"
                    % (item["current_file_bytes"], item["current_file_sha256"]))
            elif item.get("current_file_access") in (
                    CURRENT_FILE_PRESENT_UNREADABLE,
                    CURRENT_FILE_ACCESS_UNAVAILABLE):
                add("  - export-time file corroboration: unavailable (`%s`)"
                    % item["current_file_access"])
            add("  - snapshot_complete: `%s` — %s"
                % (item.get("snapshot_complete", SNAPSHOT_UNPROVEN),
                   item.get("snapshot_completeness_basis", "unknown")))
            add("  - package payload: `%s` via `%s`%s"
                % (item.get("materialization_status"),
                   item.get("materialization_source") or "none",
                   ", member `%s`" % item["materialized_member"]
                   if item.get("materialized_member") else ""))
            if item.get("snapshot_matches_current_file") is True:
                add("  - recovered snapshot is byte-identical to the export-time file")
            elif item.get("snapshot_source") == "current_file_at_export_time":
                add("  - snapshot re-read at export time; send-time byte identity "
                    "is not proven")
        add("")
        for item in attachments:
            if not item.get("text"):
                continue
            complete = item.get("snapshot_complete", SNAPSHOT_UNPROVEN)
            add("#### %s — `%s`"
                % ("Task-definition snapshot" if complete == SNAPSHOT_COMPLETE
                   else "Partial persisted snapshot", item["filename"]))
            add("")
            add("Source: `%s` (%s%s), sha256 `%s`."
                % (item["snapshot_source"], item.get("snapshot_assembly", "n/a"),
                   ", truncated" if item["snapshot_truncated"] else "",
                   item["snapshot_sha256"]))
            add("")
            if complete == SNAPSHOT_COMPLETE:
                add("Completeness: `true` — %s."
                    % item.get("snapshot_completeness_basis", ""))
            else:
                add("**Completeness: `%s` — %s.** This text is shown because it "
                    "is useful evidence, not because the whole attachment was "
                    "recovered."
                    % (complete, item.get("snapshot_completeness_basis", "")))
            if item.get("snapshot_segments"):
                add("")
                for command in item["snapshot_segments"]:
                    add("- recovered from: `%s`" % truncate(command, 160))
            add("")
            fence = _fence(item["text"])
            add("%stext" % fence)
            add(item["text"].rstrip("\n"))
            add(fence)
            add("")
    artifacts = context["artifacts"]
    add("### Output artifacts")
    add("")
    add("- **review_bundle_present**: `%s`"
        % ("true" if receipt["artifacts"]["review_bundle_present"] else "false"))
    if artifacts:
        for item in artifacts:
            if item.get("materialization_status") == "materialized":
                add("- `%s` — role `%s`, %d bytes, sha256 `%s`, member `%s`"
                    % (item["path_normalized"], item["role"], item["bytes"],
                       item["sha256"], item["materialized_member"]))
            else:
                add("- `%s` — role `%s`, package payload `%s`"
                    % (item["path_normalized"], item["role"],
                       item.get("materialization_status", "unavailable")))
    else:
        add("- No artifact was declared for this export "
            "(artifacts are never discovered by scanning).")
    add("")

    # -- Receipt summary ---------------------------------------------------
    add("## Export receipt summary")
    add("")
    add("```text")
    add("exporter_version           %s" % receipt["exporter_version"])
    add("export_status              %s" % receipt["export_status"])
    add("source_sha256_pre          %s" % receipt["source"]["rollout_sha256_pre"])
    add("source_sha256_post         %s" % receipt["source"]["rollout_sha256_post"])
    add("source_changed             %s" % receipt["source"]["source_changed_during_export"])
    add("source_bytes               %s" % receipt["source"]["rollout_bytes"])
    for key, value in sorted(receipt["counts"].items()):
        add("%-26s %s" % (key, value))
    add("unknown_record_types       %s" % json.dumps(receipt["schema"]["unknown_record_type_counts"], sort_keys=True))
    add("unknown_payload_types      %s" % json.dumps(receipt["schema"]["unknown_payload_type_counts"], sort_keys=True))
    add("unparsable_lines           %s" % receipt["schema"]["unparsable_lines"])
    add("normalization_enabled      %s" % receipt["privacy"]["normalization_enabled"])
    add("redactions                 %s" % json.dumps(receipt["privacy"]["redactions"], sort_keys=True))
    add("drops                      %s" % json.dumps(receipt["privacy"]["drops"], sort_keys=True))
    add("hidden_chain_of_thought_exported   %s"
        % receipt["reasoning_boundary"]["hidden_chain_of_thought_exported"])
    add("opaque_reasoning_exported          %s"
        % receipt["reasoning_boundary"]["opaque_reasoning_exported"])
    add("missing_reasoning_reconstructed    %s"
        % receipt["reasoning_boundary"]["missing_reasoning_reconstructed"])
    add("```")
    add("")
    if receipt["warnings"]:
        add("### Warnings")
        add("")
        for warning in receipt["warnings"]:
            add("- %s" % warning)
        add("")
    add("_The Conversation Export explains how and why this Codex run proceeded. "
        "It does not replace a Review Bundle, which evidences whether the produced "
        "work is correct._")
    return "\n".join(out) + "\n"


def markdown_body(markdown: str) -> str:
    index = markdown.find(BODY_MARKER)
    return markdown[index:] if index >= 0 else markdown


# --------------------------------------------------------------------------
# Receipt
# --------------------------------------------------------------------------


def resolve_status(unknown: bool, unparsable: bool, partial: bool) -> str:
    if unknown or unparsable:
        return STATUS_DEGRADED
    if partial:
        return STATUS_PARTIAL
    return STATUS_COMPLETE


def build_receipt(context: Dict[str, Any]) -> Dict[str, Any]:
    model: RolloutModel = context["model"]
    privacy: Privacy = context["privacy"]
    reasoning = context["reasoning"]["counts"]
    timeline = context["timeline"]["counts"]
    source = context["source"]

    meta = model.session_meta
    git = as_dict(meta.get("git"))
    first_turn = next(iter(model.turn_contexts.values()), {})
    workspace_roots = [privacy.clean(root)
                       for root in as_list(first_turn.get("workspace_roots"))]
    partial_turns = [turn for turn in model.turn_order
                     if turn not in model.turn_completed]
    unknown_types = bool(model.unknown_record_type_counts) or \
        bool(model.unknown_payload_type_counts)
    retryable_final_artifacts = retryable_final_answer_artifact_count(
        context["classified_final_artifact_candidates"],
        context["artifacts"],
    )

    duplicates_deduped = sum(
        value for key, value in model.duplicate_breakdown.items()
        if not key.endswith("unmatched")
    )
    base_instructions = as_dict(meta.get("base_instructions"))
    base_text = base_instructions.get("text") or ""

    receipt: Dict[str, Any] = {
        "exporter_version": EXPORTER_VERSION,
        "export_status": context["export_status"],
        "package": {
            "package_schema_version": PACKAGE_SCHEMA_VERSION,
            "package_dirname": context["package"]["package_dirname"],
            "package_relative_path":
                context["package"]["package_relative_path"],
            "display_title": context["package"]["display_title"],
            "display_title_source":
                context["package"]["display_title_source"],
            "project_bucket": context["package"]["project_bucket"],
            "project_bucket_source":
                context["package"]["project_bucket_source"],
            "rollout_coordinate_sha256":
                context["package"]["rollout_coordinate_sha256"],
            "package_complete": context["package"]["package_complete"],
            "materialized_payload_bytes":
                context["package"]["materialized_payload_bytes"],
            "max_package_bytes": context["package"]["max_package_bytes"],
            "canonical_representation": "project_grouped_session_folder",
            "handoff_zip_role": "derived_transfer_artifact",
            "handoff_zip_default_generation": "disabled",
            "handoff_zip_on_demand": "available",
        },
        "source": {
            "session_id": meta.get("session_id") or meta.get("id"),
            "stable_rollout_identity": context["rollout_identity"],
            "stable_output_basename": context["basename"],
            "source_class": source["source_class"],
            "rollout_tilde_normalized": source["rollout_tilde_normalized"],
            "rollout_bytes": source["pre"]["bytes"],
            "rollout_sha256_pre": source["pre"]["sha256"],
            "rollout_sha256_post": source["post"]["sha256"],
            "stability_gate": source.get("stability_gate"),
            "rollout_mtime_ns_pre": source["pre"]["mtime_ns"],
            "rollout_mtime_ns_post": source["post"]["mtime_ns"],
            "source_changed_during_export": source["changed"],
            "source_opened_for_write": False,
        },
        "session": {
            "session_id": meta.get("session_id") or meta.get("id"),
            "cli_version": meta.get("cli_version"),
            "originator": meta.get("originator"),
            "source": meta.get("source"),
            "thread_source": meta.get("thread_source"),
            "model_provider": meta.get("model_provider"),
            "history_mode": meta.get("history_mode"),
            "model": first_turn.get("model") or model.thread_settings.get("model"),
            "reasoning_effort": first_turn.get("effort")
            or model.thread_settings.get("reasoning_effort"),
            "reasoning_summary": model.thread_settings.get("reasoning_summary"),
            "approval_policy": first_turn.get("approval_policy"),
            "sandbox_policy": as_dict(first_turn.get("sandbox_policy")).get("type"),
            "workspace": privacy.clean(meta.get("cwd") or ""),
            "workspace_roots": workspace_roots,
            "repo": privacy.repo_identity(git.get("repository_url")),
            "branch": git.get("branch"),
            "head": git.get("commit_hash"),
            "started_at": meta.get("timestamp"),
            "last_activity_at": context["last_activity_at"],
            "turn_ids": list(model.turn_order),
            "turn_count": len(model.turn_order),
            "completed_turn_count": len(model.turn_completed),
            "partial_turn_ids": partial_turns,
            "aborted_turns": [
                {"turn_id": turn, "reason": model.turn_aborted[turn]["reason"]}
                for turn in model.turn_order if turn in model.turn_aborted
            ],
            "turn_outcome_conflicts": [
                model.turn_outcome_conflicts[turn] for turn in model.turn_order
                if turn in model.turn_outcome_conflicts
            ],
            "models": sorted(
                set(str(turn.get("model")) for turn in model.turn_contexts.values()
                    if turn.get("model"))
            ),
            "efforts": sorted(
                set(str(turn.get("effort")) for turn in model.turn_contexts.values()
                    if turn.get("effort"))
            ),
            "base_instructions_bytes": len(base_text.encode("utf-8")),
            "base_instructions_sha256": sha256_text(base_text) if base_text else None,
            "base_instructions_provenance_model":
                as_dict(base_instructions.get("provenance")).get("model"),
            "dynamic_tool_groups": [
                {"name": group.get("name"),
                 "tool_count": len(as_list(group.get("tools")))}
                for group in as_list(meta.get("dynamic_tools"))
                if isinstance(group, dict)
            ],
            "aggregate_token_usage": model.token_usage or None,
        },
        "schema": {
            "record_type_counts": dict(model.record_type_counts),
            "payload_type_counts": dict(model.payload_type_counts),
            "item_type_counts": dict(model.item_type_counts),
            "unknown_record_type_counts": dict(model.unknown_record_type_counts),
            "unknown_payload_type_counts": dict(model.unknown_payload_type_counts),
            "unknown_item_type_counts": dict(model.unknown_item_type_counts),
            "unparsable_lines": model.unparsable_lines,
            "total_lines": model.total_lines,
        },
        "counts": {
            "owner_messages_exported": len(model.owner_messages),
            "owner_messages_attachment_envelope_separated":
                model.attachment_envelopes_separated,
            "attachment_wrappers_unparsed_kept_as_context":
                model.attachment_wrappers_unparsed,
            "assistant_progress_exported": sum(
                1 for message in model.assistant_messages
                if message.get("phase") != "final_answer"
            ),
            "assistant_final_exported": sum(
                1 for message in model.assistant_messages
                if message.get("phase") == "final_answer"
            ),
            "developer_messages_omitted": len(model.developer_messages),
            "persisted_user_context_omitted":
                len(getattr(model, "persisted_user_context_visible", [])),
            "reasoning_records_source": reasoning["reasoning_records_source"],
            "visible_reasoning_records_source":
                reasoning["visible_reasoning_records_source"],
            "visible_reasoning_records_selected":
                reasoning["visible_reasoning_records_selected"],
            "visible_reasoning_blocks_source":
                reasoning["visible_reasoning_blocks_source"],
            "visible_reasoning_blocks_selected":
                reasoning["visible_reasoning_blocks_selected"],
            "visible_reasoning_records_omitted":
                reasoning["visible_reasoning_records_omitted"],
            "opaque_reasoning_count": reasoning["opaque_reasoning_count"],
            "tool_calls_source": timeline["tool_calls_source"],
            "command_execution_records_source":
                timeline["command_execution_records_source"],
            "command_execution_records_paired":
                timeline["command_execution_records_paired"],
            "command_execution_records_unpaired":
                timeline["command_execution_records_unpaired"],
            "command_execution_records_unpaired_failed":
                timeline["command_execution_records_unpaired_failed"],
            "command_execution_records_rendered_unpaired":
                timeline["command_execution_records_rendered_unpaired"],
            "command_execution_accounting_closed":
                timeline["command_execution_accounting_closed"],
            "command_execution_rendering_closed":
                timeline["command_execution_rendering_closed"],
            "logical_tool_rows_exported": timeline["logical_tool_rows_exported"],
            "timeline_rows_exported": timeline["timeline_rows_exported"],
            "tool_rows_folded": timeline["tool_rows_folded"],
            "fold_windows": timeline["fold_windows"],
            "shell_command_folding": timeline["shell_command_folding"],
            "empty_stdin_poll_folding": timeline["empty_stdin_poll_folding"],
            "wait_folding": timeline["wait_folding"],
            "fold_policy_version": timeline["fold_policy_version"],
            "stdin_empty_polls": timeline["stdin_empty_polls"],
            "stdin_non_empty_inputs": timeline["stdin_non_empty_inputs"],
            "stdin_unproven_inputs": timeline["stdin_unproven_inputs"],
            "wait_actions": timeline["wait_actions"],
            "failed_actions": timeline["failed_actions"],
            "failed_tool_actions": timeline["failed_tool_actions"],
            "exit_status_resolved": timeline["exit_status_resolved"],
            "file_change_records": len(model.file_changes),
            "file_change_paths": sum(len(change["changes"])
                                     for change in model.file_changes),
            "context_compactions": len(model.context_compactions),
            "duplicate_logical_records_detected": sum(
                model.duplicate_breakdown.values()
            ),
            "duplicate_logical_records_deduped": duplicates_deduped,
            "unbound_records": model.unbound_records,
            "mcp_tool_call_records_recognized": model.mcp_tool_calls_recognized,
            "turn_aborts_unbound": len(model.unbound_turn_aborts),
            "turn_outcome_conflicts": len(model.turn_outcome_conflicts),
        },
        "duplicate_breakdown": dict(model.duplicate_breakdown),
        "reasoning_selection": reasoning,
        "tool_timeline": timeline,
        "privacy": {
            "normalization_enabled": privacy.normalize_enabled,
            "redactions": dict(privacy.redactions),
            "redactions_total": sum(privacy.redactions.values()),
            "drops": dict(privacy.drops),
            "drops_total": sum(privacy.drops.values()),
            "always_dropped_categories": [
                "rate_limit_and_credit_telemetry",
                "account_identity",
                "dynamic_tool_schemas",
                "base_instructions_text",
                "world_state_envelopes",
                "injected_user_context_envelope",
                "file_change_content",
                "file_change_unified_diff",
            ],
            "sanitization_rescan_residuals": context.get("residuals", {}),
        },
        "reasoning_boundary": {
            "visible_reasoning_summary_if_exposed": True,
            "hidden_chain_of_thought_promised": False,
            "hidden_chain_of_thought_exported": False,
            "opaque_reasoning_exported": False,
            "missing_reasoning_reconstructed": False,
            "opaque_reasoning_count": reasoning["opaque_reasoning_count"],
        },
        "attachments": [public_payload_row(item)
                        for item in context["attachments"]],
        "artifacts": {
            "artifact_provenance_version": ARTIFACT_PROVENANCE_VERSION,
            "candidate_provenance": context["artifact_candidates"],
            "artifact_count": len(context["artifacts"]),
            "review_bundle_present": context["review_bundle_present"],
            "items": [public_payload_row(item)
                      for item in context["artifacts"]],
            "discovery": "exact_paths_only_no_filesystem_scan",
            "retryable_exact_final_artifact_candidates":
                retryable_final_artifacts,
            # Compatibility field retained for existing v2.1 consumers. Its
            # count mirrors the clearer retryable field so older readers also
            # re-evaluate post-READY materialization failures.
            "missing_exact_final_path_candidates":
                retryable_final_artifacts,
        },
        "git_provenance": context["git_provenance"],
        "partial_state": {
            "all_turns_completed": not partial_turns,
            "partial_turn_count": len(partial_turns),
            "aborted_turn_count": len(model.turn_aborted),
            "final_answer_present": any(
                message.get("phase") == "final_answer"
                for message in model.assistant_messages
            ),
            "final_answer_invented": False,
        },
        "warnings": list(model.warnings),
    }
    if unknown_types:
        receipt["warnings"].append(
            "unknown record/payload types present; export marked degraded"
        )
    if model.unparsable_lines:
        receipt["warnings"].append(
            "%d unparsable JSONL line(s) skipped and counted"
            % model.unparsable_lines
        )
    return receipt


def deterministic_receipt_core(receipt: Dict[str, Any]) -> Dict[str, Any]:
    core = dict(receipt)
    core.pop("volatile", None)
    return core


def sanitization_rescan(text: str) -> Dict[str, int]:
    """Fail-closed check that no high-confidence credential survived rendering."""
    residuals: Dict[str, int] = {}
    for name, pattern in CREDENTIAL_SHAPE_RULES:
        hits = len(pattern.findall(text))
        if hits:
            residuals[name] = hits
    return residuals


# --------------------------------------------------------------------------
# Export orchestration
# --------------------------------------------------------------------------


def configure_privacy(privacy: Privacy, model: RolloutModel) -> None:
    home = str(Path.home())
    meta = model.session_meta
    cwd = meta.get("cwd")
    if isinstance(cwd, str) and cwd:
        privacy.add_literal("workspace", cwd, "$WORKSPACE")
    extra = 0
    for turn in model.turn_contexts.values():
        for root in as_list(turn.get("workspace_roots")):
            if not isinstance(root, str) or root == cwd:
                continue
            if root.startswith(os.path.join(home, ".codex")):
                privacy.add_literal("codex_state", root, "$CODEX_STATE")
                continue
            extra += 1
            privacy.add_literal("workspace_root", root, "$WORKSPACE_ROOT%d" % extra)
    for name in ATTACHMENT_ROOTS:
        privacy.add_literal("attachment_root", os.path.join(home, name),
                            "$ATTACHMENT" if name == "Downloads"
                            else "$%s" % name.upper())
    privacy.add_literal("codex_state", os.path.join(home, ".codex"), "$CODEX_STATE")
    privacy.add_literal("home", home, "~")


def last_activity_timestamp(model: RolloutModel) -> Optional[str]:
    stamps = []
    for detail in model.turn_completed.values():
        if detail.get("timestamp"):
            stamps.append(detail["timestamp"])
    for message in model.assistant_messages:
        if message.get("timestamp"):
            stamps.append(message["timestamp"])
    for action in model.tool_actions:
        if action.get("timestamp"):
            stamps.append(action["timestamp"])
    if not stamps:
        return model.session_meta.get("timestamp")
    return max(stamps)


def output_basename(model: RolloutModel, privacy: Privacy,
                    label: Optional[str], source_path: Path) -> str:
    # Compatibility wrapper for callers/tests that already hold a parsed model.
    # Last activity is deliberately absent from the stable identity.
    del privacy
    return stable_output_basename(model.session_meta, source_path, label)


def run_export(options: argparse.Namespace) -> Dict[str, Any]:
    source_path = resolve_source(options.rollout, options.session_id)
    pre = source_identity(source_path)

    privacy = Privacy(normalize=not options.no_normalize)
    model = RolloutModel(privacy)
    model.load(source_path)
    configure_privacy(privacy, model)

    session_id = canonical_session_uuid(
        model.session_meta.get("session_id") or model.session_meta.get("id")
    )
    if getattr(options, "session_index_preloaded", False):
        thread_name = getattr(options, "session_thread_name", None)
    else:
        thread_name = resolve_session_thread_names(
            [session_id] if session_id else []
        ).get(session_id or "")

    reasoning = select_visible_reasoning(model, options.reasoning_cap)
    timeline = build_timeline(model, privacy, options.command_chars)
    attachments = build_attachments(model, privacy,
                                    options.max_attachment_snapshot_bytes)
    classified_final_candidates = classify_final_answer_artifact_candidates(
        model
    )
    artifacts, review_bundle_present = build_artifacts(
        model, privacy, options.review_bundle, options.artifact or [],
        classified_final_candidates,
    )
    carry_forward = getattr(options, "managed_v2_carry_forward", None) or []
    apply_managed_v2_payload_carry_forward(
        attachments, artifacts, carry_forward
    )
    artifact_candidates = artifact_turn_provenance(
        model, artifacts, classified_final_candidates
    )
    package_payloads = prepare_package_payloads(
        attachments,
        artifacts,
        getattr(options, "max_package_bytes", DEFAULT_MAX_PACKAGE_BYTES),
    )
    if any(
        row.get("_managed_v2_carry_forward") is True
        and row.get("materialization_status") not in ("materialized", "partial")
        for row in attachments + artifacts
    ):
        raise ExportBlocked(
            STATUS_BLOCKED_PACKAGE,
            "verified legacy payload could not be preserved in the new package",
        )
    provenance = git_provenance(model.session_meta.get("cwd"), privacy,
                                not options.no_git_probe)

    partial = any(turn not in model.turn_completed for turn in model.turn_order)
    unknown = bool(model.unknown_record_type_counts) or \
        bool(model.unknown_payload_type_counts)
    status = resolve_status(unknown, bool(model.unparsable_lines), partial)

    last_activity = last_activity_timestamp(model)
    context: Dict[str, Any] = {
        "model": model,
        "privacy": privacy,
        "reasoning": reasoning,
        "timeline": timeline,
        "attachments": attachments,
        "artifacts": artifacts,
        "artifact_candidates": artifact_candidates,
        "classified_final_artifact_candidates": classified_final_candidates,
        "review_bundle_present": review_bundle_present,
        "git_provenance": provenance,
        "export_status": status,
        "last_activity_at": last_activity,
        "generated_at": options.generated_at,
        "residuals": {},
        "source": {
            "path": source_path,
            "source_class": source_class_of(source_path),
            "rollout_tilde_normalized": privacy.normalize_paths(str(source_path)),
            "pre": pre,
            # Rendered as the asserted-stable state; the assertion is verified
            # by the stability gate below, before anything is published.
            "post": pre,
            "changed": False,
            "stability_gate": "post_render_immediately_before_publication",
        },
    }
    context["rollout_identity"] = stable_rollout_identity(
        model.session_meta, source_path
    )
    context["basename"] = output_basename(
        model, privacy, options.label, source_path
    )
    context["package"] = stable_package_dirname(
        model.session_meta,
        [message.get("text") or "" for message in model.owner_messages],
        privacy,
        context["rollout_identity"]["coordinate_sha256"],
        label=options.label,
        thread_name=thread_name,
        completed_owner_messages=[
            message.get("text") or ""
            for message in model.owner_messages
            if message.get("turn_id") in model.turn_completed
        ],
    )
    context["package"].update(package_payloads)

    # First pass populates the redaction/normalization counters that the receipt
    # reports; the second pass renders the identical text with counting frozen.
    first_receipt = build_receipt(context)
    context["receipt"] = first_receipt
    render_markdown(context)
    privacy.freeze()

    receipt = build_receipt(context)
    context["receipt"] = receipt
    markdown = render_markdown(context)

    residuals = sanitization_rescan(markdown)
    residuals.update(sanitization_rescan(json.dumps(receipt, ensure_ascii=False)))
    residuals.update(sanitization_rescan(json.dumps(
        {
            "package": context["package"],
            "attachments": [public_payload_row(item) for item in attachments],
            "artifacts": [public_payload_row(item) for item in artifacts],
        }, ensure_ascii=False,
    )))
    if residuals:
        receipt["privacy"]["sanitization_rescan_residuals"] = residuals
        receipt["export_status"] = STATUS_BLOCKED_PRIVACY
        raise ExportBlocked(
            STATUS_BLOCKED_PRIVACY,
            "credential-shaped values survived redaction: %s" % sorted(residuals),
            receipt,
        )
    # -- Source stability gate ---------------------------------------------
    # Re-read the source identity only now: after the Markdown and the receipt
    # exist in memory and immediately before either becomes visible. Anything
    # earlier leaves a window in which the rollout can move while the export is
    # still being rendered.
    post = source_identity(source_path)
    changed = (pre["sha256"] != post["sha256"] or pre["bytes"] != post["bytes"])
    if changed:
        receipt["source"]["rollout_sha256_post"] = post["sha256"]
        receipt["source"]["rollout_bytes_post"] = post["bytes"]
        receipt["source"]["source_changed_during_export"] = True
        receipt["export_status"] = STATUS_BLOCKED_SOURCE
        raise ExportBlocked(
            STATUS_BLOCKED_SOURCE,
            "source rollout changed during export; retry once the source is stable",
            receipt,
        )

    context["markdown"] = markdown
    context["output_index"] = render_output_index(artifact_candidates, artifacts, privacy)
    if sanitization_rescan(context["output_index"]):
        raise ExportBlocked(STATUS_BLOCKED_PRIVACY, "output index failed sanitization")
    return context


def atomic_write_bytes(path: Path, payload: bytes,
                       staging_dir: Optional[Path] = None) -> None:
    """Publish one complete file with a temp file and a replace.

    The temp is always in the destination parent. Package destinations are
    already inside the private assembly tree, so failures leave no trace in
    the canonical package. ``staging_dir`` remains a compatibility argument.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_dir = path.parent
    temporary_dir.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".codex-write-",
        suffix=".tmp",
        dir=str(temporary_dir),
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary_path), str(path))
    except BaseException:
        try:
            temporary_path.unlink()
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str,
                      staging_dir: Optional[Path] = None) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), staging_dir=staging_dir)


@contextlib.contextmanager
def package_staging_area(output_dir: Path, package_dirname: str):
    """Yield a private, empty assembly directory for one publish attempt.

    Always removed on the way out, so neither a failed attempt nor a
    successful one leaves residue that a later reconciliation could mistake
    for a published package.
    """
    root = Path(output_dir) / STAGING_DIRNAME
    stage = root / safe_member_filename(package_dirname, "package",
                                        byte_limit=220)
    shutil.rmtree(str(stage), ignore_errors=True)
    stage.mkdir(parents=True, exist_ok=True)
    try:
        yield stage
    finally:
        shutil.rmtree(str(stage), ignore_errors=True)
        try:
            root.rmdir()
        except OSError:
            pass


def _json_bytes(value: Dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)
            + "\n").encode("utf-8")


def is_volatile_finder_metadata(name: str) -> bool:
    """Is this directory entry Finder display metadata rather than payload?"""
    return name == VOLATILE_FINDER_METADATA_FILENAME


def _safe_package_member(member: str) -> bool:
    if not member or member.startswith(("/", "\\")) or "\\" in member:
        return False
    parts = member.split("/")
    return all(part not in ("", ".", "..") for part in parts)


def _manifest_payload_row(item: Dict[str, Any], kind: str) -> Dict[str, Any]:
    row = {
        "kind": kind,
        "original_filename": item.get("filename"),
        "provenance": item.get("provenance"),
        "source_reference": item.get("path_normalized"),
        "materialization_status": item.get("materialization_status"),
        "materialization_source": item.get("materialization_source"),
        "completeness": bool(item.get("payload_complete")),
        "member_path": item.get("materialized_member"),
        "bytes": item.get("materialized_bytes"),
        "sha256": item.get("materialized_sha256"),
    }
    if item.get("legacy_carry_forward"):
        row["legacy_carry_forward"] = item["legacy_carry_forward"]
    if kind == "attachment":
        row["snapshot_complete"] = item.get("snapshot_complete")
    else:
        row["role"] = item.get("role")
        for key in (
                "artifact_provenance_version", "source_artifact_id", "turn_id",
                "source_turn_ordinal", "visible_turn_dir", "attribution_status",
                "attribution_source", "attribution_reason", "turn_completed_at",
                "source_reference_at", "source_final_references", "turn_label",
                "turn_label_source", "planned_member", "source_mtime_ns",
                "payload_mtime_ns", "payload_time_source"):
            row[key] = item.get(key)
    return row


def _file_row(path: str, payload: bytes) -> Dict[str, Any]:
    return {
        "path": path,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def package_files_current(package_dir: Path,
                          manifest: Optional[Dict[str, Any]] = None) -> bool:
    """Validate canonical package members without depending on a derived ZIP."""
    try:
        if manifest is None:
            manifest = json.loads(
                (package_dir / PACKAGE_MANIFEST_FILENAME).read_text("utf-8")
            )
        if not isinstance(manifest, dict):
            return False
        rows = list(as_list(manifest.get("conversation_files")))
        rows.extend(
            row for row in as_list(manifest.get("attachments"))
            if row.get("member_path")
        )
        rows.extend(
            row for row in as_list(manifest.get("artifacts"))
            if row.get("member_path")
        )
        for row in rows:
            member = row.get("path") or row.get("member_path")
            if not isinstance(member, str) or not _safe_package_member(member):
                return False
            path = package_dir / member
            if not path.is_file() or path.stat().st_size != row.get("bytes"):
                return False
            if sha256_file(path) != row.get("sha256"):
                return False
        return True
    except (OSError, UnicodeError, ValueError, KeyError):
        return False


def _canonical_package_members(manifest: Dict[str, Any]) -> List[str]:
    members = [PACKAGE_MANIFEST_FILENAME]
    rows = list(as_list(manifest.get("conversation_files")))
    rows.extend(
        row for row in as_list(manifest.get("attachments"))
        if row.get("member_path")
    )
    rows.extend(
        row for row in as_list(manifest.get("artifacts"))
        if row.get("member_path")
    )
    for row in rows:
        member = as_dict(row).get("path") or as_dict(row).get("member_path")
        if not isinstance(member, str) or not _safe_package_member(member):
            raise ExportBlocked(
                STATUS_BLOCKED_PACKAGE, "unsafe canonical package member"
            )
        if member not in members:
            members.append(member)
    return sorted(members)


def handoff_zip_current(package_dir: Path,
                        manifest: Optional[Dict[str, Any]] = None) -> bool:
    """Validate an explicitly generated current handoff ZIP when present."""
    try:
        if manifest is None:
            manifest = json.loads(
                (package_dir / PACKAGE_MANIFEST_FILENAME).read_text("utf-8")
            )
        if not isinstance(manifest, dict) or not package_files_current(
                package_dir, manifest):
            return False
        handoff = as_dict(manifest.get("handoff_zip"))
        zip_member = handoff.get("path")
        if zip_member not in (
                HANDOFF_ZIP_FILENAME, LEGACY_V2_HANDOFF_ZIP_FILENAME):
            return False
        if handoff.get("status") not in ("generated", "generated_on_demand"):
            return False
        zip_path = package_dir / zip_member
        if not zip_path.is_file() \
                or zip_path.stat().st_size != handoff.get("bytes") \
                or sha256_file(zip_path) != handoff.get("sha256"):
            return False
        expected_members = set(_canonical_package_members(manifest))
        with zipfile.ZipFile(zip_path, "r") as archive:
            names = archive.namelist()
            if archive.testzip() is not None or zip_member in names:
                return False
            if any(not _safe_package_member(name) for name in names):
                return False
            if len(names) != len(set(names)) or set(names) != expected_members:
                return False
            archived_manifest_bytes = archive.read(PACKAGE_MANIFEST_FILENAME)
            archived_manifest_sha = handoff.get("manifest_member_sha256")
            if archived_manifest_sha is not None \
                    and (not isinstance(archived_manifest_sha, str)
                         or hashlib.sha256(archived_manifest_bytes).hexdigest()
                         != archived_manifest_sha):
                return False
            archived_manifest = json.loads(
                archived_manifest_bytes.decode("utf-8")
            )
            if not isinstance(archived_manifest, dict):
                return False
            canonical_semantics = dict(manifest)
            archived_semantics = dict(archived_manifest)
            canonical_semantics.pop("handoff_zip", None)
            archived_semantics.pop("handoff_zip", None)
            if archived_semantics != canonical_semantics:
                return False
        return True
    except (OSError, UnicodeError, ValueError, KeyError, zipfile.BadZipFile,
            ExportBlocked):
        return False


def _write_deterministic_zip(zip_path: Path, package_dir: Path,
                             members: Sequence[str],
                             staging_dir: Optional[Path] = None,
                             member_overrides: Optional[
                                 Dict[str, bytes]
                             ] = None,
                             member_times: Optional[Dict[str, int]] = None) -> None:
    temporary_dir = zip_path.parent
    temporary_dir.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".codex-zip-",
        suffix=".tmp",
        dir=str(temporary_dir),
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    member_overrides = dict(member_overrides or {})
    try:
        with zipfile.ZipFile(temporary_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for member in sorted(members):
                if not _safe_package_member(member) or member in (
                        HANDOFF_ZIP_FILENAME, LEGACY_V2_HANDOFF_ZIP_FILENAME):
                    raise ExportBlocked(
                        STATUS_BLOCKED_PRIVACY,
                        "unsafe handoff ZIP member name",
                    )
                stamp = (member_times or {}).get(member)
                zip_time = (1980, 1, 1, 0, 0, 0)
                if isinstance(stamp, int):
                    try:
                        # ZIP DOS timestamps encode process-local wall time;
                        # manifest/receipt retain the absolute nanoseconds.
                        value = datetime.fromtimestamp(stamp / 1000000000)
                        if 1980 <= value.year <= 2107:
                            zip_time = value.timetuple()[:6]
                    except (ValueError, OverflowError, OSError):
                        pass
                info = zipfile.ZipInfo(member, date_time=zip_time)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100600 << 16
                payload = member_overrides.get(member)
                if payload is None:
                    payload = (package_dir / member).read_bytes()
                archive.writestr(info, payload)
        os.replace(str(temporary_path), str(zip_path))
    except BaseException:
        try:
            temporary_path.unlink()
        except OSError:
            pass
        raise


class PackageCommitInterrupted(OSError):
    """Publication stopped once it had begun replacing canonical files.

    Distinguishable so that the caller does not undo a rename that the
    partially published package now depends on.
    """


def _install_staged_package(stage: Path, package_dir: Path) -> None:
    """Install a brand-new package as one directory rename.

    Nothing may exist at the canonical path until the whole validated package
    can appear there at once. Creating the directory first and then moving
    members into it would leave an empty package behind whenever the first
    move failed, and POSIX marks the parent directory modified as soon as that
    directory is created. The project bucket is the only thing created ahead
    of the rename, and it is removed again if it was created for a package
    that never arrived.
    """
    bucket = package_dir.parent
    created_bucket = not bucket.exists()
    bucket.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(str(stage), str(package_dir))
    except OSError:
        if created_bucket:
            try:
                bucket.rmdir()
            except OSError:
                pass
        raise


def _commit_staged_package(stage: Path, package_dir: Path,
                           members: Sequence[str]) -> None:
    """Publish a validated staged package into its canonical directory.

    A package that does not exist yet is installed whole, so a failure leaves
    no canonical path at all. An existing package is updated one member at a
    time, which is what keeps a superseded package readable until its
    replacement is complete. The manifest is published last either way, so an
    interrupted update still leaves a package whose manifest does not attest
    its contents and which the next reconcile therefore republishes.
    """
    ordered = sorted(set(members) - {PACKAGE_MANIFEST_FILENAME})
    for member in ordered + [PACKAGE_MANIFEST_FILENAME]:
        if not _safe_package_member(member):
            raise ExportBlocked(
                STATUS_BLOCKED_PRIVACY, "unsafe package member name"
            )
    if not package_dir.exists():
        _install_staged_package(stage, package_dir)
        return
    committed = 0
    for member in ordered + [PACKAGE_MANIFEST_FILENAME]:
        source = stage / member
        target = package_dir / member
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(str(source), str(target))
        except OSError as error:
            # Nothing landed, so the caller may still undo a relocation and
            # treat the attempt as if it had never started.
            if committed == 0:
                raise
            raise PackageCommitInterrupted(str(error)) from error
        committed += 1


def _remove_stale_materialized_members(package_dir: Path,
                                       old_manifest: Optional[Dict[str, Any]],
                                       keep: set) -> None:
    if not old_manifest:
        return
    old_rows = as_list(old_manifest.get("attachments")) \
        + as_list(old_manifest.get("artifacts"))
    for row in old_rows:
        member = as_dict(row).get("member_path")
        if not isinstance(member, str) or member in keep \
                or not _safe_package_member(member):
            continue
        try:
            (package_dir / member).unlink()
        except FileNotFoundError:
            pass
    for relative in (
        "%s/_partial" % ATTACHMENTS_DIRNAME,
        ATTACHMENTS_DIRNAME,
        ARTIFACTS_DIRNAME,
        "%s/_partial" % LEGACY_V2_ATTACHMENTS_DIRNAME,
        LEGACY_V2_ATTACHMENTS_DIRNAME,
        LEGACY_V2_ARTIFACTS_DIRNAME,
    ):
        try:
            (package_dir / relative).rmdir()
        except OSError:
            pass


def _remove_legacy_v2_human_members(package_dir: Path) -> None:
    """Remove superseded v2 reading names, but preserve derived ZIP history."""
    try:
        (package_dir / LEGACY_V2_CONVERSATION_FILENAME).unlink()
    except FileNotFoundError:
        pass
    for relative in (
        "%s/_partial" % LEGACY_V2_ATTACHMENTS_DIRNAME,
        LEGACY_V2_ATTACHMENTS_DIRNAME,
        LEGACY_V2_ARTIFACTS_DIRNAME,
    ):
        # A legacy payload folder the Owner ever opened in Finder still holds
        # display metadata. Leaving it would keep the superseded directory
        # alive and the conversion visibly incomplete.
        try:
            (package_dir / relative /
             VOLATILE_FINDER_METADATA_FILENAME).unlink()
        except OSError:
            pass
        try:
            (package_dir / relative).rmdir()
        except OSError:
            pass


def _require_legacy_preservation(package_dir: Path,
                                 old_manifest: Optional[Dict[str, Any]],
                                 manifest: Dict[str, Any],
                                 context: Dict[str, Any]) -> None:
    """Check the supplied exact preservation proof before any publication.

    This boundary never discovers or migrates legacy bytes. A caller that
    cannot supply the exact verified proof must leave the old package alone.
    """
    if not old_manifest or old_manifest.get("package_schema_version") \
            not in RECOGNIZED_PACKAGE_SCHEMA_VERSIONS:
        return
    schema = old_manifest["package_schema_version"]
    required_rows = [
        (kind, as_dict(row))
        for kind, collection in (("attachment", "attachments"), ("artifact", "artifacts"))
        for row in as_list(old_manifest.get(collection))
        if (schema == "2.1" and as_dict(row).get("materialization_status") in ("materialized", "partial"))
        or (schema == "2.2" and "legacy_carry_forward" in as_dict(row))
    ]
    if not required_rows:
        return

    def refuse():
        raise ExportBlocked(
            STATUS_BLOCKED_PACKAGE,
            "legacy payload preservation proof is missing or inconsistent; "
            "refusing to replace the existing package",
            context["receipt"],
        )

    def proof_key(kind, row, proof):
        member, size, digest = row.get("member_path"), row.get("bytes"), row.get("sha256")
        if row.get("kind") != kind or not isinstance(member, str) \
                or not _safe_package_member(member) or type(size) is not int or size < 0 \
                or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) \
                or not isinstance(proof, dict) \
                or proof.get("source_package_schema_version") != "2.1" \
                or not isinstance(proof.get("source_member_path"), str) \
                or not _safe_package_member(proof["source_member_path"]) \
                or proof.get("payload_sha256") != digest:
            refuse()
        return (kind, size, digest, row.get("original_filename"),
                row.get("materialization_status"), row.get("completeness"),
                json.dumps(proof, sort_keys=True, ensure_ascii=False))

    # The old manifest is the requirement set, not the new caller's flags.
    # Integrity failure must not turn a required legacy row into an omission.
    if not package_files_current(package_dir, old_manifest):
        refuse()
    try:
        old_receipt = json.loads((package_dir / RECEIPT_FILENAME).read_text("utf-8"))
        required = {}
        for kind, row in required_rows:
            proof = row.get("legacy_carry_forward")
            if schema == "2.1":
                proof = {
                    "source_package_schema_version": "2.1",
                    "source_member_path": row.get("member_path"),
                    "source_rollout_sha256": as_dict(old_receipt.get("source")).get("rollout_sha256_pre"),
                    "source_receipt_core_sha256": old_receipt.get("receipt_core_sha256"),
                    "payload_sha256": row.get("sha256"),
                    "archived_payload_mtime_ns": (package_dir / row["member_path"]).stat().st_mtime_ns,
                    "reason": "attested_v2_1_payload_without_producing_turn_proof",
                }
            key = proof_key(kind, row, proof)
            if key in required:
                refuse()
            required[key] = row["member_path"]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        refuse()

    # Match both the published attestations and the actual candidate bytes.
    # A truthy marker or a matching digest on some other legacy member is not
    # sufficient, even when two old payloads happen to contain identical bytes.
    payloads = {item.get("materialized_member"): item
                for item in context["attachments"] + context["artifacts"]
                if item.get("materialized_member")}
    proven = set()
    for kind, collection in (("attachment", "attachments"), ("artifact", "artifacts")):
        for row in manifest[collection]:
            if "legacy_carry_forward" not in row:
                continue
            key = proof_key(kind, row, row["legacy_carry_forward"])
            item = payloads.get(row["member_path"], {})
            data = item.get("_materialized_bytes")
            if key not in required or key in proven \
                    or item.get("_managed_v2_carry_forward") is not True \
                    or item.get("_legacy_member_path") != required[key] \
                    or item.get("legacy_carry_forward") != row["legacy_carry_forward"] \
                    or not isinstance(data, bytes) or len(data) != row["bytes"] \
                    or sha256_bytes(data) != row["sha256"]:
                refuse()
            proven.add(key)
    if proven != set(required):
        refuse()


def write_outputs(context: Dict[str, Any], output_dir: Path) -> Dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    package = context["package"]
    package_dir = (
        output_dir / package["project_bucket"] / package["package_dirname"]
    )
    # The package directory is created by the commit, not here: a first
    # publish that fails must not leave an empty folder behind either.
    markdown_path = package_dir / CONVERSATION_FILENAME
    receipt_path = package_dir / RECEIPT_FILENAME
    manifest_path = package_dir / PACKAGE_MANIFEST_FILENAME
    handoff_path = package_dir / HANDOFF_ZIP_FILENAME
    markdown = context["markdown"]
    receipt = context["receipt"]

    body = markdown_body(markdown)
    display = output_path_privacy()
    receipt["volatile"] = {
        "generated_at": context["generated_at"],
        "markdown_path": display.normalize_paths(str(markdown_path)),
        "receipt_path": display.normalize_paths(str(receipt_path)),
        "package_path": display.normalize_paths(str(package_dir)),
        "manifest_path": display.normalize_paths(str(manifest_path)),
        "handoff_zip_path": None,
        "handoff_zip_on_demand_path":
            display.normalize_paths(str(handoff_path)),
        "handoff_zip_generation": "not_generated_by_default",
        "markdown_bytes": len(markdown.encode("utf-8")),
        "markdown_sha256": sha256_text(markdown),
    }
    # Only the body hash is deterministic: the file header carries the
    # generation timestamp, which is deliberately isolated from it.
    receipt["markdown"] = {
        "body_bytes": len(body.encode("utf-8")),
        "body_sha256": sha256_text(body),
    }
    core = deterministic_receipt_core(receipt)
    receipt["receipt_core_sha256"] = sha256_text(
        json.dumps(core, sort_keys=True, ensure_ascii=False)
    )
    markdown_bytes = markdown.encode("utf-8")
    receipt_bytes = _json_bytes(receipt)
    attachment_rows = [
        _manifest_payload_row(item, "attachment")
        for item in context["attachments"]
    ]
    artifact_rows = [
        _manifest_payload_row(item, "artifact")
        for item in context["artifacts"]
    ]
    semantic_receipt = deterministic_receipt_core(receipt)
    semantic_receipt.pop("receipt_core_sha256", None)
    content_fingerprint = sha256_text(json.dumps(
        {
            "markdown_body_sha256": sha256_text(body),
            "receipt_core": semantic_receipt,
            "attachments": attachment_rows,
            "artifacts": artifact_rows,
        },
        sort_keys=True,
        ensure_ascii=False,
    ))
    manifest: Dict[str, Any] = {
        "package_schema_version": PACKAGE_SCHEMA_VERSION,
        "session_id": receipt["session"]["session_id"],
        "package_dirname": package["package_dirname"],
        "package_relative_path": package["package_relative_path"],
        "display_title": package["display_title"],
        "display_title_source": package["display_title_source"],
        "project_bucket": package["project_bucket"],
        "project_bucket_source": package["project_bucket_source"],
        "rollout_coordinate_sha256": package["rollout_coordinate_sha256"],
        "session_started_at": receipt["session"]["started_at"],
        "export_status": receipt["export_status"],
        "package_complete": package["package_complete"],
        "content_fingerprint": content_fingerprint,
        "max_package_bytes": package["max_package_bytes"],
        "materialized_payload_bytes": package["materialized_payload_bytes"],
        "conversation_files": [
            _file_row(CONVERSATION_FILENAME, markdown_bytes),
            _file_row(RECEIPT_FILENAME, receipt_bytes),
        ],
        "attachments": attachment_rows,
        "artifacts": artifact_rows,
        "artifact_provenance_version": ARTIFACT_PROVENANCE_VERSION,
        "artifact_candidates": context["artifact_candidates"],
        "handoff_zip": {
            "status": "not_generated_by_default",
            "path": HANDOFF_ZIP_FILENAME,
            "sha256": None,
            "bytes": None,
            "excludes_itself": True,
            "generation": "on_demand",
            "canonical_package_dependency": False,
        },
    }

    index_bytes = context["output_index"].encode("utf-8")
    manifest["conversation_files"].append(_file_row(OUTPUT_INDEX_MEMBER, index_bytes))

    manifest_text = json.dumps(manifest, ensure_ascii=False)
    residuals = sanitization_rescan(manifest_text)
    if residuals or str(Path.home()) in manifest_text:
        raise ExportBlocked(
            STATUS_BLOCKED_PRIVACY,
            "package manifest failed sanitization",
            receipt,
        )

    old_manifest = None
    try:
        candidate = json.loads(manifest_path.read_text("utf-8"))
        old_manifest = candidate if isinstance(candidate, dict) else None
    except (OSError, ValueError):
        pass
    _require_legacy_preservation(package_dir, old_manifest, manifest, context)
    old_handoff = as_dict(
        old_manifest.get("handoff_zip") if old_manifest else None
    )
    old_handoff_member = old_handoff.get("path")
    if old_handoff_member in (
            HANDOFF_ZIP_FILENAME, LEGACY_V2_HANDOFF_ZIP_FILENAME):
        old_handoff_path = package_dir / old_handoff_member
        try:
            old_handoff_bytes = old_handoff_path.stat().st_size
            old_handoff_sha = sha256_file(old_handoff_path)
        except OSError:
            pass
        else:
            if old_handoff_bytes == old_handoff.get("bytes") \
                    and old_handoff_sha == old_handoff.get("sha256"):
                manifest["handoff_zip"] = {
                    "status": "preserved_existing_derived",
                    "path": old_handoff_member,
                    "sha256": old_handoff_sha,
                    "bytes": old_handoff_bytes,
                    "excludes_itself": bool(
                        old_handoff.get("excludes_itself", True)
                    ),
                    "generation": old_handoff.get("generation") or
                        "legacy_automatic_or_on_demand",
                    "canonical_package_dependency": False,
                    "current_canonical_content": False,
                }
    if old_manifest \
            and old_manifest.get("package_schema_version") == \
            PACKAGE_SCHEMA_VERSION \
            and old_manifest.get("project_bucket") == package["project_bucket"] \
            and old_manifest.get("content_fingerprint") == content_fingerprint \
            and any(
                as_dict(row).get("path") == CONVERSATION_FILENAME
                for row in as_list(old_manifest.get("conversation_files"))
            ) \
            and package_files_current(package_dir, old_manifest):
        try:
            context["receipt"] = json.loads(receipt_path.read_text("utf-8"))
        except (OSError, ValueError):
            pass
        return {
            "package_dir": package_dir,
            "markdown_path": markdown_path,
            "receipt_path": receipt_path,
            "manifest_path": manifest_path,
            "handoff_zip_path": (
                handoff_path if handoff_zip_current(package_dir, old_manifest)
                else None
            ),
            "handoff_zip_on_demand_path": handoff_path,
            "changed": False,
        }

    payload_members = {}
    for item in context["attachments"] + context["artifacts"]:
        member = item.get("materialized_member")
        data = item.get("_materialized_bytes")
        if member and isinstance(data, bytes):
            if not _safe_package_member(member):
                raise ExportBlocked(
                    STATUS_BLOCKED_PRIVACY, "unsafe package member name", receipt
                )
            payload_members[member] = data
    keep_members = set(payload_members)
    payload_members[OUTPUT_INDEX_MEMBER] = index_bytes
    artifact_times = {item["materialized_member"]: item["payload_mtime_ns"]
                      for item in context["artifacts"]
                      if item.get("materialized_member") and "payload_mtime_ns" in item}

    # The complete replacement package is assembled in a private staging area
    # and validated there. Only then is it published into the canonical
    # directory. An attempt that fails at any point before that commit leaves
    # the canonical package byte-identical and, because no directory entry was
    # ever created inside it, keeps its Finder modified time. The commit still
    # publishes one file at a time with the manifest last, so a torn package
    # remains detectable and the next reconcile deterministically repairs it.
    with package_staging_area(output_dir, package["package_dirname"]) as stage:
        atomic_write_text(stage / CONVERSATION_FILENAME, markdown,
                          staging_dir=stage)
        atomic_write_text(stage / RECEIPT_FILENAME,
                          receipt_bytes.decode("utf-8"), staging_dir=stage)
        for member, data in sorted(payload_members.items()):
            atomic_write_bytes(stage / member, data, staging_dir=stage)
            if member in artifact_times:
                stamp = artifact_times[member]
                os.utime(stage / member, ns=(stamp, stamp))

        members = [CONVERSATION_FILENAME, RECEIPT_FILENAME,
                   PACKAGE_MANIFEST_FILENAME] + sorted(payload_members)
        atomic_write_bytes(stage / PACKAGE_MANIFEST_FILENAME,
                           _json_bytes(manifest), staging_dir=stage)
        if not package_files_current(stage, manifest):
            raise OSError("staged package failed integrity validation")
        # Raises PackageCommitInterrupted only once a canonical file has
        # actually been replaced; a commit that lands nothing stays an
        # ordinary OSError so the caller can undo a relocation.
        _commit_staged_package(stage, package_dir, members)
    if not package_files_current(package_dir, manifest):
        raise OSError("published package failed integrity validation")
    # Retain superseded payload bytes until the complete replacement manifest
    # and ZIP have passed integrity validation. This is especially important
    # for v2 -> v2.1 migration, where the old package is the only remaining
    # source for an unavailable historical attachment or artifact.
    _remove_stale_materialized_members(package_dir, old_manifest, keep_members)
    _remove_legacy_v2_human_members(package_dir)
    return {
        "package_dir": package_dir,
        "markdown_path": markdown_path,
        "receipt_path": receipt_path,
        "manifest_path": manifest_path,
        "handoff_zip_path": None,
        "handoff_zip_on_demand_path": handoff_path,
        "changed": True,
    }


def generate_handoff_zip(package_dir: Path) -> Dict[str, Any]:
    """Generate one deterministic derived ZIP from a verified package."""
    try:
        package_dir = package_dir.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ExportBlocked(
            STATUS_BLOCKED_PACKAGE, "canonical package directory is unavailable"
        ) from error
    if not package_dir.is_dir():
        raise ExportBlocked(
            STATUS_BLOCKED_PACKAGE, "canonical package path is not a directory"
        )
    manifest_path = package_dir / PACKAGE_MANIFEST_FILENAME
    try:
        manifest = json.loads(manifest_path.read_text("utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise ExportBlocked(
            STATUS_BLOCKED_PACKAGE, "canonical package manifest is unreadable"
        ) from error
    if not isinstance(manifest, dict) or not package_files_current(
            package_dir, manifest):
        raise ExportBlocked(
            STATUS_BLOCKED_PACKAGE,
            "canonical package integrity verification failed",
        )

    members = _canonical_package_members(manifest)
    archived_manifest = dict(manifest)
    archived_manifest["handoff_zip"] = {
        "status": "generated_on_demand_self_attestation_external",
        "path": HANDOFF_ZIP_FILENAME,
        "sha256": None,
        "bytes": None,
        "excludes_itself": True,
        "generation": "on_demand",
        "canonical_package_dependency": False,
        "manifest_member_semantics":
            "deterministic snapshot; canonical sibling manifest attests ZIP bytes",
    }
    archived_manifest_bytes = _json_bytes(archived_manifest)
    handoff_path = package_dir / HANDOFF_ZIP_FILENAME
    _write_deterministic_zip(
        handoff_path,
        package_dir,
        members,
        member_overrides={PACKAGE_MANIFEST_FILENAME: archived_manifest_bytes},
        member_times={row["member_path"]: row["payload_mtime_ns"]
                      for row in as_list(manifest.get("artifacts"))
                      if row.get("member_path") and isinstance(row.get("payload_mtime_ns"), int)},
    )
    manifest["handoff_zip"] = {
        "status": "generated_on_demand",
        "path": HANDOFF_ZIP_FILENAME,
        "sha256": sha256_file(handoff_path),
        "bytes": handoff_path.stat().st_size,
        "excludes_itself": True,
        "generation": "on_demand",
        "canonical_package_dependency": False,
        "manifest_member_sha256":
            hashlib.sha256(archived_manifest_bytes).hexdigest(),
        "manifest_member_semantics":
            "deterministic snapshot; canonical sibling manifest attests ZIP bytes",
    }
    atomic_write_bytes(manifest_path, _json_bytes(manifest))
    if not package_files_current(package_dir, manifest) \
            or not handoff_zip_current(package_dir, manifest):
        raise OSError("on-demand handoff ZIP failed integrity validation")
    return {
        "path": handoff_path,
        "bytes": handoff_path.stat().st_size,
        "sha256": sha256_file(handoff_path),
        "canonical_representation": "project_grouped_session_folder",
        "handoff_zip_role": "derived_transfer_artifact",
        "generation": "on_demand",
    }


def write_blocked_receipt(output_dir: Path, blocked: ExportBlocked,
                          generated_at: str,
                          protected_package_dir: Optional[Path] = None) -> Optional[Path]:
    """Bounded fail-closed receipt. Never a private path, never a partial export."""
    if protected_package_dir is not None:
        try:
            output_dir.resolve().relative_to(protected_package_dir.resolve())
        except ValueError:
            pass
        except (OSError, RuntimeError):
            return None
        else:
            # Even an explicit blocked-receipt destination must not modify
            # the package whose replacement was just refused.
            return None
    # Defence in depth: selection already normalizes what it raises, but a
    # blocked receipt is a generated artifact and must satisfy the same
    # contract whatever produced it.
    receipt = scrub_structure(dict(blocked.receipt), selection_privacy())
    receipt.setdefault("exporter_version", EXPORTER_VERSION)
    receipt["export_status"] = blocked.status
    receipt["blocked_detail"] = selection_privacy().clean(blocked.detail)
    receipt["volatile"] = {"generated_at": generated_at}
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    session = as_dict(receipt.get("source")).get("session_id") or "unknown-session"
    path = output_dir / ("%s__%s.blocked.receipt.json"
                         % (safe_label(str(session)[:8], "session"),
                            blocked.status.lower()))
    atomic_write_text(
        path,
        json.dumps(receipt, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
    )
    return path


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def bounded_hours(value: str) -> int:
    number = int(value)
    if number < 1 or number > MAX_SINCE_HOURS:
        raise argparse.ArgumentTypeError(
            "--since-hours must be between 1 and %d" % MAX_SINCE_HOURS
        )
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-preserve export",
        description="Export one local Codex rollout as a concise Conversation "
                    "Export Markdown plus a machine receipt.",
    )
    selection = parser.add_argument_group("selection")
    selection.add_argument("--rollout", help="exact rollout JSONL path")
    selection.add_argument("--session-id", help="exact Codex session id")
    selection.add_argument("--list-candidates", action="store_true",
                           help="list sanitized candidate sessions and exit")
    selection.add_argument("--workspace", help="filter candidates by workspace path")
    selection.add_argument("--since-hours", type=bounded_hours,
                           help="only consider rollouts modified within N hours")

    output = parser.add_argument_group("output")
    output.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    output.add_argument(
        "--blocked-receipt-dir",
        help="optional private destination for blocked receipts; the manual "
             "default remains --output-dir",
    )
    output.add_argument("--label", help="explicit human title for the package folder")
    output.add_argument(
        "--build-handoff-zip",
        metavar="PACKAGE_DIR",
        help="verify one canonical package and generate its derived handoff ZIP",
    )
    output.add_argument("--stdout-receipt", action="store_true",
                        help="also print the receipt JSON to stdout")
    output.add_argument("--quiet", action="store_true")

    provenance = parser.add_argument_group("provenance")
    provenance.add_argument("--review-bundle",
                            help="exact path of the Review Bundle for this task")
    provenance.add_argument("--artifact", action="append", metavar="ROLE=PATH",
                            help="exact artifact path, repeatable")
    provenance.add_argument("--no-git-probe", action="store_true",
                            help="skip read-only export-time Git provenance")

    policy = parser.add_argument_group("policy")
    policy.add_argument("--no-normalize", action="store_true",
                        help="disable human-facing path normalization "
                             "(redaction still applies)")
    policy.add_argument("--reasoning-cap", type=int,
                        default=DEFAULT_REASONING_CAP)
    policy.add_argument("--command-chars", type=int, default=DEFAULT_COMMAND_CHARS)
    policy.add_argument("--max-attachment-snapshot-bytes", type=int,
                        default=DEFAULT_ATTACHMENT_SNAPSHOT_BYTES)
    policy.add_argument("--max-package-bytes", type=int,
                        default=DEFAULT_MAX_PACKAGE_BYTES)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    options = parser.parse_args(argv)
    options.generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    output_dir = Path(options.output_dir).expanduser()

    if options.build_handoff_zip:
        if any((options.rollout, options.session_id, options.list_candidates,
                options.workspace, options.since_hours, options.label,
                options.review_bundle, options.artifact)):
            parser.error(
                "--build-handoff-zip cannot be combined with export selection "
                "or provenance arguments"
            )
        try:
            handoff = generate_handoff_zip(Path(options.build_handoff_zip))
        except ExportBlocked as blocked:
            print(json.dumps({
                "status": blocked.status,
                "detail": selection_privacy().clean(blocked.detail),
            }, indent=2, sort_keys=True, ensure_ascii=False), file=sys.stderr)
            return 2
        display = output_path_privacy()
        print(json.dumps({
            "status": "GENERATED_ON_DEMAND",
            "handoff_zip": display.normalize_paths(str(handoff["path"])),
            "bytes": handoff["bytes"],
            "sha256": handoff["sha256"],
            "canonical_representation": handoff["canonical_representation"],
            "handoff_zip_role": handoff["handoff_zip_role"],
        }, indent=2, sort_keys=True, ensure_ascii=False))
        return 0

    if options.list_candidates:
        rows = scrub_structure(
            list_candidates(options.workspace, options.since_hours),
            selection_privacy(),
        )
        print(json.dumps({"candidate_count": len(rows), "candidates": rows},
                         indent=2, sort_keys=True, ensure_ascii=False))
        return 0

    try:
        context = run_export(options)
    except ExportBlocked as blocked:
        blocked_output_dir = Path(
            options.blocked_receipt_dir or options.output_dir
        ).expanduser()
        path = write_blocked_receipt(
            blocked_output_dir, blocked, options.generated_at
        )
        display = output_path_privacy()
        payload = {
            "export_status": blocked.status,
            "detail": selection_privacy().clean(blocked.detail),
            "blocked_receipt": display.normalize_paths(str(path)) if path else None,
        }
        print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False),
              file=sys.stderr)
        return 2

    try:
        written = write_outputs(context, output_dir)
    except ExportBlocked as blocked:
        blocked_output_dir = Path(
            options.blocked_receipt_dir or options.output_dir
        ).expanduser()
        path = write_blocked_receipt(
            blocked_output_dir, blocked, options.generated_at,
            protected_package_dir=(output_dir / context["package"]["project_bucket"]
                                   / context["package"]["package_dirname"]),
        )
        display = output_path_privacy()
        print(json.dumps({
            "export_status": blocked.status,
            "detail": selection_privacy().clean(blocked.detail),
            "blocked_receipt": display.normalize_paths(str(path)) if path else None,
        }, indent=2, sort_keys=True, ensure_ascii=False), file=sys.stderr)
        return 2
    receipt = context["receipt"]
    if options.stdout_receipt:
        print(json.dumps(receipt, indent=2, sort_keys=True, ensure_ascii=False))
    elif not options.quiet:
        display = output_path_privacy()
        print(json.dumps(
            {
                "export_status": receipt["export_status"],
                "markdown": display.normalize_paths(str(written["markdown_path"])),
                "markdown_bytes": receipt["volatile"]["markdown_bytes"],
                "receipt": display.normalize_paths(str(written["receipt_path"])),
                "package": display.normalize_paths(str(written["package_dir"])),
                "manifest": display.normalize_paths(str(written["manifest_path"])),
                "handoff_zip": (
                    display.normalize_paths(str(written["handoff_zip_path"]))
                    if written["handoff_zip_path"] is not None else None
                ),
                "handoff_zip_on_demand": display.normalize_paths(
                    str(written["handoff_zip_on_demand_path"])
                ),
                "package_changed": written["changed"],
                "source_sha256_pre": receipt["source"]["rollout_sha256_pre"],
                "source_sha256_post": receipt["source"]["rollout_sha256_post"],
            },
            indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
