"""Durable record of which diagnostic packages have been uploaded.

This store, not the in-memory queue, is the source of truth. The queue only
makes the common case fast; anything it loses is recovered by the Uploader's
directory scan joined against this table.

Thread safety: the Uploader and SpaceWatcher both read this store, so each
thread gets its own connection rather than sharing one.
"""

import logging
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_PENDING = "PENDING"
STATE_IN_PROGRESS = "IN_PROGRESS"
STATE_UPLOADED = "UPLOADED"
STATE_FAILED = "FAILED"
STATE_SKIPPED = "SKIPPED"

TERMINAL_STATES = (STATE_UPLOADED, STATE_FAILED, STATE_SKIPPED)

REASON_BACKFILL_WINDOW = "backfill_window"
REASON_LOCAL_QUOTA_EVICTED = "local_quota_evicted"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
    package_id      TEXT PRIMARY KEY,
    local_path      TEXT NOT NULL,
    size_bytes      INTEGER NOT NULL,
    content_sha256  TEXT,
    blob_name       TEXT,
    state           TEXT NOT NULL,
    reason          TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at INTEGER,
    created_at      INTEGER NOT NULL,
    uploaded_at     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_state_next ON uploads(state, next_attempt_at);
"""


class UploadStateStore:
    """SQLite-backed upload state."""

    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def reset_in_progress(self) -> int:
        """Recover rows left IN_PROGRESS by a crash.

        `attempts` is deliberately not incremented: the interrupted transfer
        never got a verdict from the service, and a retry is safe because the
        upload is idempotent on the blob name.
        """
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE uploads SET state=?, next_attempt_at=? WHERE state=?",
                (STATE_PENDING, int(time.time()), STATE_IN_PROGRESS),
            )
        if cur.rowcount:
            logger.info("Reset %d interrupted upload(s) to PENDING", cur.rowcount)
        return cur.rowcount

    def register(self, package_id: str, local_path: str, size_bytes: int) -> bool:
        """Record a newly discovered package. Returns True if it was new."""
        now = int(time.time())
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO uploads "
                "(package_id, local_path, size_bytes, state, attempts, next_attempt_at, created_at) "
                "VALUES (?, ?, ?, ?, 0, ?, ?)",
                (package_id, str(local_path), size_bytes, STATE_PENDING, now, now),
            )
        return cur.rowcount > 0

    def claim(self, package_id: str) -> bool:
        """Move PENDING -> IN_PROGRESS. False if another pass already took it."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE uploads SET state=? WHERE package_id=? AND state=?",
                (STATE_IN_PROGRESS, package_id, STATE_PENDING),
            )
        return cur.rowcount > 0

    def mark_uploaded(self, package_id: str, blob_name: str, sha256: str | None = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE uploads SET state=?, blob_name=?, content_sha256=?, uploaded_at=?, "
                "reason=NULL WHERE package_id=?",
                (STATE_UPLOADED, blob_name, sha256, int(time.time()), package_id),
            )

    def mark_retry(self, package_id: str, reason: str, next_attempt_at: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE uploads SET state=?, reason=?, attempts=attempts+1, next_attempt_at=? "
                "WHERE package_id=?",
                (STATE_PENDING, reason, int(next_attempt_at), package_id),
            )

    def mark_blocked(self, package_id: str, reason: str, next_attempt_at: int) -> None:
        """Return to PENDING without consuming an attempt.

        Used when the circuit breaker trips: a misconfigured destination is not
        the package's fault, so it must not exhaust that package's retries.
        """
        with self._conn() as conn:
            conn.execute(
                "UPDATE uploads SET state=?, reason=?, next_attempt_at=? WHERE package_id=?",
                (STATE_PENDING, reason, int(next_attempt_at), package_id),
            )

    def mark_failed(self, package_id: str, reason: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE uploads SET state=?, reason=? WHERE package_id=?",
                (STATE_FAILED, reason, package_id),
            )

    def mark_skipped(self, package_id: str, reason: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE uploads SET state=?, reason=? WHERE package_id=?",
                (STATE_SKIPPED, reason, package_id),
            )

    def due_packages(self, limit: int = 100) -> list[sqlite3.Row]:
        """PENDING packages whose backoff has expired, oldest first."""
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM uploads WHERE state=? AND (next_attempt_at IS NULL OR next_attempt_at<=?) "
                "ORDER BY created_at ASC LIMIT ?",
                (STATE_PENDING, int(time.time()), limit),
            ).fetchall()

    def get(self, package_id: str) -> sqlite3.Row | None:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM uploads WHERE package_id=?", (package_id,)
            ).fetchone()

    def states(self) -> dict[str, str]:
        """package_id -> state, for SpaceWatcher's eviction ordering."""
        with self._conn() as conn:
            rows = conn.execute("SELECT package_id, state FROM uploads").fetchall()
        return {row["package_id"]: row["state"] for row in rows}

    def pending_bytes(self) -> int:
        """Bytes held locally only because they have not been uploaded yet."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(size_bytes), 0) AS total FROM uploads WHERE state IN (?, ?)",
                (STATE_PENDING, STATE_IN_PROGRESS),
            ).fetchone()
        return int(row["total"])

    def counts_by_state(self) -> dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT state, COUNT(*) AS n FROM uploads GROUP BY state"
            ).fetchall()
        return {row["state"]: row["n"] for row in rows}

    def delete(self, package_id: str) -> None:
        """Drop the row for a package whose local file is gone."""
        with self._conn() as conn:
            conn.execute("DELETE FROM uploads WHERE package_id=?", (package_id,))

    def prune(self, older_than_epoch: int) -> int:
        """Remove terminal rows past their retention, bounding the store."""
        with self._conn() as conn:
            cur = conn.execute(
                f"DELETE FROM uploads WHERE state IN ({','.join('?' * len(TERMINAL_STATES))}) "
                "AND created_at < ?",
                (*TERMINAL_STATES, int(older_than_epoch)),
            )
        return cur.rowcount
