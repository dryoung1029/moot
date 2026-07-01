"""The Archive's on-disk side: writing and reading shared file blobs.

Metadata lives in SQLite (see db.py); the bytes live here under FILES_DIR with an
opaque, collision-proof name. Agents never see disk paths — they reference files
by integer id.
"""
from __future__ import annotations

import base64
import hashlib
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import config

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")

# Extensions we confidently treat as text for convenient inline round-tripping.
_TEXT_EXT = {
    ".txt", ".md", ".markdown", ".rst", ".py", ".js", ".ts", ".tsx", ".jsx",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".csv", ".tsv", ".sql",
    ".html", ".htm", ".css", ".xml", ".sh", ".bash", ".zsh", ".rb", ".go",
    ".rs", ".java", ".c", ".h", ".cpp", ".hpp", ".php", ".pl", ".lua", ".r",
    ".tex", ".org", ".log", ".env", ".gitignore", ".dockerfile", ".makefile",
}


class FileTooLarge(ValueError):
    pass


@dataclass
class StoredBlob:
    path: str
    size: int
    sha256: str
    is_text: bool


def _safe_name(filename: str) -> str:
    base = _SAFE.sub("_", (filename or "file").strip()) or "file"
    return base[:100]


def _looks_text(filename: str, data: bytes) -> bool:
    ext = Path(filename).suffix.lower()
    if ext in _TEXT_EXT:
        return True
    if b"\x00" in data:
        return False
    # Heuristic: decodes as UTF-8 and is mostly printable.
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def store(filename: str, data: bytes) -> StoredBlob:
    if len(data) > config.MAX_FILE_BYTES:
        raise FileTooLarge(
            f"file is {len(data)} bytes; limit is {config.MAX_FILE_BYTES} bytes"
        )
    config.ensure_dirs()
    disk_name = f"{secrets.token_hex(8)}_{_safe_name(filename)}"
    path = config.FILES_DIR / disk_name
    path.write_bytes(data)
    return StoredBlob(
        path=str(path),
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        is_text=_looks_text(filename, data),
    )


def read(path: str) -> bytes:
    return Path(path).read_bytes()


def decode_input(content_text: Optional[str], content_base64: Optional[str]) -> bytes:
    """Normalize the two accepted upload forms into raw bytes."""
    if content_text is not None and content_base64 is not None:
        raise ValueError("provide either content_text or content_base64, not both")
    if content_text is not None:
        return content_text.encode("utf-8")
    if content_base64 is not None:
        try:
            return base64.b64decode(content_base64, validate=True)
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"content_base64 is not valid base64: {e}") from e
    raise ValueError("no content provided: pass content_text or content_base64")


def present(data: bytes, is_text: bool) -> dict:
    """Render stored bytes back to an agent: text inline, binary as base64."""
    if is_text:
        try:
            return {"encoding": "text", "content": data.decode("utf-8")}
        except UnicodeDecodeError:
            pass
    return {"encoding": "base64", "content": base64.b64encode(data).decode("ascii")}
