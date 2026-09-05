"""Content hashing helpers used for provenance and for the LLM cache key."""

from __future__ import annotations

import hashlib
from pathlib import Path


def sha256_file(path: str) -> str:
    """SHA-256 of a file's raw bytes, for the run manifest's input fingerprints."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    """SHA-256 of a string, used as the LLM cache key."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
