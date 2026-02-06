"""
Condition storage with source priority and confidence tracking.
"""

import sqlite3
from pathlib import Path
from typing import Optional, Dict, Any, List
from datetime import datetime
from dataclasses import dataclass
import json


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


class ConditionStore:
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

    def __init__(self, db_path: Path):
        """Initialize condition store."""
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        """Initialize conditions database."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
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
            conn.execute("""
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

            conn.commit()

    def set(
        self,
        path: str,
        value: Any,
        source: str,
        priority: int,
        confidence: float = 1.0,
    ) -> ConditionValue:
        """
        Set a condition value.

        Args:
            path: Dotted path (e.g., "weather.visibility_m")
            value: Condition value (will be JSON-encoded)
            source: Source identifier
            priority: Priority level
            confidence: Confidence score (0.0-1.0)

        Returns:
            ConditionValue
        """
        updated_at = datetime.now()
        value_json = json.dumps(value)

        with sqlite3.connect(self.db_path) as conn:
            # Get existing condition to check priority
            cursor = conn.execute(
                "SELECT priority FROM conditions WHERE path = ?", (path,)
            )
            row = cursor.fetchone()

            # Only update if new priority >= existing priority
            if row is None or priority >= row[0]:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO conditions
                    (path, value, source, priority, confidence, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (path, value_json, source, priority, confidence, updated_at),
                )

                # Archive to history
                conn.execute(
                    """
                    INSERT INTO condition_history
                    (path, value, source, priority, confidence)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (path, value_json, source, priority, confidence),
                )

                conn.commit()

                return ConditionValue(
                    path=path,
                    value=value,
                    source=source,
                    priority=priority,
                    confidence=confidence,
                    updated_at=updated_at,
                )

        # Priority too low - return the existing stored value
        return self.get(path)

    def get(self, path: str) -> Optional[ConditionValue]:
        """Get current condition value by path."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT * FROM conditions WHERE path = ?", (path,)
            )
            row = cursor.fetchone()

        if not row:
            return None

        return ConditionValue(
            path=row["path"],
            value=json.loads(row["value"]),
            source=row["source"],
            priority=row["priority"],
            confidence=row["confidence"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def get_all(self, prefix: Optional[str] = None) -> Dict[str, ConditionValue]:
        """
        Get all conditions, optionally filtered by path prefix.

        Args:
            prefix: Optional prefix filter (e.g., "weather" for all weather conditions)

        Returns:
            Dictionary mapping path to ConditionValue
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            if prefix:
                cursor = conn.execute(
                    "SELECT * FROM conditions WHERE path LIKE ?",
                    (f"{prefix}.%",),
                )
            else:
                cursor = conn.execute("SELECT * FROM conditions")
            rows = cursor.fetchall()

        return {
            row["path"]: ConditionValue(
                path=row["path"],
                value=json.loads(row["value"]),
                source=row["source"],
                priority=row["priority"],
                confidence=row["confidence"],
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
            for row in rows
        }

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

    def clear_operator_overrides(self) -> int:
        """
        Clear all operator overrides (priority >= 1000).

        Returns:
            Number of conditions cleared
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM conditions WHERE priority >= 1000"
            )
            conn.commit()
            return cursor.rowcount

    def get_stale_conditions(self, max_age_seconds: int = 300) -> List[str]:
        """
        Find conditions that haven't been updated recently.

        Args:
            max_age_seconds: Maximum age in seconds

        Returns:
            List of stale condition paths
        """
        # Compare as ISO strings since both updated_at and cutoff use local time
        cutoff_ts = datetime.now().timestamp() - max_age_seconds
        cutoff_iso = datetime.fromtimestamp(cutoff_ts).isoformat()

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """
                SELECT path FROM conditions
                WHERE updated_at < ?
                """,
                (cutoff_iso,),
            )
            rows = cursor.fetchall()

        return [row["path"] for row in rows]
