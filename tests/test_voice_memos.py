from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from knowledge_hub.recall import _frontmatter
from knowledge_hub.voice_memos import (SourcePermissionError, VoiceMemosIngestor,
                                       main)


class VoiceMemosTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.source = root / "Recordings"
        self.archive = root / "Archive"
        self.vault = root / "Vault"
        self.state = root / "state" / "voice-memos.json"
        self.source.mkdir()
        self.vault.mkdir()
        self.transcriber = root / "transcribe.py"
        self.transcriber.write_text(
            "import sys\nprint('これは テスト 録音 の文字起こしです')\n", encoding="utf-8"
        )
        self.command = "{} {}".format(sys.executable, self.transcriber)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _ingestor(self, command: str | None = None) -> VoiceMemosIngestor:
        return VoiceMemosIngestor(self.source, self.archive, self.vault, self.state,
                                  settle_seconds=0, transcribe_command=self.command if command is None else command)

    def _recording(self, name: str = "private-name.m4a", data: bytes = b"audio",
                   age_seconds: float = 0) -> Path:
        path = self.source / name
        path.write_bytes(data)
        mtime = time.time_ns() - int(age_seconds * 1_000_000_000)
        os.utime(path, ns=(mtime, mtime))
        return path

    def _baseline(self) -> None:
        self._ingestor().baseline_existing()

    def test_first_run_refuses_without_explicit_gate(self) -> None:
        self._recording()
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main([
                "--once", "--source", str(self.source), "--archive", str(self.archive),
                "--vault", str(self.vault), "--state", str(self.state),
            ])
        self.assertEqual(code, 2)
        self.assertIn("--baseline-existing", stderr.getvalue())
        self.assertFalse(self.state.exists())

    def test_baseline_marks_existing_without_copy_or_card(self) -> None:
        self._recording(age_seconds=10)
        report = self._ingestor().baseline_existing()
        self.assertEqual(report.discovered, 1)
        self.assertFalse(self.archive.exists())
        self.assertFalse((self.vault / "Cards").exists())
        self.assertEqual(self._ingestor().scan_once().cards, 0)

    def test_delayed_historical_sync_is_baselined_by_cutover(self) -> None:
        self._baseline()
        self._recording("delayed-sync.m4a", age_seconds=10)
        report = self._ingestor().scan_once()
        self.assertEqual(report.cards, 0)
        self.assertEqual(report.archived, 0)
        self.assertFalse(self.archive.exists())
        self.assertEqual(self._ingestor().scan_once().cards, 0)

    def test_new_file_requires_two_scans_then_archives_and_creates_searchable_card(self) -> None:
        self._baseline()
        self._recording()
        self.assertEqual(self._ingestor().scan_once().cards, 0)
        report = self._ingestor().scan_once()
        self.assertEqual(report.archived, 1)
        self.assertEqual(report.cards, 1)
        audio = list(self.archive.glob("*/*.m4a"))
        cards = list((self.vault / "Cards" / "Audio").glob("*/*.md"))
        self.assertEqual(len(audio), 1)
        self.assertEqual(len(cards), 1)
        self.assertRegex(audio[0].name, r"^\d{4}_\d{4}_voice-memo_[0-9a-f]{10}\.m4a$")
        metadata = _frontmatter(cards[0])
        self.assertEqual(metadata["source_type"], "audio")
        self.assertEqual(metadata["source_app"], "voice_memos")
        self.assertEqual(metadata["source_path"], str(audio[0].relative_to(self.archive)))
        self.assertIn("テスト 録音", cards[0].read_text(encoding="utf-8"))

    def test_completed_recording_is_idempotent(self) -> None:
        self._baseline()
        self._recording()
        self._ingestor().scan_once()
        self._ingestor().scan_once()
        report = self._ingestor().scan_once()
        self.assertEqual((report.archived, report.cards), (0, 0))
        self.assertEqual(len(list(self.archive.glob("*/*.m4a"))), 1)
        self.assertEqual(len(list((self.vault / "Cards" / "Audio").glob("*/*.md"))), 1)

    def test_settle_age_is_required_in_addition_to_two_scans(self) -> None:
        self._baseline()
        recording = self._recording()
        mtime = recording.stat().st_mtime
        ingestor = VoiceMemosIngestor(self.source, self.archive, self.vault, self.state,
                                      settle_seconds=120, transcribe_command=self.command)
        ingestor.scan_once(now=mtime + 20)
        pending = ingestor.scan_once(now=mtime + 20)
        self.assertEqual(pending.cards, 0)
        ready = ingestor.scan_once(now=mtime + 121)
        self.assertEqual(ready.cards, 1)

    def test_edited_recording_is_a_new_revision(self) -> None:
        self._baseline()
        recording = self._recording(data=b"first")
        self._ingestor().scan_once()
        self._ingestor().scan_once()
        recording.write_bytes(b"second revision")
        changed = time.time_ns()
        os.utime(recording, ns=(changed, changed))
        self._ingestor().scan_once()
        report = self._ingestor().scan_once()
        self.assertEqual(report.cards, 1)
        self.assertEqual(len(list(self.archive.glob("*/*.m4a"))), 2)
        self.assertEqual(len(list((self.vault / "Cards" / "Audio").glob("*/*.md"))), 2)

    def test_backfill_is_opt_in_and_processes_after_stability(self) -> None:
        self._recording()
        exit_code = main([
            "--backfill-existing", "--once", "--source", str(self.source), "--archive", str(self.archive),
            "--vault", str(self.vault), "--state", str(self.state), "--settle-seconds", "0",
            "--transcribe-cmd", self.command,
        ])
        self.assertEqual(exit_code, 0)
        self.assertFalse(list(self.archive.glob("*/*.m4a")))
        report = self._ingestor().scan_once()
        self.assertEqual(report.cards, 1)

    def test_transcription_failure_keeps_archive_and_retries_without_duplicate(self) -> None:
        self._baseline()
        self._recording()
        failing = self.temp.name + "/does-not-exist"
        ingestor = self._ingestor(failing)
        ingestor.scan_once()
        report = ingestor.scan_once()
        self.assertEqual(report.archived, 1)
        self.assertEqual(report.cards, 0)
        self.assertTrue(report.errors)
        retry = self._ingestor().scan_once()
        self.assertEqual(retry.cards, 1)
        self.assertEqual(len(list(self.archive.glob("*/*.m4a"))), 1)

    def test_transcription_timeout_is_privacy_safe_and_retryable(self) -> None:
        self._baseline()
        self._recording()
        ingestor = self._ingestor()
        ingestor.scan_once()
        with patch("knowledge_hub.voice_memos.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(["fake"], 1)):
            report = ingestor.scan_once()
        self.assertEqual(report.archived, 1)
        self.assertEqual(report.cards, 0)
        self.assertTrue(any("時間切れ" in error for error in report.errors))
        self.assertNotIn("private-name", " ".join(report.errors))
        self.assertEqual(self._ingestor().scan_once().cards, 1)

    def test_permission_error_and_missing_source_are_clear_config_errors(self) -> None:
        with patch("knowledge_hub.voice_memos._iter_recordings", side_effect=SourcePermissionError("denied")):
            with self.assertRaises(SourcePermissionError):
                self._ingestor().baseline_existing()
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main(["--once", "--source", str(self.source / "missing"), "--archive", str(self.archive),
                         "--vault", str(self.vault), "--state", str(self.state)])
        self.assertEqual(code, 2)
        self.assertIn("ソースが見つからない", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
