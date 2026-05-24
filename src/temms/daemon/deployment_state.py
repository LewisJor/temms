from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


class DeploymentState(str, Enum):
    PENDING = "PENDING"
    DOWNLOADING = "DOWNLOADING"
    READY = "READY"
    FAILED = "FAILED"
    OFFLINE = "OFFLINE"
    DEGRADED = "DEGRADED"


@dataclass
class DeploymentStateStore:
    path: Path

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.set_state(DeploymentState.PENDING, "bootstrap")

    def get_state(self) -> DeploymentState:
        payload = self._read()
        return DeploymentState(payload.get("state", DeploymentState.PENDING.value))

    def set_state(self, state: DeploymentState | str, reason: str) -> None:
        normalized = state.value if isinstance(state, DeploymentState) else str(state)
        payload = {
            "state": normalized,
            "reason": reason,
            "updated_at": datetime.now().isoformat(),
        }
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        logger.info("Deployment state transition -> %s (%s)", normalized, reason)

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {"state": DeploymentState.PENDING.value}
