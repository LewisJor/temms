"""
Offline-first telemetry buffer.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


class TelemetryBuffer:
    """Append-only JSONL telemetry store for disconnected edge agents."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("", encoding="utf-8")

    def append(
        self,
        event_type: str,
        payload: dict[str, Any],
        source: str = "edge-agent",
    ) -> dict[str, Any]:
        """Append an event and return the stored envelope."""
        event = {
            "event_id": str(uuid4()),
            "event_type": event_type,
            "source": source,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "payload": payload,
        }
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(event, sort_keys=True) + "\n")
        return event

    def read(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Read telemetry events in arrival order."""
        events: list[dict[str, Any]] = []
        if not self.path.exists():
            return events
        with self.path.open("r", encoding="utf-8") as file:
            for line in file:
                if not line.strip():
                    continue
                events.append(json.loads(line))
        if limit is not None:
            return events[-limit:]
        return events

    def export_bundle(self, limit: int | None = None) -> dict[str, Any]:
        """Return an air-gap friendly telemetry bundle."""
        events = self.read(limit=limit)
        return {
            "schema_version": "temms-telemetry-bundle/v1",
            "exported_at": datetime.utcnow().isoformat() + "Z",
            "events": events,
            "count": len(events),
        }

    def clear(self) -> int:
        """Clear all buffered events and return the number removed."""
        count = len(self.read())
        self.path.write_text("", encoding="utf-8")
        return count

    def replay(self, clear: bool = False) -> dict[str, Any]:
        """Mark events as replayed locally, optionally clearing the buffer."""
        events = self.read()
        replayed = len(events)
        if clear:
            self.clear()
        return {
            "status": "success",
            "replayed": replayed,
            "cleared": clear,
            "replayed_at": datetime.utcnow().isoformat() + "Z",
        }
