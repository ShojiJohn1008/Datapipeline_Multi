from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from knowledge_hub.pdf_extract import (
    PdfExtractionError,
    PdfNeedsOcrError,
    PdfOutputTooLargeError,
    PdfTimeoutError,
    PdfToolMissingError,
    PdfTooLargeError,
    extract_pdf_text,
)


class TestPdfExtraction(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.pdf = self.root / "input.pdf"
        self.pdf.write_bytes(b"%PDF-fake")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def command(self, body: str) -> str:
        script = self.root / ("fake_" + str(len(list(self.root.glob("fake_*")))) + ".py")
        script.write_text(body, encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        return f'{sys.executable} "{script}"'

    def test_extract_success_and_explicit_truncation(self) -> None:
        command = self.command("print('日本語の本文です。' * 30)\n")
        result = extract_pdf_text(
            self.pdf,
            command=command,
            min_text_chars=5,
            max_agent_chars=30,
        )
        self.assertEqual(len(result.text), 30)
        self.assertGreater(result.extracted_char_count, 30)
        self.assertTrue(result.truncated)

    def test_scanned_or_empty_pdf_requires_ocr(self) -> None:
        command = self.command("print('  \\n  ')\n")
        with self.assertRaises(PdfNeedsOcrError):
            extract_pdf_text(self.pdf, command=command, min_text_chars=5)

    def test_timeout_is_a_dedicated_error(self) -> None:
        command = self.command("import time\ntime.sleep(2)\nprint('late')\n")
        with self.assertRaises(PdfTimeoutError):
            extract_pdf_text(self.pdf, command=command, timeout=0.05)

    def test_missing_tool_is_a_dedicated_error(self) -> None:
        with self.assertRaises(PdfToolMissingError):
            extract_pdf_text(self.pdf, command=str(self.root / "missing-tool"))

    def test_input_and_output_limits_fail_loudly(self) -> None:
        command = self.command("print('x' * 10000)\n")
        with self.assertRaises(PdfTooLargeError):
            extract_pdf_text(self.pdf, command=command, max_pdf_bytes=2)
        with self.assertRaises(PdfOutputTooLargeError):
            extract_pdf_text(self.pdf, command=command, max_output_bytes=100)

    def test_explicit_nonpositive_limits_are_never_replaced_by_defaults(self) -> None:
        command = self.command("print('enough embedded text ' * 10)\n")
        arguments = (
            {"max_pdf_bytes": 0},
            {"max_output_bytes": -1},
            {"max_agent_chars": 0},
            {"min_text_chars": -1},
            {"timeout": 0},
        )
        for argument in arguments:
            with self.subTest(argument=argument), self.assertRaises(PdfExtractionError):
                extract_pdf_text(self.pdf, command=command, **argument)


if __name__ == "__main__":
    unittest.main()
