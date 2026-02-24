"""
Shared database base class for SQLite-backed components.

Provides:
- WAL journal mode for concurrent read/write access
- Shared connection management (reusable across methods)
- Generic row-to-object mapping pattern
"""

import sqlite3
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Type, TypeVar
import logging

logger = logging.getLogger(__name__)

T = TypeVar("T")


class Database:
    """
    Base class for SQLite-backed components.

    Uses WAL journal mode for concurrent access (allows reads during writes).
    Maintains a shared connection with check_same_thread=False for use
    across daemon threads (thread safety managed by callers via locks).
    """

    def __init__(self, db_path: Path):
        """
        Initialize database connection.

        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._setup_connection()
        self._init_tables()

    def _setup_connection(self) -> None:
        """Create shared connection with WAL mode."""
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        # WAL mode: allows concurrent readers with a single writer
        self._conn.execute("PRAGMA journal_mode=WAL")
        # Reasonable busy timeout for concurrent access (5 seconds)
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.commit()

    def _init_tables(self) -> None:
        """Override in subclasses to create tables."""
        pass

    @property
    def conn(self) -> sqlite3.Connection:
        """Get the shared database connection."""
        if self._conn is None:
            self._setup_connection()
        return self._conn

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute SQL and return cursor."""
        return self.conn.execute(sql, params)

    def execute_and_commit(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute SQL, commit, and return cursor."""
        cursor = self.conn.execute(sql, params)
        self.conn.commit()
        return cursor

    def executemany_and_commit(self, sql: str, params_list: list) -> None:
        """Execute SQL for multiple parameter sets and commit."""
        self.conn.executemany(sql, params_list)
        self.conn.commit()

    def fetchone(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        """Execute SQL and fetch one row."""
        cursor = self.conn.execute(sql, params)
        return cursor.fetchone()

    def fetchall(self, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
        """Execute SQL and fetch all rows."""
        cursor = self.conn.execute(sql, params)
        return cursor.fetchall()

    def map_row(self, row: sqlite3.Row, mapper: Callable[[sqlite3.Row], T]) -> T:
        """Map a single row using a mapper function."""
        return mapper(row)

    def map_rows(self, rows: List[sqlite3.Row], mapper: Callable[[sqlite3.Row], T]) -> List[T]:
        """Map multiple rows using a mapper function."""
        return [mapper(row) for row in rows]

    def fetch_one_mapped(
        self,
        sql: str,
        params: tuple,
        mapper: Callable[[sqlite3.Row], T],
    ) -> Optional[T]:
        """Execute SQL, fetch one row, and map it."""
        row = self.fetchone(sql, params)
        if row is None:
            return None
        return mapper(row)

    def fetch_all_mapped(
        self,
        sql: str,
        params: tuple,
        mapper: Callable[[sqlite3.Row], T],
    ) -> List[T]:
        """Execute SQL, fetch all rows, and map them."""
        rows = self.fetchall(sql, params)
        return self.map_rows(rows, mapper)

    def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
