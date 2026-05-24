from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


@dataclass
class PendingOperationsStore:
    path: Path

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("[]", encoding="utf-8")

    def enqueue(self, operation: str, payload: Dict[str, Any]) -> None:
        entries = self.read_all()
        entries.append(
            {
                "operation": operation,
                "payload": payload,
                "recorded_at": datetime.now().isoformat(),
            }
        )
        self.path.write_text(json.dumps(entries, indent=2), encoding="utf-8")

    def read_all(self) -> List[Dict[str, Any]]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def clear(self) -> None:
        self.path.write_text("[]", encoding="utf-8")
