"""
Condition storage with source priority and confidence tracking.
"""

import sqlite3
from pathlib import Path
from typing import Optional, Dict, Any, List
from datetime import datetime
from dataclasses import dataclass
import json
import logging

from temms.core.database import Database

logger = logging.getLogger(__name__)


@dataclass
class ConditionValue:
    """A condition value with metadata."""
    path: str  # e.g., "weather.visibility_m"
    value: Any
    source: str  # sensor, operator, derived, cached
    priority: int  # Higher = more authoritative
    confidence: float  # 0.0 - 1.0
    updated_at: datetime

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "value": self.value,
            "source": self.source,
            "priority": self.priority,
            "confidence": self.confidence,
            "updated_at": self.updated_at.isoformat(),
        }


class ConditionStore(Database):
    """
    Stores and manages runtime conditions.

    Priority levels:
    - 1000: Operator override (highest)
    - 100: Onboard sensors
    - 90: Derived/computed
    - 50: External data (when connected)
    - 10: Last-known-good/cached
    - 0: Default assumptions
    """

    def _init_tables(self) -> None:
        """Initialize conditions database."""
        self.execute("""
            CREATE TABLE IF NOT EXISTS conditions (
                path TEXT PRIMARY KEY,
                value TEXT,
                source TEXT,
                priority INTEGER,
                confidence REAL DEFAULT 1.0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Condition history for replay/analysis
        self.execute("""
            CREATE TABLE IF NOT EXISTS condition_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT,
                value TEXT,
                source TEXT,
                priority INTEGER,
                confidence REAL,
                recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        self.conn.commit()

    @staticmethod
    def _row_to_condition(row: sqlite3.Row) -> Optional[ConditionValue]:
        """Map a database row to a ConditionValue, handling corrupt data."""
        try:
            value = json.loads(row["value"])
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning(
                f"Corrupt condition value for path '{row['path']}': {e}. Skipping."
            )
            return None

        return ConditionValue(
            path=row["path"],
            value=value,
            source=row["source"],
            priority=row["priority"],
            confidence=row["confidence"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def set(
        self,
        path: str,
        value: Any,
        source: str,
        priority: int,
        confidence: float = 1.0,
    ) -> Optional[ConditionValue]:
        """
        Set a condition value.

        Only updates if the new priority >= existing priority for the same path.

        Args:
            path: Dotted path (e.g., "weather.visibility_m")
            value: Condition value (will be JSON-encoded)
            source: Source identifier
            priority: Priority level
            confidence: Confidence score (0.0-1.0)

        Returns:
            ConditionValue if set/updated, or existing value if priority too low
        """
        updated_at = datetime.now()
        value_json = json.dumps(value)

        # Check existing priority
        row = self.fetchone(
            "SELECT priority FROM conditions WHERE path = ?", (path,)
        )

        # Only update if new priority >= existing priority
        if row is not None and priority < row["priority"]:
            # Priority too low — return the existing stored value
            return self.get(path)

        self.execute(
            """
            INSERT OR REPLACE INTO conditions
            (path, value, source, priority, confidence, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (path, value_json, source, priority, confidence, updated_at),
        )

        # Archive to history
        self.execute(
            """
            INSERT INTO condition_history
            (path, value, source, priority, confidence)
            VALUES (?, ?, ?, ?, ?)
            """,
            (path, value_json, source, priority, confidence),
        )

        self.conn.commit()

        return ConditionValue(
            path=path,
            value=value,
            source=source,
            priority=priority,
            confidence=confidence,
            updated_at=updated_at,
        )

    def get(self, path: str) -> Optional[ConditionValue]:
        """Get current condition value by path."""
        row = self.fetchone(
            "SELECT * FROM conditions WHERE path = ?", (path,)
        )

        if not row:
            return None

        return self._row_to_condition(row)

    def get_all(self, prefix: Optional[str] = None) -> Dict[str, ConditionValue]:
        """
        Get all conditions, optionally filtered by path prefix.

        Args:
            prefix: Optional prefix filter (e.g., "weather" for all weather conditions)

        Returns:
            Dictionary mapping path to ConditionValue
        """
        if prefix:
            rows = self.fetchall(
                "SELECT * FROM conditions WHERE path LIKE ?",
                (f"{prefix}.%",),
            )
        else:
            rows = self.fetchall("SELECT * FROM conditions")

        result = {}
        for row in rows:
            cond = self._row_to_condition(row)
            if cond is not None:
                result[cond.path] = cond
        return result

    def get_snapshot(self) -> Dict[str, Any]:
        """
        Get full snapshot of current conditions (for decision logging).

        Returns:
            Nested dictionary of all conditions
        """
        conditions = self.get_all()
        snapshot = {}

        for path, cond_value in conditions.items():
            parts = path.split(".")
            current = snapshot
            for part in parts[:-1]:
                if part not in current:
                    current[part] = {}
                current = current[part]
            current[parts[-1]] = cond_value.value

        return snapshot

    def exists(self, path: str) -> bool:
        """Check if a condition path exists in the store."""
        row = self.fetchone(
            "SELECT 1 FROM conditions WHERE path = ?", (path,)
        )
        return row is not None

    def clear_operator_overrides(self) -> int:
        """
        Clear all operator overrides (priority >= 1000).

        Returns:
            Number of conditions cleared
        """
        cursor = self.execute_and_commit(
            "DELETE FROM conditions WHERE priority >= 1000"
        )
        return cursor.rowcount

    def get_stale_conditions(self, max_age_seconds: int = 300) -> List[str]:
        """
        Find conditions that haven't been updated recently.

        Uses SQLite datetime functions for reliable timestamp comparison.

        Args:
            max_age_seconds: Maximum age in seconds

        Returns:
            List of stale condition paths
        """
        rows = self.fetchall(
            """
            SELECT path FROM conditions
            WHERE datetime(updated_at) < datetime('now', ? || ' seconds')
            """,
            (str(-max_age_seconds),),
        )

        return [row["path"] for row in rows]
