"""Bounded PDF text extraction for the local-first ingest pipeline."""
from __future__ import annotations

import os
import selectors
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


DEFAULT_MAX_PDF_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_OUTPUT_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_AGENT_CHARS = 100_000
DEFAULT_MIN_TEXT_CHARS = 40
DEFAULT_TIMEOUT = 60.0
MAX_STDERR_BYTES = 8_192


class PdfExtractionError(RuntimeError):
    """Base class for safe, user-facing PDF extraction failures."""


class PdfToolMissingError(PdfExtractionError):
    """The configured pdftotext executable was not found."""


class PdfTimeoutError(PdfExtractionError):
    """pdftotext exceeded its configured deadline."""


class PdfTooLargeError(PdfExtractionError):
    """The source PDF exceeds the configured input limit."""


class PdfOutputTooLargeError(PdfExtractionError):
    """Extracted output exceeds the configured stdout limit."""


class PdfNeedsOcrError(PdfExtractionError):
    """The PDF has too little embedded text and needs OCR."""


class PdfCommandError(PdfExtractionError):
    """pdftotext exited unsuccessfully."""


@dataclass(frozen=True)
class PdfExtraction:
    """Text safe to send to an agent, plus explicit truncation metadata."""

    text: str
    extracted_char_count: int
    truncated: bool


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise PdfExtractionError(f"{name} must be an integer") from exc
    if value <= 0:
        raise PdfExtractionError(f"{name} must be positive")
    return value


def _positive_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise PdfExtractionError(f"{name} must be a number") from exc
    if value <= 0:
        raise PdfExtractionError(f"{name} must be positive")
    return value


def _positive_explicit(value: int | float, name: str) -> int | float:
    if value <= 0:
        raise PdfExtractionError(f"{name} must be positive")
    return value


def _bounded_command(argv: list[str], timeout: float, max_stdout: int) -> bytes:
    """Run one command without shell expansion and bound both time and stdout.

    Pipes are drained concurrently with ``selectors`` so a noisy stderr cannot
    deadlock the process.  The process is killed as soon as stdout exceeds the
    configured limit rather than collecting an unbounded response in memory.
    """
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise PdfToolMissingError("pdftotext command was not found") from exc
    assert process.stdout is not None
    assert process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    stdout = bytearray()
    stderr = bytearray()
    deadline = time.monotonic() + timeout
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                process.wait()
                raise PdfTimeoutError("pdftotext timed out")
            events = selector.select(min(remaining, 0.2))
            if not events and process.poll() is not None:
                # A final select iteration drains EOF from both pipes.
                events = selector.select(0)
            for key, _ in events:
                chunk = os.read(key.fileobj.fileno(), 65_536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stdout":
                    stdout.extend(chunk)
                    if len(stdout) > max_stdout:
                        process.kill()
                        process.wait()
                        raise PdfOutputTooLargeError(
                            "pdftotext output exceeded the configured limit"
                        )
                elif len(stderr) < MAX_STDERR_BYTES:
                    stderr.extend(chunk[: MAX_STDERR_BYTES - len(stderr)])
        return_code = process.wait()
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
        if process.poll() is None:
            process.kill()
            process.wait()
    if return_code != 0:
        # Do not put extracted source text or untrusted stderr into the error.
        raise PdfCommandError(f"pdftotext failed with exit code {return_code}")
    return bytes(stdout)


def extract_pdf_text(
    source: Path,
    *,
    command: str | None = None,
    timeout: float | None = None,
    max_pdf_bytes: int | None = None,
    max_output_bytes: int | None = None,
    max_agent_chars: int | None = None,
    min_text_chars: int | None = None,
) -> PdfExtraction:
    """Extract embedded PDF text using Poppler's ``pdftotext``.

    OCR is intentionally not hidden behind this function.  A scanned document
    raises :class:`PdfNeedsOcrError`, allowing a later OCR adapter to be added
    without ever producing a misleading empty card.
    """
    path = Path(source)
    input_limit = (
        int(_positive_explicit(max_pdf_bytes, "max_pdf_bytes"))
        if max_pdf_bytes is not None
        else _positive_int_env("KH_PDF_MAX_BYTES", DEFAULT_MAX_PDF_BYTES)
    )
    output_limit = (
        int(_positive_explicit(max_output_bytes, "max_output_bytes"))
        if max_output_bytes is not None
        else _positive_int_env("KH_PDF_MAX_OUTPUT_BYTES", DEFAULT_MAX_OUTPUT_BYTES)
    )
    agent_limit = (
        int(_positive_explicit(max_agent_chars, "max_agent_chars"))
        if max_agent_chars is not None
        else _positive_int_env("KH_PDF_MAX_AGENT_CHARS", DEFAULT_MAX_AGENT_CHARS)
    )
    minimum = (
        int(_positive_explicit(min_text_chars, "min_text_chars"))
        if min_text_chars is not None
        else _positive_int_env("KH_PDF_MIN_TEXT_CHARS", DEFAULT_MIN_TEXT_CHARS)
    )
    deadline = (
        float(_positive_explicit(timeout, "timeout"))
        if timeout is not None
        else _positive_float_env("KH_PDFTOTEXT_TIMEOUT", DEFAULT_TIMEOUT)
    )
    size = path.stat().st_size
    if size > input_limit:
        raise PdfTooLargeError("PDF exceeds the configured input-size limit")

    configured = (
        command
        if command is not None
        else os.environ.get("KH_PDFTOTEXT_CMD", "pdftotext")
    )
    argv = shlex.split(configured)
    if not argv:
        raise PdfToolMissingError("pdftotext command is empty")
    raw = _bounded_command(argv + ["-layout", str(path), "-"], deadline, output_limit)
    text = raw.decode("utf-8", errors="replace").replace("\x00", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    meaningful = sum(1 for character in text if character.isalnum())
    if meaningful < minimum:
        raise PdfNeedsOcrError("PDF has insufficient embedded text and needs OCR")
    extracted_chars = len(text)
    truncated = extracted_chars > agent_limit
    if truncated:
        text = text[:agent_limit]
    return PdfExtraction(text, extracted_chars, truncated)
