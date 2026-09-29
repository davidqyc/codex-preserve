"""Provider-neutral byte and filesystem primitives used by package export.

This module contains no conversation model, provider discovery, status policy,
schema construction, or receipt rendering. Existing Codex entry points retain
their names in ``exporter`` for compatibility with internal callers.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional


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


def json_bytes(value: Dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)
            + "\n").encode("utf-8")


def safe_package_member(member: str) -> bool:
    if not member or member.startswith(("/", "\\")) or "\\" in member:
        return False
    parts = member.split("/")
    return all(part not in ("", ".", "..") for part in parts)


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
