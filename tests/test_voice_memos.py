from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from knowledge_hub.agent_provider import CardDraft
from knowledge_hub.config import PipelinePaths, ensure_pipeline_directories
from knowledge_hub.job_store import JobStore, sha256_file
from knowledge_hub.recall import _frontmatter
from knowledge_hub.voice_memos import (
    AUDIO_MEDIA_TYPE,
    SourcePermissionError,
    VoiceMemosIngestor,
    audio_output_paths,
    process_one_audio,
    main,
)


class FakeSummaryProvider:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def create_card(self, text: str, metadata: dict[str, object]) -> CardDraft:
        self.calls += 1
        if self.fail:
            raise RuntimeError("summary unavailable")
        return CardDraft(
            title="構造化した音声メモ",
            summary="意味のある要約です。",
            category="meeting",
            tags=["会議", "音声"],
            key_points=["最初の要点", "次の要点"],
        )


class VoiceMemosTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.source = root / "Recordings"
        self.archive = root / "Archive"
        self.vault = root / "Vault"
        self.cards = self.vault / "Cards"
        self.state = root / "observations" / "voice-memos.json"
        self.source.mkdir()
        self.archive.mkdir()
        self.vault.mkdir()
        self.paths = PipelinePaths(self.vault, self.archive, self.archive / "Inbox",
                                   self.cards, root / "state-db" / "jobs.sqlite3")
        ensure_pipeline_directories(self.paths)
        self.transcriber = root / "transcribe.py"
        self.transcriber.write_text(
            "import sys\nprint('これは テスト 録音 の文字起こしです')\n", encoding="utf-8"
        )
        self.command = "{} {}".format(sys.executable, self.transcriber)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _ingestor(self, command: str | None = None, settle_seconds: float = 0,
                  **kwargs: object) -> VoiceMemosIngestor:
        return VoiceMemosIngestor(
            self.source, self.paths, self.state, settle_seconds=settle_seconds,
            transcribe_command=self.command if command is None else command,
            **kwargs,
        )

    def _recording(self, name: str = "private-name.m4a", data: bytes = b"audio",
                   age_seconds: float = 0) -> Path:
        path = self.source / name
        path.write_bytes(data)
        mtime = time.time_ns() - int(age_seconds * 1_000_000_000)
        os.utime(path, ns=(mtime, mtime))
        return path

    def _baseline(self) -> None:
        self._ingestor().baseline_existing()

    def _audio_jobs(self) -> list:
        return [job for job in JobStore(self.paths.state_db).list() if job.media_type == AUDIO_MEDIA_TYPE]

    def test_first_run_refuses_without_explicit_gate(self) -> None:
        self._recording()
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main([
                "--once", "--source", str(self.source), "--archive", str(self.archive),
                "--vault", str(self.vault), "--state", str(self.state),
                "--state-db", str(self.paths.state_db),
            ])
        self.assertEqual(code, 2)
        self.assertIn("--baseline-existing", stderr.getvalue())
        self.assertFalse(self.state.exists())

    def test_baseline_marks_existing_without_archive_card_or_job(self) -> None:
        self._recording(age_seconds=10)
        report = self._ingestor().baseline_existing()
        self.assertEqual(report.discovered, 1)
        self.assertFalse(list(self.archive.glob("*/*/*.m4a")))
        self.assertFalse(list(self.cards.glob("*/*/*.md")))
        self.assertEqual(self._audio_jobs(), [])

    def test_delayed_historical_sync_is_baselined_by_cutover(self) -> None:
        self._baseline()
        self._recording("delayed-sync.m4a", age_seconds=10)
        report = self._ingestor().scan_once()
        self.assertEqual((report.registered, report.cards), (0, 0))
        self.assertEqual(self._audio_jobs(), [])

    def test_new_file_two_scans_completes_shared_audio_job_and_card(self) -> None:
        self._baseline()
        recording = self._recording()
        self.assertEqual(self._ingestor().scan_once().cards, 0)
        report = self._ingestor().scan_once()
        self.assertEqual((report.registered, report.cards), (1, 1))
        jobs = self._audio_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].status, "completed")
        self.assertEqual(jobs[0].content_hash, sha256_file(recording))
        audio = list(self.archive.glob("*/*/*.m4a"))
        cards = list(self.cards.glob("*/*/*.md"))
        self.assertEqual((len(audio), len(cards)), (1, 1))
        self.assertRegex(audio[0].name, r"^voice-memo--[0-9a-f]{8}\.m4a$")
        metadata = _frontmatter(cards[0])
        self.assertEqual(metadata["source_type"], "audio")
        self.assertEqual(metadata["source_app"], "voice_memos")
        self.assertEqual(metadata["source_path"], str(audio[0].relative_to(self.archive)))
        self.assertIn("テスト 録音", cards[0].read_text(encoding="utf-8"))
        observation = json.loads(self.state.read_text(encoding="utf-8"))
        observed_item = next(iter(observation["items"].values()))
        self.assertFalse({"sha256", "archive_rel", "archive_path", "card_path", "transcript"} & observed_item.keys())

    def test_sidecar_and_semantic_summary_are_durable_before_completion(self) -> None:
        self._baseline()
        provider = FakeSummaryProvider()
        ingestor = self._ingestor(summary_provider=provider)
        recording = self._recording()
        ingestor.scan_once()
        report = ingestor.scan_once()
        job = self._audio_jobs()[0]
        output = audio_output_paths(job, self.paths)
        payload = json.loads(output.transcript.read_text(encoding="utf-8"))
        self.assertEqual(report.cards, 1)
        self.assertEqual(payload["schema"], "knowledge_hub.voice_memos.transcript")
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["content_hash"], sha256_file(recording))
        self.assertEqual(payload["source_sha256"], job.content_hash)
        self.assertEqual(payload["transcriber"], Path(sys.executable).name)
        self.assertIsInstance(payload["model"], str)
        self.assertIsInstance(payload["language"], str)
        self.assertEqual(payload["transcript"], "これは テスト 録音 の文字起こしです")
        self.assertEqual(provider.calls, 1)
        card_text = output.card.read_text(encoding="utf-8")
        metadata = _frontmatter(output.card)
        self.assertEqual(metadata["title"], "構造化した音声メモ")
        self.assertEqual(metadata["summary"], "意味のある要約です。")
        self.assertEqual(metadata["category"], "meeting")
        self.assertIn('tags: ["会議","音声"]', card_text)
        self.assertIn("## Key points", card_text)
        self.assertIn("これは テスト 録音 の文字起こしです", card_text)

    def test_summary_retry_reuses_sidecar_without_rerunning_transcriber(self) -> None:
        self._baseline()
        provider = FakeSummaryProvider(fail=True)
        ingestor = self._ingestor(summary_provider=provider)
        self._recording()
        ingestor.scan_once()
        report = ingestor.scan_once()
        job = self._audio_jobs()[0]
        output = audio_output_paths(job, self.paths)
        self.assertEqual((report.errors, JobStore(self.paths.state_db).get(job.content_hash).status),
                         (["Voice Memos 処理保留: audio_summary_failed"], "failed"))
        self.assertTrue(output.transcript.is_file())
        provider.fail = False
        with patch("knowledge_hub.voice_memos._run_transcriber",
                   side_effect=AssertionError("sidecar should be reused")):
            retry = ingestor.scan_once()
        self.assertEqual(retry.cards, 1)
        self.assertEqual(provider.calls, 2)
        self.assertEqual(self._audio_jobs()[0].status, "completed")

    def test_invalid_or_mismatched_sidecar_fails_safely(self) -> None:
        self._baseline()
        provider = FakeSummaryProvider(fail=True)
        ingestor = self._ingestor(summary_provider=provider)
        self._recording()
        ingestor.scan_once()
        ingestor.scan_once()
        job = self._audio_jobs()[0]
        output = audio_output_paths(job, self.paths)
        payload = json.loads(output.transcript.read_text(encoding="utf-8"))
        payload["content_hash"] = "0" * 64
        output.transcript.write_text(json.dumps(payload), encoding="utf-8")
        report = ingestor.scan_once()
        failed = JobStore(self.paths.state_db).get(job.content_hash)
        self.assertEqual((report.errors, failed.last_error),
                         (["Voice Memos 処理保留: output_collision"], "output_collision"))
        self.assertNotIn("private-name", " ".join(report.errors))
        output.transcript.write_text("{not-json", encoding="utf-8")
        corrupt = ingestor.scan_once()
        self.assertEqual(corrupt.errors, ["Voice Memos 処理保留: transcript_sidecar_invalid"])
        self.assertEqual(JobStore(self.paths.state_db).get(job.content_hash).last_error,
                         "transcript_sidecar_invalid")

    def test_existing_card_without_sidecar_never_short_circuits_completion(self) -> None:
        self._baseline()
        recording = self._recording()
        store = JobStore(self.paths.state_db)
        job, _ = store.register_file(recording, media_type=AUDIO_MEDIA_TYPE)
        output = audio_output_paths(job, self.paths)
        output.archive.parent.mkdir(parents=True)
        output.archive.write_bytes(recording.read_bytes())
        output.card.parent.mkdir(parents=True)
        output.card.write_text('---\ncontent_hash: "{}"\n---\n'.format(job.content_hash), encoding="utf-8")
        with patch("knowledge_hub.voice_memos._run_transcriber",
                   side_effect=AssertionError("invalid completion must not transcribe")):
            result = process_one_audio(self.paths, store, self.source, transcribe_command=self.command)
        self.assertEqual(result.error_code, "transcript_sidecar_invalid")
        self.assertEqual(JobStore(self.paths.state_db).get(job.content_hash).status, "failed")

    def test_same_hash_is_deduplicated(self) -> None:
        self._baseline()
        self._recording("one.m4a", b"same")
        self._recording("two.m4a", b"same")
        self._ingestor().scan_once()
        self._ingestor().scan_once()
        self.assertEqual(len(self._audio_jobs()), 1)
        self.assertEqual(len(list(self.archive.glob("*/*/*.m4a"))), 1)
        self.assertEqual(len(list(self.cards.glob("*/*/*.md"))), 1)

    def test_pdf_pending_job_is_untouched_by_audio_adapter(self) -> None:
        pdf = self.paths.inbox / "unrelated.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        store = JobStore(self.paths.state_db)
        pdf_job, _ = store.register_file(pdf, media_type="application/pdf")
        self._baseline()
        self._recording()
        self._ingestor().scan_once()
        self._ingestor().scan_once()
        self.assertEqual(JobStore(self.paths.state_db).get(pdf_job.content_hash).status, "pending")
        self.assertEqual(self._audio_jobs()[0].status, "completed")

    def test_stale_audio_claim_recovers_without_touching_pdf_pending_job(self) -> None:
        self._baseline()
        recording = self._recording()
        store = JobStore(self.paths.state_db)
        audio_job, _ = store.register_file(recording, media_type=AUDIO_MEDIA_TYPE)
        self.assertEqual(store.claim_next(media_type=AUDIO_MEDIA_TYPE).status, "processing")
        pdf = self.paths.inbox / "pending.pdf"
        pdf.write_bytes(b"%PDF-1.4 pending")
        pdf_job, _ = store.register_file(pdf, media_type="application/pdf")
        report = self._ingestor(stale_after_seconds=0).scan_once()
        recovered = JobStore(self.paths.state_db).get(audio_job.content_hash)
        self.assertEqual((report.cards, recovered.status), (1, "completed"))
        self.assertEqual(recovered.attempts, 2)
        self.assertEqual(JobStore(self.paths.state_db).get(pdf_job.content_hash).status, "pending")

    def test_edited_recording_registers_new_revision(self) -> None:
        self._baseline()
        recording = self._recording(data=b"first")
        self._ingestor().scan_once()
        self._ingestor().scan_once()
        recording.write_bytes(b"second revision")
        changed = time.time_ns()
        os.utime(recording, ns=(changed, changed))
        self._ingestor().scan_once()
        self._ingestor().scan_once()
        self.assertEqual(len(self._audio_jobs()), 2)
        self.assertTrue(all(job.status == "completed" for job in self._audio_jobs()))

    def test_settle_age_is_required_in_addition_to_two_scans(self) -> None:
        self._baseline()
        recording = self._recording()
        mtime = recording.stat().st_mtime
        ingestor = self._ingestor(settle_seconds=120)
        ingestor.scan_once(now=mtime + 20)
        pending = ingestor.scan_once(now=mtime + 20)
        self.assertEqual(pending.cards, 0)
        ready = ingestor.scan_once(now=mtime + 121)
        self.assertEqual(ready.cards, 1)

    def test_transcription_failure_records_sanitized_shared_job_and_retries_from_archive(self) -> None:
        self._baseline()
        recording = self._recording()
        failing = self.temp.name + "/does-not-exist"
        ingestor = self._ingestor(failing)
        ingestor.scan_once()
        report = ingestor.scan_once()
        job = self._audio_jobs()[0]
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.last_error, "audio_transcriber_unavailable")
        self.assertNotIn("private-name", job.last_error)
        self.assertEqual(report.cards, 0)
        self.assertEqual(len(list(self.archive.glob("*/*/*.m4a"))), 1)
        source_mtime = recording.stat().st_mtime
        recording.unlink()  # retry must rely on the archived original, not source access.
        retry = self._ingestor().scan_once()
        self.assertEqual(retry.cards, 1)
        self.assertEqual(self._audio_jobs()[0].status, "completed")
        card = next(self.cards.glob("*/*/*.md"))
        self.assertAlmostEqual(datetime.fromisoformat(_frontmatter(card)["captured_at"]).timestamp(),
                               source_mtime, delta=1)

    def test_transcription_timeout_is_privacy_safe_and_retryable(self) -> None:
        self._baseline()
        self._recording()
        ingestor = self._ingestor()
        ingestor.scan_once()
        with patch("knowledge_hub.voice_memos.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(["fake"], 1)):
            report = ingestor.scan_once()
        job = self._audio_jobs()[0]
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.last_error, "audio_transcription_timeout")
        self.assertTrue(any("audio_transcription_timeout" in error for error in report.errors))
        self.assertNotIn("private-name", " ".join(report.errors))
        self.assertEqual(self._ingestor().scan_once().cards, 1)

    def test_once_returns_one_when_processing_reports_error(self) -> None:
        self._baseline()
        self._recording()
        arguments = [
            "--once", "--source", str(self.source), "--archive", str(self.archive),
            "--vault", str(self.vault), "--state", str(self.state), "--state-db", str(self.paths.state_db),
            "--settle-seconds", "0", "--transcribe-cmd", self.temp.name + "/missing-command",
        ]
        self.assertEqual(main(arguments), 0)  # first observation only
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(main(arguments), 1)
        self.assertIn("audio_transcriber_unavailable", stderr.getvalue())

    def test_backfill_is_opt_in_and_rejects_existing_observation_state(self) -> None:
        recording = self._recording(age_seconds=3600)
        arguments = [
            "--backfill-existing", "--once", "--source", str(self.source), "--archive", str(self.archive),
            "--vault", str(self.vault), "--state", str(self.state), "--state-db", str(self.paths.state_db),
            "--settle-seconds", "0", "--transcribe-cmd", self.command,
        ]
        self.assertEqual(main(arguments), 0)
        self.assertEqual(self._ingestor().scan_once().cards, 1)
        card = next(self.cards.glob("*/*/*.md"))
        metadata = _frontmatter(card)
        self.assertAlmostEqual(datetime.fromisoformat(metadata["captured_at"]).timestamp(),
                               recording.stat().st_mtime, delta=1)
        self.assertEqual(metadata["title"], "Voice Memo " + metadata["captured_at"].replace("T", " ")[:16])
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(main(arguments), 2)
        self.assertIn("既存の観測状態", stderr.getvalue())

    def test_kh_cards_path_is_honored_by_cli(self) -> None:
        custom_cards = Path(self.temp.name) / "SeparateCards"
        self._recording(age_seconds=10)
        arguments = [
            "--backfill-existing", "--once", "--source", str(self.source), "--archive", str(self.archive),
            "--vault", str(self.vault), "--state", str(self.state), "--state-db", str(self.paths.state_db),
            "--settle-seconds", "0", "--transcribe-cmd", self.command,
        ]
        with patch.dict(os.environ, {"KH_CARDS_PATH": str(custom_cards)}):
            self.assertEqual(main(arguments), 0)
            self.assertEqual(main([value for value in arguments if value != "--backfill-existing"]), 0)
        self.assertEqual(len(list(custom_cards.glob("*/*/*.md"))), 1)

    def test_permission_error_and_missing_source_are_clear_config_errors(self) -> None:
        with patch("knowledge_hub.voice_memos._iter_recordings", side_effect=SourcePermissionError("denied")):
            with self.assertRaises(SourcePermissionError):
                self._ingestor().baseline_existing()
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main(["--once", "--source", str(self.source / "missing"), "--archive", str(self.archive),
                         "--vault", str(self.vault), "--state", str(self.state),
                         "--state-db", str(self.paths.state_db)])
        self.assertEqual(code, 2)
        self.assertIn("ソースが見つからない", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
