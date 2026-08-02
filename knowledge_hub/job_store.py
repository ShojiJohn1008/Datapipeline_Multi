"""Local SQLite ledger for idempotent Inbox processing.

The ledger intentionally stores only metadata and paths.  The source bytes,
extracted text, card body, and AI credentials remain outside SQLite.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional, Tuple, Union


SCHEMA_VERSION = 1
MAX_ERROR_LENGTH = 1_000
_SECRET_PATTERN = re.compile(
    r"(?i)\b(bearer|token|api[_ -]?key|secret)\s*(?:=|:|\s)\s*[^\s,;]+"
)


class JobStateTransitionError(RuntimeError):
    """Raised when a job operation is not valid for its current state."""


@dataclass(frozen=True)
class JobRecord:
    """Metadata needed to safely resume one source-file processing job."""

    content_hash: str
    source_path: str
    original_name: str
    size_bytes: int
    media_type: Optional[str]
    status: str
    attempts: int
    last_error: Optional[str]
    archived_path: Optional[str]
    card_path: Optional[str]
    created_at: str
    updated_at: str
    started_at: Optional[str]
    completed_at: Optional[str]


def sha256_file(path: Union[str, Path], chunk_size: int = 1024 * 1024) -> str:
    """Return a streaming SHA-256 digest without loading the file into memory."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while True:
            chunk = source.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sanitize_error(error: object) -> str:
    """Bound an error message and mask common credential-shaped fragments."""
    text = str(error).strip()
    text = _SECRET_PATTERN.sub(lambda match: match.group(1) + " [REDACTED]", text)
    return text[:MAX_ERROR_LENGTH] or "unknown processing error"


