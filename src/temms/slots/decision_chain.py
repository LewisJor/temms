"""Tamper-evident decision chain (issue #27), extracted from SlotManager (#16/review).

Every model activation is appended as a link in a hash chain: each entry embeds
the canonical hash of the previous one, so deletion, reordering, or mutation of
any past decision is detectable offline against the signed chain head. This is
the record the provenance guarantee rests on.

It lives in its own class rather than inside ``SlotManager`` because it is a
distinct responsibility — a cryptographic audit log over the ``slot_decisions``
table — with its own schema, invariants, and tests. ``SlotManager`` composes one
and delegates to it; the chain only needs a database handle
(``execute`` / ``fetchall`` / ``fetchone`` / ``conn``).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from temms.core.database import Database
from temms.core.mission_package import canonical_json_hash

# Genesis link: the prev_hash of the very first decision.
DECISION_CHAIN_GENESIS = "0" * 64

# The decision fields that are hashed into the chain, in canonical order.
_CONTENT_FIELDS = (
    "slot",
    "from_model",
    "to_model",
    "trigger_type",
    "trigger_detail",
    "conditions_snapshot",
    "audit_metadata",
    "created_at",
)


class DecisionChain:
    """A hash-linked, signable audit log over the ``slot_decisions`` table."""

    def __init__(self, db: Database) -> None:
        self._db = db

    # -- schema ------------------------------------------------------------

    def ensure_schema(self) -> None:
        """Create the decision table and its chain columns if absent."""
        self._db.execute(
            """
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
            """
        )
        columns = {
            row["name"] for row in self._db.fetchall("PRAGMA table_info(slot_decisions)")
        }
        if "audit_metadata" not in columns:
            self._db.execute("ALTER TABLE slot_decisions ADD COLUMN audit_metadata JSON")
        # Chain columns: every decision embeds the hash of the previous one.
        for chain_column in ("entry_hash", "prev_hash"):
            if chain_column not in columns:
                self._db.execute(
                    f"ALTER TABLE slot_decisions ADD COLUMN {chain_column} TEXT"
                )
        self._db.conn.commit()
        self.backfill()

    # -- hashing (pure) ----------------------------------------------------

    @staticmethod
    def entry_hash(content: dict[str, Any], prev_hash: str) -> str:
        """Return the canonical hash linking one decision to the previous one."""
        return canonical_json_hash(
            {field: content.get(field) for field in _CONTENT_FIELDS} | {"prev_hash": prev_hash}
        )

    @staticmethod
    def content_from_row(row: sqlite3.Row) -> dict[str, Any]:
        """Parse a decision row into the content that is hashed for the chain."""
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

    # -- writing -----------------------------------------------------------

    def latest_hash(self) -> str:
        row = self._db.fetchone(
            "SELECT entry_hash FROM slot_decisions WHERE entry_hash IS NOT NULL "
            "ORDER BY id DESC LIMIT 1"
        )
        return row["entry_hash"] if row and row["entry_hash"] else DECISION_CHAIN_GENESIS

    def append(self, content: dict[str, Any]) -> str:
        """Insert one decision, linked to the current head. Does not commit.

        The caller commits so the decision and any accompanying state update
        (e.g. the slot's active model) land atomically. Returns the new entry
        hash.
        """
        prev_hash = self.latest_hash()
        entry_hash = self.entry_hash(content, prev_hash)
        self._db.execute(
            """
            INSERT INTO slot_decisions
            (slot, from_model, to_model, trigger_type, trigger_detail,
             conditions_snapshot, audit_metadata, created_at, prev_hash, entry_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                content["slot"],
                content["from_model"],
                content["to_model"],
                content["trigger_type"],
                content["trigger_detail"],
                json.dumps(content["conditions_snapshot"]),
                json.dumps(content["audit_metadata"]),
                content["created_at"],
                prev_hash,
                entry_hash,
            ),
        )
        return entry_hash

    def backfill(self) -> None:
        """Compute chain links for legacy rows written before the chain existed."""
        rows = self._db.fetchall(
            "SELECT * FROM slot_decisions WHERE entry_hash IS NULL ORDER BY id ASC"
        )
        if not rows:
            return
        prev_hash = self.latest_hash()
        for row in rows:
            entry_hash = self.entry_hash(self.content_from_row(row), prev_hash)
            self._db.execute(
                "UPDATE slot_decisions SET entry_hash = ?, prev_hash = ? WHERE id = ?",
                (entry_hash, prev_hash, row["id"]),
            )
            prev_hash = entry_hash
        self._db.conn.commit()

    # -- reading / verifying ----------------------------------------------

    def count(self) -> int:
        """Number of decisions in the chain (cheap; for metrics)."""
        row = self._db.fetchone("SELECT COUNT(*) AS n FROM slot_decisions")
        return int(row["n"]) if row else 0

    def head(self) -> str:
        return self.latest_hash()

    def verify(self) -> dict[str, Any]:
        """Verify the chain end to end: every entry links and hashes correctly."""
        rows = self._db.fetchall("SELECT * FROM slot_decisions ORDER BY id ASC")
        prev_hash = DECISION_CHAIN_GENESIS
        for index, row in enumerate(rows):
            if row["prev_hash"] != prev_hash:
                return {
                    "valid": False,
                    "length": len(rows),
                    "broken_at": index,
                    "reason": "prev_hash link mismatch",
                }
            expected = self.entry_hash(self.content_from_row(row), prev_hash)
            if expected != row["entry_hash"]:
                return {
                    "valid": False,
                    "length": len(rows),
                    "broken_at": index,
                    "reason": "entry content does not match its hash",
                }
            prev_hash = row["entry_hash"]
        return {"valid": True, "length": len(rows), "head_hash": prev_hash}

    def export(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Return the ordered chain (content + hashes) for offline audit."""
        sql = "SELECT * FROM slot_decisions ORDER BY id ASC"
        params: tuple = ()
        if limit is not None:
            sql = "SELECT * FROM slot_decisions ORDER BY id DESC LIMIT ?"
            params = (limit,)
        rows = self._db.fetchall(sql, params)
        if limit is not None:
            rows = list(reversed(rows))
        return [
            {
                **self.content_from_row(row),
                "prev_hash": row["prev_hash"],
                "entry_hash": row["entry_hash"],
            }
            for row in rows
        ]

    def sign_head(self, signing_key: str, signer: str = "temms") -> dict[str, Any]:
        """Sign the current chain head so the log is offline-verifiable (issue #27)."""
        from temms.core.signing import ed25519_sign, signing_key_fingerprint

        head = self.latest_hash()
        verification = self.verify()
        return {
            "schema_version": "temms-decision-chain-head/v1",
            "head_hash": head,
            "length": verification.get("length", 0),
            "signed_at": datetime.now().isoformat(),
            "signer": signer,
            "key_fingerprint": signing_key_fingerprint(signing_key),
            "signature": ed25519_sign(head.encode("utf-8"), signing_key),
        }
