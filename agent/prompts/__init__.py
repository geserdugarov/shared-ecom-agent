"""Markdown prompt loaders. Read alongside this package on demand."""

from __future__ import annotations

from pathlib import Path


_HERE = Path(__file__).resolve().parent


def _read(name: str) -> str:
    return (_HERE / name).read_text(encoding="utf-8")


CODEX_PREAMBLE = _read("codex_preamble.md")
INSTRUCTIONS = _read("instructions.md")
