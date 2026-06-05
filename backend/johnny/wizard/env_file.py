"""Read / write / merge ``.env`` files.

Intentionally not a wrapper around ``python-dotenv``: we want to preserve
comments and ordering on disk so the file the user sees post-wizard
matches the structure of ``.env.example``. We just patch values in-place.

The parser is tolerant: it accepts ``KEY=value`` lines with optional
surrounding whitespace and quoted values, skips blank lines and
``#`` comments, and round-trips unchanged content verbatim.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from pathlib import Path


def parse_env(text: str) -> dict[str, str]:
    """Parse ``KEY=value`` lines into a dict.

    Comments (``#``) and blank lines are ignored. Quoted values
    (``'…'`` or ``"…"``) are unquoted. Lines without ``=`` are skipped
    silently to match shell-style ``set -a; source .env`` behavior.
    """
    out: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key] = value
    return out


def read_env_file(path: Path) -> dict[str, str]:
    """Read ``path`` into a key→value dict. Returns ``{}`` if absent."""
    if not path.exists():
        return {}
    return parse_env(path.read_text(encoding="utf-8"))


def ensure_env_file(target: Path, template: Path) -> bool:
    """Create ``target`` by copying ``template`` if it does not exist.

    Returns ``True`` if the file was created, ``False`` if it was already
    present. Raises :class:`FileNotFoundError` if the template is missing.
    """
    if target.exists():
        return False
    if not template.exists():
        raise FileNotFoundError(f"template .env not found at {template}")
    shutil.copyfile(template, target)
    return True


def write_env_values(path: Path, updates: Mapping[str, str]) -> None:
    """Patch ``updates`` into ``path`` in-place, preserving order/comments.

    For each key in ``updates``:

    * If a line ``KEY=…`` already exists, its value is replaced.
    * Otherwise the line is appended to the end of the file.

    The file is rewritten atomically by writing a sibling tempfile and
    renaming it over the original.
    """
    if not updates:
        return

    if path.exists():
        existing_lines = path.read_text(encoding="utf-8").splitlines()
    else:
        existing_lines = []

    seen: set[str] = set()
    new_lines: list[str] = []
    for raw_line in existing_lines:
        line = raw_line
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _, _ = stripped.partition("=")
            key = key.strip()
            if key in updates:
                line = f"{key}={updates[key]}"
                seen.add(key)
        new_lines.append(line)

    for key, value in updates.items():
        if key in seen:
            continue
        if new_lines and new_lines[-1].strip():
            new_lines.append("")
        new_lines.append(f"{key}={value}")

    text = "\n".join(new_lines)
    if not text.endswith("\n"):
        text = text + "\n"

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


__all__ = ["ensure_env_file", "parse_env", "read_env_file", "write_env_values"]