class JobStore:
    """A small, multi-process-safe SQLite state machine for local jobs.

    The database parent must already exist.  Callers establish that boundary
    through ``ensure_pipeline_directories()`` before constructing this class.
    """

    def __init__(self, db_path: Union[str, Path]):
        self.db_path = Path(db_path).expanduser()
        if not self.db_path.parent.is_dir():
            raise FileNotFoundError(
                "JobStore database parent does not exist; call "
                "ensure_pipeline_directories() first: " + str(self.db_path.parent)
            )
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path), timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a transaction and always close its SQLite connection."""
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    "JobStore database schema is newer than this program: "
                    f"{version} > {SCHEMA_VERSION}"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    content_hash TEXT PRIMARY KEY,
                    source_path TEXT NOT NULL,
                    original_name TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
                    media_type TEXT,
                    status TEXT NOT NULL CHECK (status IN
                        ('pending', 'processing', 'completed', 'failed')),
                    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                    last_error TEXT,
                    archived_path TEXT,
                    card_path TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS jobs_claim_idx "
                "ON jobs(status, attempts, created_at)"
            )
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @staticmethod
    def _record(row: sqlite3.Row) -> JobRecord:
        return JobRecord(**dict(row))

    def register(
        self,
        content_hash: str,
        source_path: Union[str, Path],
        *,
        original_name: Optional[str] = None,
        size_bytes: Optional[int] = None,
        media_type: Optional[str] = None,
    ) -> Tuple[JobRecord, bool]:
        """Discover a source item, returning ``(job, created)``.

        ``content_hash`` is the identity.  A same-content file with a different
        source path therefore returns the original record instead of creating a
        second job.
        """
        if len(content_hash) != 64 or any(char not in "0123456789abcdef" for char in content_hash.lower()):
            raise ValueError("content_hash must be a 64-character SHA-256 hex digest")
        source = Path(source_path)
        name = original_name if original_name is not None else source.name
        if size_bytes is None:
            size_bytes = source.stat().st_size
        if size_bytes < 0:
            raise ValueError("size_bytes must be non-negative")
        now = _now()
        with self._connection() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO jobs (
                        content_hash, source_path, original_name, size_bytes,
                        media_type, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (content_hash.lower(), str(source), name, size_bytes, media_type, now, now),
                )
                created = True
            except sqlite3.IntegrityError:
                created = False
            row = connection.execute(
                "SELECT * FROM jobs WHERE content_hash = ?", (content_hash.lower(),)
            ).fetchone()
        return self._record(row), created

    # "discover" is deliberately an alias: it is the vocabulary used by an
    # Inbox scanner, while ``register`` is useful to direct callers/tests.
    discover = register

    def register_file(
        self, source_path: Union[str, Path], *, media_type: Optional[str] = None
    ) -> Tuple[JobRecord, bool]:
        """Hash and register a source file using metadata read from the file."""
        source = Path(source_path)
        return self.register(
            sha256_file(source), source, size_bytes=source.stat().st_size,
            media_type=media_type,
        )

    def get(self, content_hash: str) -> Optional[JobRecord]:
        """Return one job, or ``None`` when that hash has not been discovered."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE content_hash = ?", (content_hash.lower(),)
            ).fetchone()
        return self._record(row) if row is not None else None

    def list(self, status: Optional[str] = None) -> Iterable[JobRecord]:
        """List jobs in discovery order, optionally restricted to one state."""
        with self._connection() as connection:
            if status is None:
                rows = connection.execute("SELECT * FROM jobs ORDER BY created_at, rowid").fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM jobs WHERE status = ? ORDER BY created_at, rowid",
                    (status,),
                ).fetchall()
        return [self._record(row) for row in rows]

    def claim_next(self, *, max_attempts: int = 3) -> Optional[JobRecord]:
        """Atomically claim the oldest eligible pending or failed job.

        Failed jobs are eligible until their attempt count reaches
        ``max_attempts``.  ``BEGIN IMMEDIATE`` serializes competing workers
        before they select a candidate.
        """
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        now = _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT content_hash FROM jobs
                WHERE status IN ('pending', 'failed') AND attempts < ?
                ORDER BY
                    CASE status WHEN 'pending' THEN 0 ELSE 1 END,
                    created_at, rowid
                LIMIT 1
                """,
                (max_attempts,),
            ).fetchone()
            if row is None:
                return None
            content_hash = row["content_hash"]
            connection.execute(
                """
                UPDATE jobs
                SET status = 'processing', attempts = attempts + 1,
                    started_at = ?, updated_at = ?, last_error = NULL
                WHERE content_hash = ? AND status IN ('pending', 'failed')
                """,
                (now, now, content_hash),
            )
            claimed = connection.execute(
                "SELECT * FROM jobs WHERE content_hash = ?", (content_hash,)
            ).fetchone()
            return self._record(claimed)

    def mark_completed(
        self,
        content_hash: str,
        archived_path: Union[str, Path],
        card_path: Union[str, Path],
    ) -> JobRecord:
        """Finish a claimed job after its archive and card writes succeeded."""
        return self._transition_from_processing(
            content_hash,
            "completed",
            archived_path=str(archived_path),
            card_path=str(card_path),
            completed_at=_now(),
        )

    def mark_failed(self, content_hash: str, error: object) -> JobRecord:
        """Record a retryable processing failure with bounded, sanitized text.

        Callers must not pass credentials or source contents as an error. Common
        ``Bearer``, token, API-key, and secret-shaped fragments are masked as a
        defence in depth measure, not as a substitute for that contract.
        """
        error_text = _sanitize_error(error)
        return self._transition_from_processing(
            content_hash, "failed", last_error=error_text
        )

    def _transition_from_processing(self, content_hash: str, target: str, **fields: object) -> JobRecord:
        now = _now()
        assignments = ["status = ?", "updated_at = ?"]
        values = [target, now]
        for field, value in fields.items():
            assignments.append(field + " = ?")
            values.append(value)
        values.append(content_hash.lower())
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE jobs SET " + ", ".join(assignments)
                + " WHERE content_hash = ? AND status = 'processing'",
                values,
            )
            if cursor.rowcount != 1:
                self._raise_bad_transition(connection, content_hash, target)
            row = connection.execute(
                "SELECT * FROM jobs WHERE content_hash = ?", (content_hash.lower(),)
            ).fetchone()
        return self._record(row)

    @staticmethod
    def _raise_bad_transition(connection: sqlite3.Connection, content_hash: str, target: str) -> None:
        row = connection.execute(
            "SELECT status FROM jobs WHERE content_hash = ?", (content_hash.lower(),)
        ).fetchone()
        if row is None:
            raise JobStateTransitionError(f"unknown job: {content_hash}")
        raise JobStateTransitionError(
            f"cannot transition job from {row['status']} to {target}; expected processing"
        )

    def requeue_stale_processing(self, *, stale_after: timedelta) -> int:
        """Return abandoned processing jobs to pending and return their count."""
        if stale_after.total_seconds() < 0:
            raise ValueError("stale_after must not be negative")
        stale_before = (datetime.now(timezone.utc) - stale_after).isoformat(timespec="seconds")
        now = _now()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET status = 'pending', updated_at = ?
                WHERE status = 'processing' AND started_at <= ?
                """,
                (now, stale_before),
            )
        return cursor.rowcount
