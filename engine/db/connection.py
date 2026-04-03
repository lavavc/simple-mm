"""SQLite connection lifecycle management."""

from __future__ import annotations

from pathlib import Path

import aiosqlite


class SQLiteConnectionManager:
    """Owns the SQLite connection lifecycle for the repository."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database connection has not been opened")
        return self._conn

    async def connect(self) -> aiosqlite.Connection:
        """Open the SQLite connection if needed and return it."""
        if self._conn is None:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = await aiosqlite.connect(self.db_path)
            self._conn.row_factory = aiosqlite.Row
            await self._conn.execute("PRAGMA foreign_keys = ON")
        return self._conn

    async def close(self) -> None:
        """Close the live connection if present."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
