"""
Slot manager for concurrent multi-model deployment.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from temms.core.database import Database
from temms.observability import model_swaps_total
from temms.slots.decision_chain import DECISION_CHAIN_GENESIS, DecisionChain

# DECISION_CHAIN_GENESIS is re-exported from decision_chain for existing importers
# (evidence.py, tests) that reference it via this module.
__all__ = ["SlotManager", "Slot", "SlotState", "OperatorOverride", "DECISION_CHAIN_GENESIS"]


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
    expires_at: datetime | None = None

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
    default_model: str | None
    active_model_id: str | None
    state: SlotState
    updated_at: datetime
    candidates: list[str]  # Model names that can run in this slot
    metadata: dict[str, Any]
    operator_override: OperatorOverride | None = None

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

        # The tamper-evident decision chain owns the slot_decisions table and its
        # schema/backfill (issue #27). SlotManager composes one and delegates.
        self._chain = DecisionChain(self)
        self._chain.ensure_schema()

    @staticmethod
    def _decision_entry_hash(content: dict[str, Any], prev_hash: str) -> str:
        """Hash linking a decision to the previous one (kept for offline verifiers)."""
        return DecisionChain.entry_hash(content, prev_hash)

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
        default_model: str | None = None,
        candidates: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
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

    def get_slot(self, name: str) -> Slot | None:
        """Get slot by name."""
        return self.fetch_one_mapped(
            "SELECT * FROM slots WHERE name = ?",
            (name,),
            self._row_to_slot,
        )

    def list_slots(self) -> list[Slot]:
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
        conditions: dict[str, Any] | None = None,
        audit_metadata: dict[str, Any] | None = None,
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

        # Append the decision to the tamper-evident chain. The commit below makes
        # the slot update and the decision entry land atomically.
        self._chain.append(
            {
                "slot": slot_name,
                "from_model": from_model,
                "to_model": model_id,
                "trigger_type": trigger_type,
                "trigger_detail": trigger_detail,
                "conditions_snapshot": conditions,
                "audit_metadata": audit_metadata,
                "created_at": updated_at.isoformat(),
            }
        )

        self.conn.commit()
        model_swaps_total.inc()

    def decision_count(self) -> int:
        """Number of decisions in the chain (cheap; for metrics)."""
        return self._chain.count()

    def verify_decision_chain(self) -> dict[str, Any]:
        """Verify the tamper-evident decision chain end to end."""
        return self._chain.verify()

    def decision_chain_head(self) -> str:
        """Return the current head hash of the decision chain."""
        return self._chain.head()

    def export_decision_chain(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Return the ordered decision chain (content + hashes) for offline audit."""
        return self._chain.export(limit)

    def sign_decision_chain_head(self, signing_key: str, signer: str = "temms") -> dict[str, Any]:
        """Sign the current chain head so the log is offline-verifiable (issue #27)."""
        return self._chain.sign_head(signing_key, signer)

    def set_operator_override(
        self,
        slot_name: str,
        model_id: str,
        reason: str = "",
        source: str = "api",
        duration_s: int | None = None,
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

    def get_decision_log(self, slot_name: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
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
