"""Content hashing utilities."""

from __future__ import annotations

import hashlib
from pathlib import Path


def file_hash(file_path: Path | str, algorithm: str = "sha256") -> str:
    """Compute hash of a file's contents."""
    sha = hashlib.new(algorithm)
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha.update(chunk)
    return sha.hexdigest()


def text_hash(text: str, algorithm: str = "sha256") -> str:
    """Compute hash of a text string."""
    return hashlib.new(algorithm, text.encode("utf-8")).hexdigest()


def short_hash(text: str, length: int = 16) -> str:
    """Short hash for IDs."""
    return text_hash(text)[:length]
