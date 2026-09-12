"""Thin verification surface over the existing package integrity primitives.

This module adds no integrity semantics of its own. The verdict is decided by
``exporter.package_files_current`` and ``exporter.handoff_zip_current``, the
same functions the exporter uses to validate a staged package before it is
published. Everything here only explains, in human-readable terms, which
manifest-attested expectation was not met.

Fail-closed rules:

- a package whose manifest cannot be read is UNVERIFIABLE, never PASS;
- a manifest whose schema version this build does not recognize is
  UNVERIFIABLE, because a verifier that cannot interpret the manifest cannot
  claim the members it did not look at are fine;
- a manifest collection or row whose shape is not the expected JSON
  object/array is UNVERIFIABLE, not FAIL: nothing about the payload has been
  shown to be wrong, the manifest simply cannot be interpreted. Those shapes
  are rejected before the reused integrity primitive is called, so it is never
  handed input it does not defend against;
- a manifest-attested member whose filesystem metadata or content cannot be
  queried at all (for example a permission error, an overlong path, or an
  ``OSError`` from the real stat/open/hash read) is UNVERIFIABLE, never FAIL:
  nothing about the member has been proven missing or altered. Only the
  minimal ``OSError`` surface of the real I/O is guarded; an unrelated
  programming error still propagates;
- determinate evidence outranks unknown state: any manifest-attested member
  proven missing or altered makes the package FAIL even when another
  member's state cannot be determined; UNVERIFIABLE requires the absence of
  determinate-bad evidence;
- a manifest-attested member path containing any symlink component is a
  determinate invalid package — FAIL, and the link is never followed, opened
  or hashed. Containment that cannot be checked safely is UNVERIFIABLE, and
  the reused integrity primitives are never given a chance to read a member
  whose containment failed or is unproven;
- any unexpected error from the integrity primitives is UNVERIFIABLE rather
  than a traceback;
- a diagnostic pass that finds no explanation while the integrity primitive
  reports failure still fails, with an explicit ``integrity_primitive_failed``
  reason;
- the derived transfer ZIP is only judged when the manifest attests it as the
  current derived representation, and only after a real-I/O probe proves the
  ZIP file itself is readable: an attested current ZIP that cannot be read at
  all (for example a permission error from the actual open) is UNVERIFIABLE —
  its content matches nothing has been proven — never a determinate
  ``handoff_zip_mismatch`` FAIL;

What the manifest attests is SHA-256 completeness and integrity of the members
it lists. That is not a cryptographic signature and proves nothing about who
produced the package.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import exporter

# Exit codes. Nonzero for anything that is not a proven-intact package.
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_UNVERIFIABLE = 2

VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"
VERDICT_UNVERIFIABLE = "UNVERIFIABLE"

ATTESTATION_SEMANTICS = (
    "sha256 manifest completeness and integrity only; "
    "not a cryptographic signature and not an authorship attestation"
)

# Exactly the collections the reused integrity primitive consumes, in the
# order it consumes them.
ROW_COLLECTIONS = ("conversation_files", "attachments", "artifacts")

# The manifest describes a current derived ZIP only under these two states.
# Any other state ("preserved_existing_derived", "not_generated_by_default")
# deliberately does not claim the ZIP mirrors current canonical content.
_CURRENT_ZIP_STATUSES = ("generated", "generated_on_demand")

# Verdict aggregation is evidence-priority, three-valued:
# PASS  — the package is proven intact;
# FAIL  — at least one manifest-attested member is proven missing or altered,
#         even if another member's state is simultaneously unknown;
# UNVERIFIABLE — no determinate-bad evidence, and at least one required state
#         could not be determined safely.
# Reason codes that prove a member missing, altered, or not a valid
# contained package member.
_DETERMINATE_BAD_REASONS = frozenset({
    "member_missing",
    "member_size_mismatch",
    "member_sha256_mismatch",
    "handoff_zip_mismatch",
    "member_symlink_not_allowed",
})
# Reason codes that mean a required state could not be determined safely.
_CANNOT_DETERMINE_REASONS = frozenset({
    "member_state_unverifiable",
    "member_unreadable",
    "member_containment_unverifiable",
    "integrity_primitive_error",
})


def _reason(code: str, detail: str,
            member: Optional[str] = None) -> Dict[str, Any]:
    row = {"code": code, "detail": detail}
    if member is not None:
        row["member"] = member
    return row


def _malformed_shape_reasons(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Reject manifest shapes the reused integrity primitive cannot survive.

    ``package_files_current`` calls ``row.get(...)`` on raw collection
    elements and does not catch ``AttributeError``/``TypeError``, so a
    non-object row would escape as a traceback. Checking the shape here keeps
    that primitive untouched and authoritative for well-formed input, and
    classifies a manifest we cannot interpret as UNVERIFIABLE rather than as
    an altered payload.
    """
    reasons: List[Dict[str, Any]] = []
    for name in ROW_COLLECTIONS:
        if name not in manifest:
            continue
        value = manifest[name]
        if not isinstance(value, list):
            reasons.append(_reason(
                "manifest_collection_not_an_array",
                "%s is present but is %s, not a JSON array"
                % (name, type(value).__name__)))
            continue
        for index, row in enumerate(value):
            if not isinstance(row, dict):
                reasons.append(_reason(
                    "manifest_row_not_an_object",
                    "%s[%d] is %s, not a JSON object"
                    % (name, index, type(row).__name__)))
    return reasons


