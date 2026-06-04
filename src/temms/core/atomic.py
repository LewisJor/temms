"""
Atomic local file write helpers.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_text_atomic(path: Path, content: str, encoding: str = "utf-8") -> None:
    """Write text through a same-directory temp file and atomic replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}-",
        dir=path.parent,
        text=True,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        temp_path.replace(path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def write_json_atomic(
    path: Path,
    data: Any,
    *,
    indent: int | None = 2,
    sort_keys: bool = False,
    encoding: str = "utf-8",
) -> None:
    """Write JSON through an atomic text replace."""
    write_text_atomic(
        path,
        json.dumps(data, indent=indent, sort_keys=sort_keys),
        encoding=encoding,
    )
