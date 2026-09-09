"""Single-host durable payment replay protection and paid response recovery."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class SQLiteStore:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    async def get(self, key):
        with self.connect() as db:
            row = db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    async def put(self, key, value):
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, json.dumps(value))
            )

    async def put_if_absent(self, key, value):
        with self.connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO kv VALUES (?, ?)", (key, json.dumps(value))
            )
            return cursor.rowcount == 1

    async def delete(self, key):
        with self.connect() as db:
            db.execute("DELETE FROM kv WHERE key = ?", (key,))
