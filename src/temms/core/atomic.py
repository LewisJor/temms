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
    """Write text through a same-directory temp file and atomic replace.

    A reader therefore sees either the previous contents or the new ones, never
    a partial write — which is what makes the DDIL state files survive an abrupt
    kill mid-write (issue #29).

    The parent directory is fsynced after the replace: rename() is atomic, but
    the *directory entry* is not durable until the directory itself is synced,
    so without this a power loss could lose an already-committed replace.
    """
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
        _fsync_directory(path.parent)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _fsync_directory(directory: Path) -> None:
    """Flush a directory entry so a completed rename survives power loss.

    Not every platform allows opening a directory for fsync (Windows does not),
    and the durability gain is unavailable rather than wrong there, so a failure
    here must not fail the write that already succeeded.
    """
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


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