def _attested_rows(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Exactly the rows ``package_files_current`` walks, in the same order."""
    rows = list(exporter.as_list(manifest.get("conversation_files")))
    rows.extend(
        row for row in exporter.as_list(manifest.get("attachments"))
        if exporter.as_dict(row).get("member_path")
    )
    rows.extend(
        row for row in exporter.as_list(manifest.get("artifacts"))
        if exporter.as_dict(row).get("member_path")
    )
    return [exporter.as_dict(row) for row in rows]


def _member_containment_reason(package_dir: Path,
                               member: str) -> Optional[Dict[str, Any]]:
    """Refuse any symlink component on a manifest-attested member path.

    pathlib follows symlinks by default, so a lexically safe relative member
    name can still resolve outside the package. Walk every relative path
    component from the already-resolved package root and reject symlinks
    without ever following, opening or hashing the target — leaf links,
    parent-directory links, links into the package and broken links alike:
    a canonical v0.1 package member is a real regular file, never an alias.

    If the check itself cannot be completed (``OSError``), containment is
    not proven and the member must be treated as cannot-determine, never
    read.
    """
    current = package_dir
    for part in member.split("/"):
        current = current / part
        try:
            is_link = current.is_symlink()
        except OSError:
            return _reason(
                "member_containment_unverifiable",
                "whether a manifest-attested member stays inside the "
                "package could not be determined safely", member)
        if is_link:
            return _reason(
                "member_symlink_not_allowed",
                "a manifest-attested member path contains a symlink "
                "component; package members must be real files inside the "
                "package", member)
    return None


def _diagnose_members(package_dir: Path,
                      rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Explain member-level failures. Never the authority on the verdict."""
    reasons: List[Dict[str, Any]] = []
    for row in rows:
        member = row.get("path") or row.get("member_path")
        if not isinstance(member, str) \
                or not exporter._safe_package_member(member):
            reasons.append(_reason(
                "unsafe_member_path",
                "the manifest names a member path that is not safe to resolve",
                member if isinstance(member, str) else None,
            ))
            continue
        containment = _member_containment_reason(package_dir, member)
        if containment is not None:
            # Never fall through to is_file/stat/hash for a member whose
            # containment failed or could not be proven.
            reasons.append(containment)
            continue
        path = package_dir / member
        try:
            is_file = path.is_file()
        except OSError:
            # Permission / ENAMETOOLONG-style failures mean the member's state
            # cannot be determined at all: it has been proven neither missing
            # nor altered, so this fails closed as UNVERIFIABLE downstream,
            # never as FAIL and never as a traceback.
            reasons.append(_reason(
                "member_state_unverifiable",
                "filesystem metadata for a manifest-attested member could "
                "not be queried", member))
            continue
        if not is_file:
            reasons.append(_reason(
                "member_missing",
                "a manifest-attested member is not present in the package",
                member,
            ))
            continue
        try:
            size = path.stat().st_size
        except OSError:
            reasons.append(_reason(
                "member_unreadable",
                "a manifest-attested member could not be read", member))
            continue
        if size != row.get("bytes"):
            reasons.append(_reason(
                "member_size_mismatch",
                "attested %s bytes, found %s bytes" % (row.get("bytes"), size),
                member,
            ))
            continue
        try:
            digest = exporter.sha256_file(path)
        except OSError:
            reasons.append(_reason(
                "member_unreadable",
                "a manifest-attested member could not be read", member))
            continue
        if digest != row.get("sha256"):
            reasons.append(_reason(
                "member_sha256_mismatch",
                "attested sha256 %s, found %s"
                % (row.get("sha256"), digest),
                member,
            ))
    return reasons


def verify_package(package_dir: Path) -> Dict[str, Any]:
    """Verify one canonical package directory and return a machine receipt."""
    display = exporter.output_path_privacy()
    receipt: Dict[str, Any] = {
        "tool": "codex-preserve",
        "check": "package_integrity",
        "exporter_version": exporter.EXPORTER_VERSION,
        "expected_package_schema_version": exporter.PACKAGE_SCHEMA_VERSION,
        "package": display.normalize_paths(str(package_dir)),
        "package_schema_version": None,
        "members_attested": 0,
        "members_verified": 0,
        "handoff_zip": {"attested_as_current": False, "status": None,
                        "verdict": "not_checked"},
        "attestation_semantics": ATTESTATION_SEMANTICS,
        "reasons": [],
        "verdict": VERDICT_UNVERIFIABLE,
        "exit_code": EXIT_UNVERIFIABLE,
    }

    try:
        resolved = package_dir.expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        receipt["reasons"].append(_reason(
            "package_path_unavailable", "the package path does not exist"))
        return receipt
    receipt["package"] = display.normalize_paths(str(resolved))
    if not resolved.is_dir():
        receipt["reasons"].append(_reason(
            "package_path_not_a_directory",
            "the package path is not a directory"))
        return receipt

    manifest_path = resolved / exporter.PACKAGE_MANIFEST_FILENAME
    try:
        manifest = json.loads(manifest_path.read_text("utf-8"))
    except FileNotFoundError:
        receipt["reasons"].append(_reason(
            "manifest_missing",
            "no %s in the package" % exporter.PACKAGE_MANIFEST_FILENAME))
        return receipt
    except (OSError, UnicodeError, ValueError):
        receipt["reasons"].append(_reason(
            "manifest_unreadable",
            "%s is unreadable or is not valid JSON"
            % exporter.PACKAGE_MANIFEST_FILENAME))
        return receipt
    if not isinstance(manifest, dict):
        receipt["reasons"].append(_reason(
            "manifest_not_an_object",
            "%s does not contain a JSON object"
            % exporter.PACKAGE_MANIFEST_FILENAME))
        return receipt

    schema = manifest.get("package_schema_version")
    receipt["package_schema_version"] = schema
    if schema not in exporter.RECOGNIZED_PACKAGE_SCHEMA_VERSIONS:
        # A verifier that cannot interpret the manifest schema must not report
        # PASS: a future schema could attest members through keys this build
        # never reads, and checking a subset is not a verification.
        supported = ", ".join(exporter.RECOGNIZED_PACKAGE_SCHEMA_VERSIONS)
        if schema is None:
            receipt["reasons"].append(_reason(
                "package_schema_missing",
                "the manifest declares no package_schema_version; "
                "supported: %s" % supported))
        else:
            receipt["reasons"].append(_reason(
                "package_schema_unsupported",
                "package_schema_version %r is not one of the supported "
                "versions %s" % (schema, supported)))
        return receipt

    malformed = _malformed_shape_reasons(manifest)
    if malformed:
        receipt["reasons"].extend(malformed)
        return receipt

    rows = _attested_rows(manifest)
    receipt["members_attested"] = len(rows)
    if not rows:
        receipt["reasons"].append(_reason(
            "manifest_attests_nothing",
            "the manifest lists no package members to verify"))
        return receipt

    member_reasons = _diagnose_members(resolved, rows)
    receipt["members_verified"] = len(rows) - len(member_reasons)
    receipt["reasons"].extend(member_reasons)
    diagnostic_codes = {row["code"] for row in member_reasons}

    # Containment gate: a member whose path is a symlink — or whose
    # containment could not be proven — must never be followed, opened or
    # hashed. The reused integrity primitives read every attested member in
    # one pass, so they are skipped entirely and the evidence-priority
    # aggregation below decides from the diagnostic reasons alone.
    skip_primitives = bool(
        diagnostic_codes
        & {"member_symlink_not_allowed", "member_containment_unverifiable"})

    # Authoritative verdict: the exporter's own integrity primitive. The
    # guard exists so an unforeseen shape can only ever become a controlled
    # UNVERIFIABLE, never a traceback and never a silent PASS.
    primitive_failed = False
    if skip_primitives:
        intact = False
    else:
        try:
            intact = exporter.package_files_current(resolved, manifest)
        except Exception:  # noqa: BLE001 - fail closed, never crash
            # Record the unknown and let the evidence-priority aggregation
            # below decide: a determinate member failure already collected
            # must not be downgraded to UNVERIFIABLE by a simultaneous
            # primitive error.
            primitive_failed = True
            intact = False
            receipt["reasons"].append(_reason(
                "integrity_primitive_error",
                "package integrity verification could not be completed for "
                "this manifest"))

    handoff = exporter.as_dict(manifest.get("handoff_zip"))
    status = handoff.get("status")
    receipt["handoff_zip"]["status"] = status
    if status in _CURRENT_ZIP_STATUSES and not primitive_failed \
            and not skip_primitives:
        receipt["handoff_zip"]["attested_as_current"] = True
        # The attested transfer ZIP is itself a package member: it gets the
        # same containment check before anything follows or reads it.
        zip_member = handoff.get("path")
        containment = None
        if isinstance(zip_member, str) \
                and exporter._safe_package_member(zip_member):
            containment = _member_containment_reason(resolved, zip_member)
        if containment is not None:
            receipt["reasons"].append(containment)
            receipt["handoff_zip"]["verdict"] = "FAIL"
            intact = False
        else:
            # EAFP readability probe: the attested current ZIP is only judged
            # by the reused primitive after a real open/read proves the path
            # is actually readable. An OSError means the ZIP's content is
            # cannot-determine, not proven bad, so the primitive — whose
            # OSError surface collapses to a determinate False — is skipped
            # and the evidence-priority aggregation below decides.
            zip_path = None
            if isinstance(zip_member, str) \
                    and exporter._safe_package_member(zip_member):
                zip_path = resolved / zip_member
            zip_unreadable = False
            if zip_path is not None:
                try:
                    with open(zip_path, "rb") as probe:
                        probe.read(1)
                except OSError:
                    zip_unreadable = True
                    receipt["reasons"].append(_reason(
                        "member_unreadable",
                        "the manifest-attested current derived transfer ZIP "
                        "could not be read; whether it matches the canonical "
                        "package could not be determined", zip_member))
                    receipt["handoff_zip"]["verdict"] = "UNVERIFIABLE"
                    intact = False
            if not zip_unreadable:
                try:
                    zip_ok = exporter.handoff_zip_current(resolved, manifest)
                except Exception:  # noqa: BLE001 - fail closed, never crash
                    receipt["reasons"].append(_reason(
                        "integrity_primitive_error",
                        "derived transfer ZIP verification could not be "
                        "completed"))
                    intact = False
                else:
                    receipt["handoff_zip"]["verdict"] = \
                        "PASS" if zip_ok else "FAIL"
                    if not zip_ok:
                        intact = False
                        receipt["reasons"].append(_reason(
                            "handoff_zip_mismatch",
                            "the manifest attests a current derived transfer "
                            "ZIP that does not match the canonical package",
                            handoff.get("path")
                            if isinstance(handoff.get("path"), str) else None,
                        ))
    elif status in _CURRENT_ZIP_STATUSES:
        receipt["handoff_zip"]["attested_as_current"] = True
    else:
        receipt["handoff_zip"]["verdict"] = "not_attested_as_current"

    if intact:
        receipt["verdict"] = VERDICT_PASS
        receipt["exit_code"] = EXIT_PASS
        receipt["reasons"] = []
        return receipt

    codes = {row["code"] for row in receipt["reasons"]}
    if codes & _DETERMINATE_BAD_REASONS:
        # A proven missing/altered member outranks any simultaneous unknown.
        receipt["verdict"] = VERDICT_FAIL
        receipt["exit_code"] = EXIT_FAIL
        return receipt
    if codes & _CANNOT_DETERMINE_REASONS:
        # Unknown state with no determinate-bad evidence: FAIL would claim
        # more than is known.
        receipt["verdict"] = VERDICT_UNVERIFIABLE
        receipt["exit_code"] = EXIT_UNVERIFIABLE
        return receipt

    if not receipt["reasons"]:
        # Fail closed: the primitive refused and the diagnostic pass could not
        # say why, so the package is not provably intact.
        receipt["reasons"].append(_reason(
            "integrity_primitive_failed",
            "package integrity verification failed without a member-level "
            "explanation"))
    receipt["verdict"] = VERDICT_FAIL
    receipt["exit_code"] = EXIT_FAIL
    return receipt


def render_human(receipt: Dict[str, Any]) -> str:
    """One short human-readable block; the reason is always stated."""
    lines = [
        "codex-preserve verify: %s" % receipt["verdict"],
        "  package: %s" % receipt["package"],
        "  schema:  %s (expected %s)" % (
            receipt.get("package_schema_version"),
            receipt["expected_package_schema_version"],
        ),
        "  members: %d/%d verified" % (receipt["members_verified"],
                                       receipt["members_attested"]),
        "  transfer zip: %s" % receipt["handoff_zip"]["verdict"],
    ]
    for row in receipt["reasons"]:
        member = row.get("member")
        lines.append("  reason: %s%s — %s" % (
            row["code"], (" [%s]" % member) if member else "", row["detail"]))
    lines.append("  attests: %s" % receipt["attestation_semantics"])
    return "\n".join(lines)
