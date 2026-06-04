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

from temms.core.database import Database


class SlotState(str, Enum):
    """Slot operational state."""
    STOPPED = "stopped"
    LOADING = "loading"
    RUNNING = "running"
    ERROR = "error"


@dataclass
class OperatorOverride:
    """Active operator override for a slot."""
    model_id: str
    reason: str
    source: str  # operator ID or "api"
    set_at: datetime
    expires_at: Optional[datetime] = None

    def is_expired(self) -> bool:
        """Check if the override has expired."""
        if self.expires_at is None:
            return False
        return datetime.now() >= self.expires_at


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
    operator_override: Optional[OperatorOverride] = None

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
            "operator_override": {
                "model_id": self.operator_override.model_id,
                "reason": self.operator_override.reason,
                "source": self.operator_override.source,
                "set_at": self.operator_override.set_at.isoformat(),
                "expires_at": self.operator_override.expires_at.isoformat()
                if self.operator_override.expires_at else None,
            } if self.operator_override else None,
        }


class SlotManager(Database):
    """Manages model slots for multi-model deployment."""

    def _init_tables(self) -> None:
        """Initialize slots database."""
        self.execute("""
            CREATE TABLE IF NOT EXISTS slots (
                name TEXT PRIMARY KEY,
                description TEXT,
                required BOOLEAN DEFAULT false,
                default_model TEXT,
                active_model_id TEXT,
                state TEXT DEFAULT 'stopped',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                candidates JSON,
                metadata JSON,
                override_model_id TEXT,
                override_reason TEXT,
                override_source TEXT,
                override_set_at TIMESTAMP,
                override_expires_at TIMESTAMP
            )
        """)

        # Decision log - every model switch
        self.execute("""
            CREATE TABLE IF NOT EXISTS slot_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slot TEXT NOT NULL,
                from_model TEXT,
                to_model TEXT,
                trigger_type TEXT,
                trigger_detail TEXT,
                conditions_snapshot JSON,
                audit_metadata JSON,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        columns = {row["name"] for row in self.fetchall("PRAGMA table_info(slot_decisions)")}
        if "audit_metadata" not in columns:
            self.execute("ALTER TABLE slot_decisions ADD COLUMN audit_metadata JSON")

        self.conn.commit()

    @staticmethod
    def _row_to_slot(row: sqlite3.Row) -> Slot:
        """Map a database row to a Slot."""
        override = None
        if row["override_model_id"] is not None:
            override = OperatorOverride(
                model_id=row["override_model_id"],
                reason=row["override_reason"] or "",
                source=row["override_source"] or "unknown",
                set_at=datetime.fromisoformat(row["override_set_at"])
                if row["override_set_at"] else datetime.now(),
                expires_at=datetime.fromisoformat(row["override_expires_at"])
                if row["override_expires_at"] else None,
            )

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
            operator_override=override,
        )

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

        self.execute_and_commit(
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
        return self.fetch_one_mapped(
            "SELECT * FROM slots WHERE name = ?",
            (name,),
            self._row_to_slot,
        )

    def list_slots(self) -> List[Slot]:
        """List all slots."""
        return self.fetch_all_mapped(
            "SELECT * FROM slots",
            (),
            self._row_to_slot,
        )

    def activate_model(
        self,
        slot_name: str,
        model_id: str,
        trigger_type: str,
        trigger_detail: str,
        conditions: Optional[Dict[str, Any]] = None,
        audit_metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Activate a model in a slot.

        Args:
            slot_name: Slot name
            model_id: Model ID to activate
            trigger_type: policy, operator, fallback, startup
            trigger_detail: Policy name or operator ID
            conditions: Current condition snapshot
            audit_metadata: Model/package/provenance details for evidence exports
        """
        slot = self.get_slot(slot_name)
        if not slot:
            raise ValueError(f"Slot not found: {slot_name}")

        from_model = slot.active_model_id
        updated_at = datetime.now()
        conditions = conditions or {}
        audit_metadata = audit_metadata or {}

        # Update slot
        self.execute(
            """
            UPDATE slots
            SET active_model_id = ?, state = ?, updated_at = ?
            WHERE name = ?
            """,
            (model_id, SlotState.RUNNING.value, updated_at, slot_name),
        )

        # Log decision
        self.execute(
            """
            INSERT INTO slot_decisions
            (slot, from_model, to_model, trigger_type, trigger_detail, conditions_snapshot, audit_metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                slot_name,
                from_model,
                model_id,
                trigger_type,
                trigger_detail,
                json.dumps(conditions),
                json.dumps(audit_metadata),
            ),
        )

        self.conn.commit()

    def set_operator_override(
        self,
        slot_name: str,
        model_id: str,
        reason: str = "",
        source: str = "api",
        duration_s: Optional[int] = None,
    ) -> None:
        """
        Set an operator override for a slot.

        When an override is active, the policy engine should skip
        evaluation for this slot.

        Args:
            slot_name: Target slot
            model_id: Model to force
            reason: Human-readable reason
            source: Override source identifier
            duration_s: Override duration in seconds (None = permanent until cleared)
        """
        slot = self.get_slot(slot_name)
        if not slot:
            raise ValueError(f"Slot not found: {slot_name}")

        now = datetime.now()
        expires_at = None
        if duration_s is not None:
            from datetime import timedelta
            expires_at = now + timedelta(seconds=duration_s)

        self.execute_and_commit(
            """
            UPDATE slots
            SET override_model_id = ?, override_reason = ?,
                override_source = ?, override_set_at = ?,
                override_expires_at = ?, updated_at = ?
            WHERE name = ?
            """,
            (model_id, reason, source, now, expires_at, now, slot_name),
        )

    def clear_operator_override(self, slot_name: str) -> None:
        """Clear operator override for a slot."""
        self.execute_and_commit(
            """
            UPDATE slots
            SET override_model_id = NULL, override_reason = NULL,
                override_source = NULL, override_set_at = NULL,
                override_expires_at = NULL, updated_at = ?
            WHERE name = ?
            """,
            (datetime.now(), slot_name),
        )

    def has_active_override(self, slot_name: str) -> bool:
        """
        Check if a slot has an active (non-expired) operator override.

        Also cleans up expired overrides automatically.
        """
        slot = self.get_slot(slot_name)
        if slot is None or slot.operator_override is None:
            return False

        if slot.operator_override.is_expired():
            self.clear_operator_override(slot_name)
            return False

        return True

    def get_decision_log(self, slot_name: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        """Get decision log for audit."""
        if slot_name:
            rows = self.fetchall(
                """
                SELECT * FROM slot_decisions
                WHERE slot = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (slot_name, limit),
            )
        else:
            rows = self.fetchall(
                """
                SELECT * FROM slot_decisions
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )

        return [dict(row) for row in rows]

    def update_slot_state(self, slot_name: str, state: SlotState) -> None:
        """Update slot state."""
        self.execute_and_commit(
            "UPDATE slots SET state = ?, updated_at = ? WHERE name = ?",
            (state.value, datetime.now(), slot_name),
        )
