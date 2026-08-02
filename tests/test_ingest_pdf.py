from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from knowledge_hub.agent_provider import CardDraft
from knowledge_hub.config import PipelinePaths, ensure_pipeline_directories
from knowledge_hub.ingest_pdf import (
    deterministic_output_paths,
    discover_pdf_jobs,
    process_one_pdf,
    render_card,
    run_once,
    safe_stem,
)
from knowledge_hub.job_store import JobStore, sha256_file
from knowledge_hub.pdf_extract import PdfExtraction, PdfNeedsOcrError


class FakeProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def create_card(self, text: str, metadata: dict[str, object]) -> CardDraft:
        self.calls.append((text, metadata))
        return CardDraft(
            "安全なタイトル",
            "短い要約です。",
            "medicine/general",
            ["PDF", "知識"],
            ["第一の要点", "第二の要点"],
        )


def fake_extract(_: Path) -> PdfExtraction:
    return PdfExtraction("秘密の抽出全文 " * 20, 180, True)


class TestPdfIngest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        vault = root / "vault"
        archive = root / "archive"
        vault.mkdir()
        archive.mkdir()
        self.paths = PipelinePaths(
            vault=vault,
            archive=archive,
            inbox=archive / "Inbox",
            cards=vault / "Cards",
            state_db=root / "state" / "jobs.sqlite3",
        )
        ensure_pipeline_directories(self.paths)
        self.store = JobStore(self.paths.state_db)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def add_pdf(self, name: str = "医学 資料.pdf", content: bytes = b"%PDF source") -> Path:
        path = self.paths.inbox / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        old = time.time() - 60
        os.utime(path, (old, old))
        return path

    def register_pdf(self, name: str = "医学 資料.pdf", content: bytes = b"%PDF source"):
        source = self.add_pdf(name, content)
        jobs = discover_pdf_jobs(self.paths, self.store, settle_seconds=5)
        self.assertEqual(len(jobs), 1)
        return source, jobs[0]

    def test_discovery_accepts_only_stable_visible_pdf(self) -> None:
        stable = self.add_pdf("stable.PDF")
        self.add_pdf("note.txt")
        self.add_pdf("report.tmp.pdf")
        self.add_pdf(".hidden.pdf")
        fresh = self.add_pdf("fresh.pdf")
        os.utime(fresh, None)
        nested = self.add_pdf("nested/inside.pdf", b"nested unique")
        jobs = discover_pdf_jobs(
            self.paths, self.store, settle_seconds=5, recursive=False
        )
        self.assertEqual([Path(job.source_path) for job in jobs], [stable])
        recursive_jobs = discover_pdf_jobs(
            self.paths, self.store, settle_seconds=5, recursive=True
        )
        self.assertIn(nested, [Path(job.source_path) for job in recursive_jobs])

    def test_safe_unicode_stem_and_deterministic_paths(self) -> None:
        _, job = self.register_pdf("医学:循環器?.pdf")
        output1 = deterministic_output_paths(job, self.paths)
        output2 = deterministic_output_paths(job, self.paths)
        self.assertEqual(output1, output2)
        self.assertIn("医学-循環器", output1.archive.name)
        self.assertNotIn(":", output1.archive.name)
        self.assertTrue(str(output1.archive).startswith(str(self.paths.archive)))
        self.assertEqual(safe_stem("...pdf"), "pdf")
        self.assertNotIn("..", safe_stem("../../escape.pdf"))

    def test_complete_vertical_slice_moves_original_and_writes_small_card(self) -> None:
        source, job = self.register_pdf()
        provider = FakeProvider()
        with patch(
            "knowledge_hub.ingest_pdf.os.link",
            side_effect=AssertionError("hard links must not be used"),
        ):
            result = process_one_pdf(
                self.paths, self.store, provider, extractor=fake_extract
            )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.status, "completed")
        self.assertFalse(source.exists())
        self.assertEqual(result.archived_path.read_bytes(), b"%PDF source")
        card = result.card_path.read_text(encoding="utf-8")
        self.assertIn(f'content_hash: "{job.content_hash}"', card)
        self.assertIn('source_type: "pdf"', card)
        self.assertIn("## Key points", card)
        self.assertNotIn("秘密の抽出全文", card)
        completed = self.store.get(job.content_hash)
        self.assertEqual(completed.status, "completed")

    def test_duplicate_content_is_not_processed_twice(self) -> None:
        _, job = self.register_pdf(content=b"same")
        provider = FakeProvider()
        process_one_pdf(self.paths, self.store, provider, extractor=fake_extract)
        duplicate = self.add_pdf("different-name.pdf", b"same")
        discovered = discover_pdf_jobs(self.paths, self.store, settle_seconds=5)
        self.assertEqual(discovered[0].content_hash, job.content_hash)
        self.assertIsNone(
            process_one_pdf(self.paths, self.store, provider, extractor=fake_extract)
        )
        self.assertTrue(duplicate.exists())
        self.assertEqual(len(provider.calls), 1)

    def test_retry_recovers_after_source_was_already_archived(self) -> None:
        source, job = self.register_pdf()
        provider = FakeProvider()
        with patch(
            "knowledge_hub.ingest_pdf._write_card_atomic",
            side_effect=RuntimeError("simulated crash after archive"),
        ):
            failed = process_one_pdf(
                self.paths, self.store, provider, extractor=fake_extract
            )
        self.assertEqual(failed.status, "failed")
        self.assertFalse(source.exists())
        output = deterministic_output_paths(job, self.paths)
        self.assertTrue(output.archive.exists())
        recovered = process_one_pdf(
            self.paths, self.store, provider, extractor=fake_extract
        )
        self.assertEqual(recovered.status, "completed")
        self.assertTrue(output.card.exists())

    def test_stale_hard_crash_claim_resumes_from_archive(self) -> None:
        source, job = self.register_pdf()
        claimed = self.store.claim_next()
        self.assertEqual(claimed.status, "processing")
        output = deterministic_output_paths(job, self.paths)
        output.archive.parent.mkdir(parents=True)
        shutil.move(source, output.archive)

        recovered = run_once(
            self.paths,
            provider=FakeProvider(),
            extractor=fake_extract,
            settle_seconds=0,
            stale_after_seconds=0,
        )
        self.assertEqual(recovered.status, "completed")
        self.assertTrue(output.card.exists())

    def test_archive_hash_collision_is_never_overwritten(self) -> None:
        source, job = self.register_pdf(content=b"expected")
        output = deterministic_output_paths(job, self.paths)
        output.archive.parent.mkdir(parents=True)
        output.archive.write_bytes(b"foreign")
        result = process_one_pdf(
            self.paths, self.store, FakeProvider(), extractor=fake_extract
        )
        self.assertEqual(result.error_code, "output_collision")
        self.assertEqual(output.archive.read_bytes(), b"foreign")
        self.assertTrue(source.exists())

    def test_existing_foreign_card_is_never_overwritten(self) -> None:
        _, job = self.register_pdf(content=b"expected")
        output = deterministic_output_paths(job, self.paths)
        output.card.parent.mkdir(parents=True)
        output.card.write_text("user-authored note", encoding="utf-8")
        result = process_one_pdf(
            self.paths, self.store, FakeProvider(), extractor=fake_extract
        )
        self.assertEqual(result.error_code, "output_collision")
        self.assertEqual(output.card.read_text(encoding="utf-8"), "user-authored note")
        self.assertTrue(output.archive.exists())

    def test_existing_same_hash_card_is_preserved_byte_for_byte(self) -> None:
        source, job = self.register_pdf(content=b"expected")
        output = deterministic_output_paths(job, self.paths)
        output.card.parent.mkdir(parents=True)
        existing = (
            f'---\ncontent_hash: "{job.content_hash}"\n---\n'
            "USER EDIT: this must survive exactly\n"
        ).encode("utf-8")
        output.card.write_bytes(existing)

        provider = FakeProvider()
        result = process_one_pdf(
            self.paths, self.store, provider, extractor=fake_extract
        )
        self.assertEqual(result.status, "completed")
        self.assertFalse(source.exists())
        self.assertEqual(output.card.read_bytes(), existing)
        self.assertEqual(len(provider.calls), 1)

    def test_completed_outputs_short_circuit_agent_during_crash_recovery(self) -> None:
        source, job = self.register_pdf(content=b"expected")
        output = deterministic_output_paths(job, self.paths)
        output.archive.parent.mkdir(parents=True)
        shutil.copy2(source, output.archive)
        source.unlink()
        output.card.parent.mkdir(parents=True)
        existing = (
            f'---\ncontent_hash: "{job.content_hash}"\n---\n'
            "USER EDIT: preserve without another agent call\n"
        ).encode("utf-8")
        output.card.write_bytes(existing)
        provider = FakeProvider()

        result = process_one_pdf(
            self.paths, self.store, provider, extractor=fake_extract
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(provider.calls, [])
        self.assertEqual(output.card.read_bytes(), existing)
        self.assertEqual(self.store.get(job.content_hash).status, "completed")

    def test_pdf_worker_does_not_claim_another_media_type(self) -> None:
        text_source = self.paths.inbox / "earlier.txt"
        text_source.write_text("plain text", encoding="utf-8")
        text_job, _ = self.store.register_file(text_source, media_type="text/plain")
        _, pdf_job = self.register_pdf(content=b"pdf follows text")

        result = process_one_pdf(
            self.paths, self.store, FakeProvider(), extractor=fake_extract
        )
        self.assertEqual(result.content_hash, pdf_job.content_hash)
        self.assertEqual(self.store.get(pdf_job.content_hash).status, "completed")
        self.assertEqual(self.store.get(text_job.content_hash).status, "pending")

    def test_needs_ocr_failure_is_classified_without_source_text(self) -> None:
        _, job = self.register_pdf()

        def needs_ocr(_: Path) -> PdfExtraction:
            raise PdfNeedsOcrError("do not store source body SECRET BODY")

        result = process_one_pdf(
            self.paths, self.store, FakeProvider(), extractor=needs_ocr
        )
        self.assertEqual(result.error_code, "pdf_needs_ocr")
        failed = self.store.get(job.content_hash)
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.last_error, "pdf_needs_ocr")

    def test_markdown_frontmatter_uses_json_quoting_and_escapes_heading(self) -> None:
        _, job = self.register_pdf()
        draft = CardDraft(
            'A: "quoted" # title',
            "line one\nline two: yes",
            "medicine: general",
            ["tag:one", "#tag"],
            ["[link](bad)", "# heading"],
        )
        card = render_card(job, draft, PdfExtraction("x", 1, False), Path("x.pdf"))
        frontmatter = card.split("---", 2)[1]
        values = {}
        for line in frontmatter.strip().splitlines():
            key, raw = line.split(":", 1)
            values[key] = json.loads(raw.strip())
        self.assertEqual(values["title"], 'A: "quoted" # title')
        self.assertEqual(values["tags"], ["tag:one", "#tag"])
        self.assertIn(r'# A: "quoted" \# title', card)
        self.assertIn(r"- \[link\](bad)", card)


if __name__ == "__main__":
    unittest.main()
