"""
Slot manager for concurrent multi-model deployment.
"""

import sqlite3
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime
from dataclasses import dataclass
from enum import Enum
import json


class SlotState(str, Enum):
    """Slot operational state."""
    STOPPED = "stopped"
    LOADING = "loading"
    RUNNING = "running"
    ERROR = "error"


@dataclass
class Slot:
    """Model slot configuration and state."""
    name: str
    description: str
    required: bool  # Robot won't operate without this slot
    default_model: Optional[str]
    active_model_id: Optional[str]
    state: SlotState
    updated_at: datetime
    candidates: List[str]  # Model names that can run in this slot
    metadata: Dict[str, Any]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "required": self.required,
            "default_model": self.default_model,
            "active_model_id": self.active_model_id,
            "state": self.state.value,
            "updated_at": self.updated_at.isoformat(),
            "candidates": self.candidates,
            "metadata": self.metadata,
        }


class SlotManager:
    """Manages model slots for multi-model deployment."""

    def __init__(self, db_path: Path):
        """Initialize slot manager."""
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        """Initialize slots database."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS slots (
                    name TEXT PRIMARY KEY,
                    description TEXT,
                    required BOOLEAN DEFAULT false,
                    default_model TEXT,
                    active_model_id TEXT,
                    state TEXT DEFAULT 'stopped',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    candidates JSON,
                    metadata JSON
                )
            """)

            # Decision log - every model switch
            conn.execute("""
                CREATE TABLE IF NOT EXISTS slot_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slot TEXT NOT NULL,
                    from_model TEXT,
                    to_model TEXT,
                    trigger_type TEXT,
                    trigger_detail TEXT,
                    conditions_snapshot JSON,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            conn.commit()

    def create_slot(
        self,
        name: str,
        description: str,
        required: bool = False,
        default_model: Optional[str] = None,
        candidates: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Slot:
        """Create a new slot."""
        candidates = candidates or []
        metadata = metadata or {}
        updated_at = datetime.now()

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO slots (name, description, required, default_model, state, updated_at, candidates, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    description,
                    required,
                    default_model,
                    SlotState.STOPPED.value,
                    updated_at,
                    json.dumps(candidates),
                    json.dumps(metadata),
                ),
            )
            conn.commit()

        return Slot(
            name=name,
            description=description,
            required=required,
            default_model=default_model,
            active_model_id=None,
            state=SlotState.STOPPED,
            updated_at=updated_at,
            candidates=candidates,
            metadata=metadata,
        )

    def get_slot(self, name: str) -> Optional[Slot]:
        """Get slot by name."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT * FROM slots WHERE name = ?", (name,))
            row = cursor.fetchone()

        if not row:
            return None

        return Slot(
            name=row["name"],
            description=row["description"],
            required=bool(row["required"]),
            default_model=row["default_model"],
            active_model_id=row["active_model_id"],
            state=SlotState(row["state"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            candidates=json.loads(row["candidates"]),
            metadata=json.loads(row["metadata"]),
        )

    def list_slots(self) -> List[Slot]:
        """List all slots."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT * FROM slots")
            rows = cursor.fetchall()

        return [
            Slot(
                name=row["name"],
                description=row["description"],
                required=bool(row["required"]),
                default_model=row["default_model"],
                active_model_id=row["active_model_id"],
                state=SlotState(row["state"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
                candidates=json.loads(row["candidates"]),
                metadata=json.loads(row["metadata"]),
            )
            for row in rows
        ]

    def activate_model(
        self,
        slot_name: str,
        model_id: str,
        trigger_type: str,
        trigger_detail: str,
        conditions: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Activate a model in a slot.

        Args:
            slot_name: Slot name
            model_id: Model ID to activate
            trigger_type: policy, operator, fallback, startup
            trigger_detail: Policy name or operator ID
            conditions: Current condition snapshot
        """
        slot = self.get_slot(slot_name)
        if not slot:
            raise ValueError(f"Slot not found: {slot_name}")

        from_model = slot.active_model_id
        updated_at = datetime.now()
        conditions = conditions or {}

        with sqlite3.connect(self.db_path) as conn:
            # Update slot
            conn.execute(
                """
                UPDATE slots
                SET active_model_id = ?, state = ?, updated_at = ?
                WHERE name = ?
                """,
                (model_id, SlotState.RUNNING.value, updated_at, slot_name),
            )

            # Log decision
            conn.execute(
                """
                INSERT INTO slot_decisions
                (slot, from_model, to_model, trigger_type, trigger_detail, conditions_snapshot)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    slot_name,
                    from_model,
                    model_id,
                    trigger_type,
                    trigger_detail,
                    json.dumps(conditions),
                ),
            )

            conn.commit()

    def get_decision_log(self, slot_name: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        """Get decision log for audit."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            if slot_name:
                cursor = conn.execute(
                    """
                    SELECT * FROM slot_decisions
                    WHERE slot = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (slot_name, limit),
                )
            else:
                cursor = conn.execute(
                    """
                    SELECT * FROM slot_decisions
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            rows = cursor.fetchall()

        return [dict(row) for row in rows]

    def update_slot_state(self, slot_name: str, state: SlotState) -> None:
        """Update slot state."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE slots SET state = ?, updated_at = ? WHERE name = ?",
                (state.value, datetime.now(), slot_name),
            )
            conn.commit()
