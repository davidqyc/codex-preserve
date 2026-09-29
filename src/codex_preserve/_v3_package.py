"""Internal schema-3.0 package assembly and integrity verification.

Schema 3 is the future Session Preserve package envelope. It is deliberately
provider-neutral only at the package/integrity layer: provider adapters render
their own conversation and receipt content before calling this module.

This module does not define a generic conversation ontology.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Dict, Iterable, Optional, Tuple

from ._shared_core import (
    atomic_write_bytes,
    json_bytes,
    safe_package_member,
    sha256_bytes,
    sha256_file,
)


PACKAGE_SCHEMA_VERSION = "3.0"
PRODUCT_NAME = "session-preserve"

CONVERSATION_FILENAME = "conversation.md"
RECEIPT_FILENAME = "export.receipt.json"
MANIFEST_FILENAME = "package.manifest.json"
ATTACHMENTS_DIRNAME = "attachments"
ARTIFACTS_DIRNAME = "artifacts"
ARTIFACT_INDEX_FILENAME = "artifacts/index.md"
TRANSFER_ZIP_FILENAME = "session-package.zip"

_CANONICAL_REQUIRED_ROLES = {
    CONVERSATION_FILENAME: "conversation",
    RECEIPT_FILENAME: "receipt",
}
_RESERVED_PATHS = frozenset((MANIFEST_FILENAME, TRANSFER_ZIP_FILENAME))


@dataclass(frozen=True)
class V3Member:
    path: str
    role: str
    payload: bytes


@dataclass(frozen=True)
class V3PackageSpec:
    provider: str
    adapter_name: str
    adapter_version: str
    coverage_status: str
    source_stable: bool
    source_identity_scope: str
    source_identity_sha256: Optional[str]
    conversation_markdown: str
    provider_receipt: Dict[str, Any]
    extra_members: Tuple[V3Member, ...] = ()


def _valid_hex_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _validate_spec(spec: V3PackageSpec) -> None:
    if not isinstance(spec, V3PackageSpec):
        raise TypeError("spec must be V3PackageSpec")
    for label, value in (
        ("provider", spec.provider),
        ("adapter_name", spec.adapter_name),
        ("adapter_version", spec.adapter_version),
        ("source_identity_scope", spec.source_identity_scope),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("%s must be a non-empty string" % label)
    if spec.coverage_status not in ("COMPLETE", "NON_COMPLETE"):
        raise ValueError("coverage_status must be COMPLETE or NON_COMPLETE")
    if not isinstance(spec.source_stable, bool):
        raise ValueError("source_stable must be boolean")
    if spec.source_stable:
        if not _valid_hex_sha256(spec.source_identity_sha256):
            raise ValueError("stable source requires a sha256 identity")
    elif spec.source_identity_sha256 is not None:
        raise ValueError("unstable source cannot attest a sha256 identity")
    if not isinstance(spec.conversation_markdown, str):
        raise TypeError("conversation_markdown must be text")
    if not isinstance(spec.provider_receipt, dict):
        raise TypeError("provider_receipt must be an object")

    seen = set(_CANONICAL_REQUIRED_ROLES)
    for member in spec.extra_members:
        if not isinstance(member, V3Member):
            raise TypeError("extra_members must contain V3Member values")
        if not safe_package_member(member.path):
            raise ValueError("unsafe package member path")
        if member.path in seen or member.path in _RESERVED_PATHS:
            raise ValueError("duplicate or reserved package member path")
        if member.path.startswith(ATTACHMENTS_DIRNAME + "/"):
            expected_role = "attachment"
        elif member.path.startswith(ARTIFACTS_DIRNAME + "/"):
            expected_role = (
                "artifact_index"
                if member.path == ARTIFACT_INDEX_FILENAME
                else "artifact"
            )
        else:
            raise ValueError(
                "extra v3 members must live under attachments/ or artifacts/"
            )
        if member.role != expected_role:
            raise ValueError(
                "member role %r does not match path %r"
                % (member.role, member.path)
            )
        if not isinstance(member.payload, bytes):
            raise TypeError("member payload must be bytes")
        seen.add(member.path)


def _receipt_bytes(spec: V3PackageSpec) -> bytes:
    receipt = {
        "product": PRODUCT_NAME,
        "package_schema_version": PACKAGE_SCHEMA_VERSION,
        "provider": spec.provider,
        "provider_adapter": {
            "name": spec.adapter_name,
            "version": spec.adapter_version,
        },
        "coverage_status": spec.coverage_status,
        "source_snapshot": {
            "stable": spec.source_stable,
            "identity_scope": spec.source_identity_scope,
            "identity_sha256": spec.source_identity_sha256,
        },
        "provider_receipt": spec.provider_receipt,
        "integrity_scope": {
            "manifest_relative": True,
            "authenticity_attested": False,
        },
    }
    return json_bytes(receipt)


def _member_rows(members: Iterable[V3Member]) -> list:
    rows = []
    for member in members:
        rows.append({
            "path": member.path,
            "role": member.role,
            "bytes": len(member.payload),
            "sha256": sha256_bytes(member.payload),
        })
    return rows


def build_v3_materialization(spec: V3PackageSpec) -> Tuple[Tuple[V3Member, ...], bytes]:
    """Return canonical members and manifest bytes without touching disk."""
    _validate_spec(spec)
    conversation = V3Member(
        CONVERSATION_FILENAME,
        "conversation",
        spec.conversation_markdown.encode("utf-8"),
    )
    receipt = V3Member(RECEIPT_FILENAME, "receipt", _receipt_bytes(spec))
    members = (conversation, receipt) + tuple(spec.extra_members)
    manifest = {
        "package_schema_version": PACKAGE_SCHEMA_VERSION,
        "product": PRODUCT_NAME,
        "provider": spec.provider,
        "provider_adapter": {
            "name": spec.adapter_name,
            "version": spec.adapter_version,
        },
        "coverage_status": spec.coverage_status,
        "package_materialized": True,
        "source_snapshot": {
            "stable": spec.source_stable,
            "identity_scope": spec.source_identity_scope,
            "identity_sha256": spec.source_identity_sha256,
        },
        "members": _member_rows(members),
        "transfer_artifact": {
            "path": TRANSFER_ZIP_FILENAME,
            "status": "not_generated",
            "canonical_package_dependency": False,
            "excludes_itself": True,
        },
    }
    return members, json_bytes(manifest)


def write_v3_package(package_dir: Path, spec: V3PackageSpec) -> Path:
    """Materialize one new schema-3 package atomically at directory granularity."""
    members, manifest_bytes = build_v3_materialization(spec)
    package_dir = Path(package_dir)
    parent = package_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    if package_dir.exists():
        raise FileExistsError(str(package_dir))

    staging = Path(tempfile.mkdtemp(
        prefix=".session-preserve-build-", dir=str(parent)))
    try:
        for member in members:
            atomic_write_bytes(staging / member.path, member.payload)
        atomic_write_bytes(staging / MANIFEST_FILENAME, manifest_bytes)
        os.replace(str(staging), str(package_dir))
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return package_dir


def _reason(code: str, detail: str, member: Optional[str] = None) -> dict:
    result = {"code": code, "detail": detail}
    if member is not None:
        result["member"] = member
    return result


def _manifest_shape(manifest: object) -> Tuple[list, Optional[list]]:
    reasons = []
    if not isinstance(manifest, dict):
        return [_reason("manifest_not_an_object",
                        "package.manifest.json is not a JSON object")], None
    if manifest.get("package_schema_version") != PACKAGE_SCHEMA_VERSION:
        return [_reason("package_schema_unsupported",
                        "expected package schema 3.0")], None
    if manifest.get("product") != PRODUCT_NAME:
        reasons.append(_reason("product_identity_invalid",
                               "schema-3 package product identity is invalid"))
    provider = manifest.get("provider")
    if not isinstance(provider, str) or not provider:
        reasons.append(_reason("provider_missing",
                               "schema-3 manifest has no provider identity"))
    adapter = manifest.get("provider_adapter")
    if not isinstance(adapter, dict) or not isinstance(adapter.get("name"), str) \
            or not isinstance(adapter.get("version"), str):
        reasons.append(_reason("provider_adapter_invalid",
                               "schema-3 provider adapter identity is invalid"))
    if manifest.get("coverage_status") not in ("COMPLETE", "NON_COMPLETE"):
        reasons.append(_reason("coverage_status_invalid",
                               "schema-3 coverage status is invalid"))
    if manifest.get("package_materialized") is not True:
        reasons.append(_reason("package_materialization_invalid",
                               "schema-3 package is not marked materialized"))

    source = manifest.get("source_snapshot")
    if not isinstance(source, dict) or not isinstance(source.get("stable"), bool):
        reasons.append(_reason("source_snapshot_invalid",
                               "schema-3 source snapshot is invalid"))
    elif source["stable"]:
        if not _valid_hex_sha256(source.get("identity_sha256")):
            reasons.append(_reason("source_snapshot_invalid",
                                   "stable source lacks sha256 identity"))
    elif source.get("identity_sha256") is not None:
        reasons.append(_reason("source_snapshot_invalid",
                               "unstable source attests a sha256 identity"))

    rows = manifest.get("members")
    if not isinstance(rows, list):
        reasons.append(_reason("members_invalid",
                               "schema-3 manifest members is not a list"))
        return reasons, None
    seen = set()
    roles = {}
    for row in rows:
        if not isinstance(row, dict):
            reasons.append(_reason("member_row_invalid",
                                   "schema-3 member row is not an object"))
            continue
        path = row.get("path")
        role = row.get("role")
        if not isinstance(path, str) or not safe_package_member(path):
            reasons.append(_reason("member_path_invalid",
                                   "schema-3 member path is unsafe"))
            continue
        if path in seen or path in _RESERVED_PATHS:
            reasons.append(_reason("member_path_duplicate_or_reserved",
                                   "schema-3 member path is duplicated or reserved",
                                   path))
        seen.add(path)
        roles[path] = role
        if not isinstance(role, str) or not role:
            reasons.append(_reason("member_role_invalid",
                                   "schema-3 member role is invalid", path))
        size = row.get("bytes")
        digest = row.get("sha256")
        if not isinstance(size, int) or size < 0:
            reasons.append(_reason("member_size_invalid",
                                   "schema-3 member size is invalid", path))
        if not _valid_hex_sha256(digest):
            reasons.append(_reason("member_sha256_invalid",
                                   "schema-3 member sha256 is invalid", path))

    for path, role in _CANONICAL_REQUIRED_ROLES.items():
        if path not in seen:
            reasons.append(_reason("required_member_missing_from_manifest",
                                   "required schema-3 member is not attested", path))
        elif roles.get(path) != role:
            reasons.append(_reason("required_member_role_invalid",
                                   "required schema-3 member role is invalid", path))

    transfer = manifest.get("transfer_artifact")
    if not isinstance(transfer, dict) \
            or transfer.get("path") != TRANSFER_ZIP_FILENAME \
            or transfer.get("canonical_package_dependency") is not False \
            or transfer.get("excludes_itself") is not True:
        reasons.append(_reason("transfer_artifact_invalid",
                               "schema-3 transfer artifact declaration is invalid"))

    return reasons, rows


def verify_v3_package(package_dir: Path) -> Dict[str, Any]:
    """Verify schema-3 manifest-relative integrity without provider semantics."""
    package_dir = Path(package_dir)
    result: Dict[str, Any] = {
        "tool": PRODUCT_NAME,
        "check": "package_integrity",
        "package_schema_version": PACKAGE_SCHEMA_VERSION,
        "package": str(package_dir),
        "members_attested": 0,
        "members_verified": 0,
        "transfer_zip": "not_attested_as_current",
        "attestation_semantics": (
            "sha256 manifest completeness and integrity only; "
            "not a cryptographic signature and not an authorship attestation"
        ),
        "reasons": [],
        "verdict": "UNVERIFIABLE",
        "exit_code": 2,
    }
    try:
        resolved = package_dir.expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        result["reasons"].append(_reason(
            "package_path_unavailable", "the package path does not exist"))
        return result
    result["package"] = str(resolved)
    if not resolved.is_dir():
        result["reasons"].append(_reason(
            "package_path_not_a_directory",
            "the package path is not a directory"))
        return result

    manifest_path = resolved / MANIFEST_FILENAME
    try:
        manifest = json.loads(manifest_path.read_text("utf-8"))
    except FileNotFoundError:
        result["reasons"].append(_reason(
            "manifest_missing", "package.manifest.json is missing"))
        return result
    except (OSError, UnicodeError, ValueError):
        result["reasons"].append(_reason(
            "manifest_unreadable",
            "package.manifest.json is unreadable or invalid JSON"))
        return result

    shape_reasons, rows = _manifest_shape(manifest)
    if shape_reasons:
        result["reasons"].extend(shape_reasons)
        return result
    assert rows is not None
    result["members_attested"] = len(rows)

    attested = set()
    member_failures = []
    cannot_determine = []
    for row in rows:
        path = row["path"]
        attested.add(path)
        target = resolved / path
        try:
            relative = target.relative_to(resolved)
        except ValueError:
            member_failures.append(_reason(
                "member_containment_failed",
                "member escapes package root", path))
            continue
        if relative.as_posix() != path:
            member_failures.append(_reason(
                "member_path_normalization_failed",
                "member path does not normalize identically", path))
            continue

        cursor = resolved
        unsafe = False
        for part in Path(path).parts:
            cursor = cursor / part
            try:
                if cursor.is_symlink():
                    unsafe = True
                    break
            except OSError:
                cannot_determine.append(_reason(
                    "member_containment_unverifiable",
                    "member containment could not be verified", path))
                unsafe = True
                break
        if unsafe:
            if not cannot_determine or cannot_determine[-1].get("member") != path:
                member_failures.append(_reason(
                    "member_symlink_not_allowed",
                    "manifest-attested members may not be symlinks", path))
            continue
        try:
            if not target.is_file():
                member_failures.append(_reason(
                    "member_missing", "manifest-attested member is missing", path))
                continue
            size = target.stat().st_size
            digest = sha256_file(target)
        except OSError:
            cannot_determine.append(_reason(
                "member_unreadable",
                "manifest-attested member could not be read", path))
            continue
        if size != row["bytes"]:
            member_failures.append(_reason(
                "member_size_mismatch",
                "manifest-attested member size does not match", path))
            continue
        if digest != row["sha256"]:
            member_failures.append(_reason(
                "member_sha256_mismatch",
                "manifest-attested member sha256 does not match", path))
            continue
        result["members_verified"] += 1

    # The manifest itself is the attestation root. The derived transfer ZIP is
    # explicitly outside canonical package integrity when not generated.
    actual = set()
    try:
        for path in resolved.rglob("*"):
            rel = path.relative_to(resolved).as_posix()
            if path.is_symlink():
                member_failures.append(_reason(
                    "symlink_in_package",
                    "schema-3 canonical packages may not contain symlinks", rel))
                continue
            if path.is_file():
                if rel not in (MANIFEST_FILENAME, TRANSFER_ZIP_FILENAME):
                    actual.add(rel)
    except OSError:
        cannot_determine.append(_reason(
            "package_enumeration_failed",
            "package member completeness could not be enumerated"))
    unexpected = sorted(actual - attested)
    missing_from_tree = sorted(attested - actual)
    for path in unexpected:
        member_failures.append(_reason(
            "unattested_member_present",
            "package contains a canonical member absent from manifest", path))
    for path in missing_from_tree:
        if not any(row.get("member") == path for row in member_failures):
            member_failures.append(_reason(
                "member_missing", "manifest-attested member is missing", path))

    result["reasons"].extend(member_failures)
    result["reasons"].extend(cannot_determine)
    if member_failures:
        result["verdict"] = "FAIL"
        result["exit_code"] = 1
    elif cannot_determine:
        result["verdict"] = "UNVERIFIABLE"
        result["exit_code"] = 2
    else:
        result["verdict"] = "PASS"
        result["exit_code"] = 0
        result["reasons"] = []
    return result
