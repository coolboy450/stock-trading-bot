"""Tiny JSON file helpers: atomic writes (temp file + rename), tolerant reads."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def read_json(path: str | Path, default: Any = None) -> Any:
    """Return the parsed file, or ``default`` if it is missing / unreadable / not valid JSON."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json_atomic(path: str | Path, data: Any) -> None:
    """Write ``data`` as JSON via temp file + ``os.replace`` so readers never see a half-written file.

    The temp file lives in the same folder (``.<name>-*.tmp``, git-ignored) so the rename is atomic.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=1, default=str)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.stem}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
