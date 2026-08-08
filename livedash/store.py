"""Daily end-of-day portfolio-value snapshots in SQLite.

The historical curve on the History view is reconstructed from the price cache
(so it's fully backfilled from day one), but we also record the realized
end-of-day total here. That gives a durable, source-of-truth record of what the
account was actually worth at each close — independent of later price
restatements — and costs nothing to keep.
"""

import sqlite3
from datetime import date


class SnapshotStore:
    def __init__(self, path: str):
        self.path = path
        self._init()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS eod_value ("
                "  day TEXT PRIMARY KEY,"
                "  total_value REAL NOT NULL,"
                "  recorded_at TEXT NOT NULL DEFAULT (datetime('now'))"
                ")"
            )

    def record(self, day: date, total_value: float) -> None:
        """Upsert the end-of-day total for `day` (idempotent within a day)."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO eod_value (day, total_value) VALUES (?, ?) "
                "ON CONFLICT(day) DO UPDATE SET total_value=excluded.total_value, "
                "recorded_at=datetime('now')",
                (day.isoformat(), float(total_value)),
            )

    def history(self) -> list[tuple[date, float]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT day, total_value FROM eod_value ORDER BY day"
            ).fetchall()
        return [(date.fromisoformat(d), v) for d, v in rows]

    def latest_day(self) -> date | None:
        with self._connect() as conn:
            row = conn.execute("SELECT MAX(day) FROM eod_value").fetchone()
        return date.fromisoformat(row[0]) if row and row[0] else None
