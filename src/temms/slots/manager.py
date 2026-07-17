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
from temms.core.mission_package import canonical_json_hash

# Genesis link for the tamper-evident decision chain (issue #27).
DECISION_CHAIN_GENESIS = "0" * 64


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
        # Tamper-evident decision chain (issue #27): every decision embeds the
        # hash of the previous one, so deletion/reorder/mutation is detectable
        # offline against the signed chain head.
        for chain_column in ("entry_hash", "prev_hash"):
            if chain_column not in columns:
                self.execute(f"ALTER TABLE slot_decisions ADD COLUMN {chain_column} TEXT")

        self.conn.commit()
        self._backfill_decision_chain()

    @staticmethod
    def _decision_entry_hash(content: Dict[str, Any], prev_hash: str) -> str:
        """Return the canonical hash linking one decision to the previous one."""
        return canonical_json_hash(
            {
                "slot": content.get("slot"),
                "from_model": content.get("from_model"),
                "to_model": content.get("to_model"),
                "trigger_type": content.get("trigger_type"),
                "trigger_detail": content.get("trigger_detail"),
                "conditions_snapshot": content.get("conditions_snapshot"),
                "audit_metadata": content.get("audit_metadata"),
                "created_at": content.get("created_at"),
                "prev_hash": prev_hash,
            }
        )

    @staticmethod
    def _decision_chain_content(row: sqlite3.Row) -> Dict[str, Any]:
        """Parse a decision row into the content hashed for the chain."""
        return {
            "slot": row["slot"],
            "from_model": row["from_model"],
            "to_model": row["to_model"],
            "trigger_type": row["trigger_type"],
            "trigger_detail": row["trigger_detail"],
            "conditions_snapshot": json.loads(row["conditions_snapshot"] or "{}"),
            "audit_metadata": json.loads(row["audit_metadata"] or "{}"),
            "created_at": row["created_at"],
        }

    def _latest_chain_hash(self) -> str:
        row = self.fetchone(
            "SELECT entry_hash FROM slot_decisions WHERE entry_hash IS NOT NULL "
            "ORDER BY id DESC LIMIT 1"
        )
        return row["entry_hash"] if row and row["entry_hash"] else DECISION_CHAIN_GENESIS

    def _backfill_decision_chain(self) -> None:
        """Compute chain links for any legacy rows written before the chain existed."""
        rows = self.fetchall(
            "SELECT * FROM slot_decisions WHERE entry_hash IS NULL ORDER BY id ASC"
        )
        if not rows:
            return
        prev_hash = self._latest_chain_hash()
        for row in rows:
            content = self._decision_chain_content(row)
            entry_hash = self._decision_entry_hash(content, prev_hash)
            self.execute(
                "UPDATE slot_decisions SET entry_hash = ?, prev_hash = ? WHERE id = ?",
                (entry_hash, prev_hash, row["id"]),
            )
            prev_hash = entry_hash
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

        # Log decision, linked into the tamper-evident chain.
        created_at = updated_at.isoformat()
        prev_hash = self._latest_chain_hash()
        entry_hash = self._decision_entry_hash(
            {
                "slot": slot_name,
                "from_model": from_model,
                "to_model": model_id,
                "trigger_type": trigger_type,
                "trigger_detail": trigger_detail,
                "conditions_snapshot": conditions,
                "audit_metadata": audit_metadata,
                "created_at": created_at,
            },
            prev_hash,
        )
        self.execute(
            """
            INSERT INTO slot_decisions
            (slot, from_model, to_model, trigger_type, trigger_detail,
             conditions_snapshot, audit_metadata, created_at, prev_hash, entry_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                slot_name,
                from_model,
                model_id,
                trigger_type,
                trigger_detail,
                json.dumps(conditions),
                json.dumps(audit_metadata),
                created_at,
                prev_hash,
                entry_hash,
            ),
        )

        self.conn.commit()

    def verify_decision_chain(self) -> Dict[str, Any]:
        """Verify the decision chain end to end.

        Recomputes each entry hash and checks that every entry links to the
        previous one. Detects deletion, reordering, or mutation of any decision.
        """
        rows = self.fetchall("SELECT * FROM slot_decisions ORDER BY id ASC")
        prev_hash = DECISION_CHAIN_GENESIS
        for index, row in enumerate(rows):
            if row["prev_hash"] != prev_hash:
                return {
                    "valid": False,
                    "length": len(rows),
                    "broken_at": index,
                    "reason": "prev_hash link mismatch",
                }
            expected = self._decision_entry_hash(
                self._decision_chain_content(row), prev_hash
            )
            if expected != row["entry_hash"]:
                return {
                    "valid": False,
                    "length": len(rows),
                    "broken_at": index,
                    "reason": "entry content does not match its hash",
                }
            prev_hash = row["entry_hash"]
        return {"valid": True, "length": len(rows), "head_hash": prev_hash}

    def decision_chain_head(self) -> str:
        """Return the current head hash of the decision chain."""
        return self._latest_chain_hash()

    def sign_decision_chain_head(self, signing_key: str, signer: str = "temms") -> Dict[str, Any]:
        """Sign the current chain head so the log is offline-verifiable (issue #27)."""
        from temms.core.signing import ed25519_sign, signing_key_fingerprint

        head = self._latest_chain_hash()
        verification = self.verify_decision_chain()
        return {
            "schema_version": "temms-decision-chain-head/v1",
            "head_hash": head,
            "length": verification.get("length", 0),
            "signed_at": datetime.now().isoformat(),
            "signer": signer,
            "key_fingerprint": signing_key_fingerprint(signing_key),
            "signature": ed25519_sign(head.encode("utf-8"), signing_key),
        }

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
