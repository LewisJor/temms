from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from temms.core.atomic import write_json_atomic


@dataclass
class PendingOperationsStore:
    path: Path

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            write_json_atomic(self.path, [])

    def enqueue(self, operation: str, payload: Dict[str, Any]) -> None:
        entries = self.read_all()
        entries.append(
            {
                "operation": operation,
                "payload": payload,
                "recorded_at": datetime.now().isoformat(),
            }
        )
        write_json_atomic(self.path, entries, indent=2)

    def read_all(self) -> List[Dict[str, Any]]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def clear(self) -> None:
        write_json_atomic(self.path, [])
