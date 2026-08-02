"""Tests for the local, metadata-only SQLite job ledger."""
from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import timedelta
from pathlib import Path

from knowledge_hub.job_store import (
    MAX_ERROR_LENGTH,
    JobStateTransitionError,
    JobStore,
    sha256_file,
)


class TestJobStore(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "state" / "jobs.sqlite3"
        self.db.parent.mkdir()
        self.store = JobStore(self.db)

    @staticmethod
    def digest(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    def register(self, value: str, name: str = "source.pdf"):
        return self.store.register(
            self.digest(value), "/inbox/" + name, original_name=name,
            size_bytes=len(value), media_type="application/pdf",
        )

    def test_schema_reinitialization_is_idempotent_and_uses_wal(self):
        self.register("first")
        reopened = JobStore(self.db)
        self.assertEqual(len(list(reopened.list())), 1)
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")

    def test_sha256_file_is_streaming_equivalent(self):
        source = self.root / "large.bin"
        source.write_bytes((b"abcdef" * 200_000) + b"end")
        self.assertEqual(sha256_file(source, chunk_size=13), hashlib.sha256(source.read_bytes()).hexdigest())

    def test_duplicate_content_is_not_registered_twice_even_with_another_name(self):
        first, created = self.register("same", "one.pdf")
        duplicate, created_again = self.register("same", "two.pdf")
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(duplicate.content_hash, first.content_hash)
        self.assertEqual(duplicate.original_name, "one.pdf")
        self.assertEqual(len(list(self.store.list())), 1)

    def test_claim_is_ordered_and_increments_attempts(self):
        first, _ = self.register("first")
        second, _ = self.register("second")
        claim = self.store.claim_next()
        self.assertEqual(claim.content_hash, first.content_hash)
        self.assertEqual(claim.status, "processing")
        self.assertEqual(claim.attempts, 1)
        self.store.mark_failed(first.content_hash, "temporary")
        next_claim = self.store.claim_next()
        self.assertEqual(next_claim.content_hash, second.content_hash)
        self.assertEqual(next_claim.attempts, 1)
        self.store.mark_completed(second.content_hash, "/archive/two.pdf", "/vault/Cards/two.md")
        retry_claim = self.store.claim_next()
        self.assertEqual(retry_claim.content_hash, first.content_hash)
        self.assertEqual(retry_claim.attempts, 2)
        self.store.mark_completed(first.content_hash, "/archive/one.pdf", "/vault/Cards/one.md")
        self.assertIsNone(self.store.claim_next())

    def test_completed_job_is_not_claimed_again(self):
        job, _ = self.register("done")
        self.store.claim_next()
        completed = self.store.mark_completed(job.content_hash, "/archive/done.pdf", "/cards/done.md")
        self.assertEqual(completed.status, "completed")
        self.assertIsNone(self.store.claim_next())

    def test_two_store_instances_cannot_claim_the_same_job(self):
        job, _ = self.register("competing workers")
        other_worker = JobStore(self.db)
        self.assertEqual(self.store.claim_next().content_hash, job.content_hash)
        self.assertIsNone(other_worker.claim_next())

    def test_failed_job_retries_only_until_max_attempts(self):
        job, _ = self.register("retry")
        self.assertEqual(self.store.claim_next(max_attempts=2).attempts, 1)
        failed = self.store.mark_failed(job.content_hash, "x" * (MAX_ERROR_LENGTH + 50))
        self.assertEqual(len(failed.last_error), MAX_ERROR_LENGTH)
        self.assertEqual(self.store.claim_next(max_attempts=2).attempts, 2)
        self.store.mark_failed(job.content_hash, "still broken")
        self.assertIsNone(self.store.claim_next(max_attempts=2))
        self.assertEqual(self.store.get(job.content_hash).status, "failed")

    def test_failed_error_masks_common_credential_fragments(self):
        job, _ = self.register("redact")
        self.store.claim_next()
        failed = self.store.mark_failed(
            job.content_hash,
            "request failed: Bearer abc.def token=abc api_key: xyz secret super-secret",
        )
        self.assertNotIn("abc.def", failed.last_error)
        self.assertNotIn("xyz", failed.last_error)
        self.assertNotIn("super-secret", failed.last_error)
        self.assertEqual(failed.last_error.count("[REDACTED]"), 4)

    def test_stale_processing_is_requeued_then_claimable(self):
        job, _ = self.register("stale")
        self.store.claim_next()
        self.assertEqual(self.store.requeue_stale_processing(stale_after=timedelta(seconds=0)), 1)
        self.assertEqual(self.store.get(job.content_hash).status, "pending")
        self.assertEqual(self.store.claim_next().attempts, 2)

    def test_persistence_across_reopen(self):
        job, _ = self.register("persist")
        reopened = JobStore(self.db)
        self.assertEqual(reopened.get(job.content_hash).original_name, "source.pdf")
        self.assertEqual(reopened.get(job.content_hash).source_path, "/inbox/source.pdf")

    def test_invalid_transitions_raise_dedicated_exception(self):
        job, _ = self.register("invalid")
        with self.assertRaises(JobStateTransitionError):
            self.store.mark_completed(job.content_hash, "/a", "/c")
        self.store.claim_next()
        self.store.mark_completed(job.content_hash, "/a", "/c")
        with self.assertRaises(JobStateTransitionError):
            self.store.mark_failed(job.content_hash, "too late")

    def test_db_parent_is_never_created_implicitly(self):
        missing_db = self.root / "typo" / "jobs.sqlite3"
        with self.assertRaises(FileNotFoundError):
            JobStore(missing_db)
        self.assertFalse(missing_db.parent.exists())


if __name__ == "__main__":
    unittest.main()
