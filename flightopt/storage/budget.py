"""Monthly call budget for paid APIs.

SerpApi's free tier is 250 searches per month. Without a counter, one search
over a wide window would spend the month in an afternoon, and the failure mode
is an invoice or a dead source rather than an error we can see.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


def month_key(now: datetime | None = None) -> str:
    """The billing month in UTC, because the provider counts in UTC.

    A local clock would roll the counter over hours early or late, and the
    month that is one call short is the one that fails silently.
    """
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m")


class MonthlyBudget:
    def __init__(self, conn: sqlite3.Connection, source: str, cap: int) -> None:
        self.conn = conn
        self.source = source
        self.cap = cap

    def used(self, *, now: datetime | None = None) -> int:
        row = self.conn.execute(
            "SELECT used FROM api_budget WHERE source=? AND month=?",
            (self.source, month_key(now)),
        ).fetchone()
        return int(row["used"]) if row else 0

    def remaining(self, *, now: datetime | None = None) -> int:
        return max(0, self.cap - self.used(now=now))

    def consume(self, *, now: datetime | None = None) -> bool:
        """Book one call. False when the cap is reached; then do not call.

        Counting and checking happen in one statement. Reading `used` first and
        writing afterwards leaves a window in which two jobs both see the last
        free call and both spend it, which is exactly the invoice this guard
        exists to prevent.
        """
        if self.cap <= 0:
            # The insert would create the first row unconditionally, so a cap
            # of zero has to be turned away before the statement runs.
            return False
        cur = self.conn.execute(
            "INSERT INTO api_budget(source, month, used) VALUES(?,?,1) "
            "ON CONFLICT(source, month) DO UPDATE SET used = used + 1 "
            "WHERE used < ?",
            (self.source, month_key(now), self.cap),
        )
        return cur.rowcount > 0
