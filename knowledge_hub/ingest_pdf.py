"""One-file-at-a-time, crash-recoverable PDF ingest vertical slice."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable

from .agent_provider import (
    AgentCommandError,
    AgentProvider,
    AgentProviderError,
    AgentResponseError,
    AgentTimeoutError,
    CardDraft,
    CliAgentProvider,
)
from .config import ConfigError, PipelinePaths, ensure_pipeline_directories, resolve_pipeline_paths
from .job_store import JobRecord, JobStore, sha256_file
from .pdf_extract import (
    PdfCommandError,
    PdfExtraction,
    PdfExtractionError,
    PdfNeedsOcrError,
    PdfOutputTooLargeError,
    PdfTimeoutError,
    PdfToolMissingError,
    PdfTooLargeError,
    extract_pdf_text,
)


class IngestError(RuntimeError):
    """Base class for deterministic pipeline failures."""


class SourceUnavailableError(IngestError):
    """Neither the registered Inbox source nor its Archive copy is usable."""


class OutputCollisionError(IngestError):
    """A deterministic output path contains unrelated content."""


class UnsafePathError(IngestError):
    """A path crossed a configured Inbox, Archive, or Cards boundary."""


@dataclass(frozen=True)
class OutputPaths:
    archive: Path
    card: Path


@dataclass(frozen=True)
class ProcessResult:
    content_hash: str
    status: str
    archived_path: Path | None = None
    card_path: Path | None = None
    error_code: str | None = None


Extractor = Callable[[Path], PdfExtraction]


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def safe_stem(filename: str, max_length: int = 80) -> str:
    """Keep readable Unicode while removing traversal and filesystem hazards."""
    name = Path(filename).name
    suffix_start = name.rfind(".")
    # pathlib before Python 3.14 treated the tail of names made only of
    # leading dots (for example ``...pdf``) as a suffix.  Derive the stem
    # explicitly so those names have the same safe result on every supported
    # Python version while ordinary and compound extensions are still removed.
    if suffix_start > 0 and name[:suffix_start].strip("."):
        name = name[:suffix_start]
    stem = unicodedata.normalize("NFKC", name)
    characters: list[str] = []
    for character in stem:
        if character.isalnum() or character in " -_().":
            characters.append(character)
        else:
            characters.append("-")
    cleaned = "".join(characters).strip(" .-_")
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    while "  " in cleaned:
        cleaned = cleaned.replace("  ", " ")
    cleaned = cleaned[:max_length].rstrip(" .-_")
    return cleaned or "document"


def deterministic_output_paths(job: JobRecord, paths: PipelinePaths) -> OutputPaths:
    """Derive stable destinations only from immutable job metadata."""
    try:
        created = datetime.fromisoformat(job.created_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IngestError("job has an invalid created_at timestamp") from exc
    name = f"{safe_stem(job.original_name)}--{job.content_hash[:8]}"
    archive = paths.archive / f"{created.year:04d}" / f"{created.month:02d}" / (name + ".pdf")
    card = paths.cards / f"{created.year:04d}" / f"{created.month:02d}" / (name + ".md")
    if not _is_within(archive.resolve(strict=False), paths.archive.resolve(strict=True)):
        raise UnsafePathError("archive destination crossed its configured boundary")
    if not _is_within(card.resolve(strict=False), paths.cards.resolve(strict=True)):
        raise UnsafePathError("card destination crossed its configured boundary")
    return OutputPaths(archive, card)


def _candidate_files(inbox: Path, recursive: bool) -> Iterable[Path]:
    if not recursive:
        yield from inbox.iterdir()
        return
    for directory, dirnames, filenames in os.walk(inbox, followlinks=False):
        dirnames[:] = [
            name
            for name in dirnames
            if not name.startswith(".") and not (Path(directory) / name).is_symlink()
        ]
        for filename in filenames:
            yield Path(directory) / filename


def _is_ignored_name(relative: Path) -> bool:
    if any(part.startswith(".") for part in relative.parts):
        return True
    lower = relative.name.lower()
    stem_lower = Path(lower).stem
    return (
        lower.startswith("~$")
        or stem_lower.endswith((".tmp", ".part", ".crdownload", ".download"))
    )


def discover_pdf_jobs(
    paths: PipelinePaths,
    store: JobStore,
    *,
    settle_seconds: float = 5.0,
    recursive: bool = False,
    now: float | None = None,
) -> list[JobRecord]:
    """Register stable, regular ``.pdf`` files contained by Inbox."""
    if settle_seconds < 0:
        raise ValueError("settle_seconds must not be negative")
    inbox_root = paths.inbox.resolve(strict=True)
    observed_at = time.time() if now is None else now
    registered: list[JobRecord] = []
    for candidate in sorted(_candidate_files(paths.inbox, recursive), key=lambda item: str(item)):
        try:
            relative = candidate.relative_to(paths.inbox)
        except ValueError:
            continue
        try:
            if candidate.suffix.lower() != ".pdf" or _is_ignored_name(relative):
                continue
            if candidate.is_symlink() or not candidate.is_file():
                continue
            resolved = candidate.resolve(strict=True)
            if not _is_within(resolved, inbox_root):
                continue
            before = candidate.stat()
            if observed_at - before.st_mtime < settle_seconds:
                continue
            digest = sha256_file(candidate)
            after = candidate.stat()
        except OSError:
            # A syncing Inbox may remove or replace a file while it is scanned.
            # Treat that as unstable and leave it for the next invocation.
            continue
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            continue
        job, _ = store.register(
            digest,
            candidate,
            original_name=candidate.name,
            size_bytes=after.st_size,
            media_type="application/pdf",
        )
        registered.append(job)
    return registered


def _ensure_parent(destination: Path, root: Path) -> None:
    root_resolved = root.resolve(strict=True)
    parent = destination.parent
    if not _is_within(parent.resolve(strict=False), root_resolved):
        raise UnsafePathError("output parent crossed its configured boundary")
    parent.mkdir(parents=True, exist_ok=True)
    if not _is_within(parent.resolve(strict=True), root_resolved):
        raise UnsafePathError("output parent resolved outside its configured boundary")


def _verify_hash(path: Path, expected: str) -> None:
    if path.is_symlink() or not path.is_file() or sha256_file(path) != expected:
        raise OutputCollisionError("existing output does not match the source hash")


def _locate_source(job: JobRecord, paths: PipelinePaths, output: OutputPaths) -> Path:
    if output.archive.exists() or output.archive.is_symlink():
        _verify_hash(output.archive, job.content_hash)
        return output.archive
    source = Path(job.source_path)
    if source.is_symlink() or not source.is_file():
        raise SourceUnavailableError("registered PDF and Archive copy are unavailable")
    if not _is_within(source.resolve(strict=True), paths.inbox.resolve(strict=True)):
        raise UnsafePathError("registered source is outside Inbox")
    if sha256_file(source) != job.content_hash:
        raise SourceUnavailableError("registered PDF changed after discovery")
    return source


def _archive_source(
    source: Path,
    destination: Path,
    expected_hash: str,
    inbox: Path,
    archive_root: Path,
) -> Path:
    _ensure_parent(destination, archive_root)
    if destination.exists() or destination.is_symlink():
        _verify_hash(destination, expected_hash)
    else:
        handle = tempfile.NamedTemporaryFile(
            mode="w+b",
            prefix=".pdf-ingest-",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        )
        temporary = Path(handle.name)
        try:
            with handle, source.open("rb") as source_file:
                shutil.copyfileobj(source_file, handle)
                handle.flush()
                os.fsync(handle.fileno())
            _verify_hash(temporary, expected_hash)
            # File Provider volumes may not support hard links. Recheck at the
            # last possible moment, then publish by a portable same-directory
            # atomic rename. A pre-existing unrelated file is never replaced.
            if destination.exists() or destination.is_symlink():
                _verify_hash(destination, expected_hash)
            else:
                os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    if source != destination:
        source_resolved = source.resolve(strict=True)
        if _is_within(source_resolved, inbox.resolve(strict=True)):
            source.unlink()
    return destination


def _yaml_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _markdown_inline(value: str) -> str:
    compact = " ".join(value.splitlines())
    for character in "\\`*_{}[]<>#":
        compact = compact.replace(character, "\\" + character)
    return compact


def _markdown_text(value: str) -> str:
    """Render agent prose as text, not executable Markdown or raw HTML."""
    return "\n".join(
        _markdown_inline(line) if line.strip() else "" for line in value.splitlines()
    )


def render_card(
    job: JobRecord,
    draft: CardDraft,
    extraction: PdfExtraction,
    archive_relative: Path,
) -> str:
    source_file = archive_relative.as_posix()
    frontmatter = {
        "id": "pdf-" + job.content_hash,
        "content_hash": job.content_hash,
        "title": draft.title,
        "summary": draft.summary,
        "source_type": "pdf",
        "source_file": source_file,
        "captured_at": job.created_at,
        "category": draft.category,
        "tags": draft.tags,
        "extracted_char_count": extraction.extracted_char_count,
        "truncated": extraction.truncated,
    }
    lines = ["---"]
    lines.extend(f"{key}: {_yaml_value(value)}" for key, value in frontmatter.items())
    lines.extend(
        [
            "---",
            "",
            "# " + _markdown_inline(draft.title),
            "",
            _markdown_text(draft.summary.strip()),
            "",
            "## Key points",
            "",
        ]
    )
    lines.extend("- " + _markdown_inline(point) for point in draft.key_points)
    lines.extend(["", "## Original", "", f"Archive: `{source_file}`", ""])
    return "\n".join(lines)


def _existing_card_hash(path: Path) -> str | None:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        return None
    with path.open("r", encoding="utf-8") as card:
        for index, line in enumerate(card):
            if index > 40 or line.strip() == "---" and index > 0:
                break
            if line.startswith("content_hash:"):
                raw = line.split(":", 1)[1].strip()
                try:
                    value = json.loads(raw)
                except json.JSONDecodeError:
                    return None
                return value if isinstance(value, str) else None
    return None


def _write_card_atomic(
    destination: Path, content: str, expected_hash: str, cards_root: Path
) -> None:
    _ensure_parent(destination, cards_root)
    if destination.exists() or destination.is_symlink():
        if _existing_card_hash(destination) != expected_hash:
            raise OutputCollisionError("existing card belongs to different content")
        # A same-hash card may have been edited by the user after generation.
        # Content identity is sufficient for recovery; never erase those edits.
        return
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=".card-",
        suffix=".tmp",
        dir=destination.parent,
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        # Recheck immediately before the portable atomic rename. This favours
        # iCloud/Google Drive File Provider compatibility over hard-link-based
        # publication while still refusing every observed collision.
        if destination.exists() or destination.is_symlink():
            if _existing_card_hash(destination) != expected_hash:
                raise OutputCollisionError("card destination was claimed by other content")
        else:
            os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _failure_code(error: Exception) -> str:
    mappings = (
        (PdfNeedsOcrError, "pdf_needs_ocr"),
        (PdfToolMissingError, "pdf_tool_missing"),
        (PdfTimeoutError, "pdf_extract_timeout"),
        (PdfTooLargeError, "pdf_input_too_large"),
        (PdfOutputTooLargeError, "pdf_output_too_large"),
        (PdfCommandError, "pdf_extract_failed"),
        (AgentTimeoutError, "agent_timeout"),
        (AgentResponseError, "agent_invalid_response"),
        (AgentCommandError, "agent_command_failed"),
        (OutputCollisionError, "output_collision"),
        (UnsafePathError, "unsafe_path"),
        (SourceUnavailableError, "source_unavailable"),
    )
    for error_type, code in mappings:
        if isinstance(error, error_type):
            return code
    if isinstance(error, PdfExtractionError):
        return "pdf_extract_failed"
    if isinstance(error, AgentProviderError):
        return "agent_failed"
    return "processing_error"


def process_one_pdf(
    paths: PipelinePaths,
    store: JobStore,
    provider: AgentProvider,
    *,
    extractor: Extractor = extract_pdf_text,
    max_attempts: int = 3,
) -> ProcessResult | None:
    """Claim and finish at most one job, recording a sanitized failure code."""
    job = store.claim_next(
        max_attempts=max_attempts, media_type="application/pdf"
    )
    if job is None:
        return None
    try:
        output = deterministic_output_paths(job, paths)
        if output.archive.exists() or output.archive.is_symlink():
            _verify_hash(output.archive, job.content_hash)
            if output.card.exists() or output.card.is_symlink():
                if _existing_card_hash(output.card) != job.content_hash:
                    raise OutputCollisionError(
                        "existing card belongs to different content"
                    )
                # Crash recovery after both durable outputs were published:
                # preserve possible user edits and do not spend another AI call.
                store.mark_completed(job.content_hash, output.archive, output.card)
                return ProcessResult(
                    job.content_hash, "completed", output.archive, output.card
                )
        source = _locate_source(job, paths, output)
        extraction = extractor(source)
        draft = provider.create_card(
            extraction.text,
            {
                "content_hash": job.content_hash,
                "original_name": job.original_name,
                "source_type": "pdf",
                "extracted_char_count": extraction.extracted_char_count,
                "truncated": extraction.truncated,
            },
        )
        archived = _archive_source(
            source,
            output.archive,
            job.content_hash,
            paths.inbox,
            paths.archive,
        )
        archive_relative = archived.relative_to(paths.archive)
        card_content = render_card(job, draft, extraction, archive_relative)
        _write_card_atomic(output.card, card_content, job.content_hash, paths.cards)
        store.mark_completed(job.content_hash, archived, output.card)
        return ProcessResult(job.content_hash, "completed", archived, output.card)
    except Exception as error:
        code = _failure_code(error)
        store.mark_failed(job.content_hash, code)
        return ProcessResult(job.content_hash, "failed", error_code=code)


def run_once(
    paths: PipelinePaths,
    *,
    provider: AgentProvider | None = None,
    extractor: Extractor = extract_pdf_text,
    settle_seconds: float = 5.0,
    recursive: bool = False,
    max_attempts: int = 3,
    stale_after_seconds: float = 900.0,
) -> ProcessResult | None:
    if stale_after_seconds < 0:
        raise ValueError("stale_after_seconds must not be negative")
    ensure_pipeline_directories(paths)
    store = JobStore(paths.state_db)
    # A killed process cannot mark_failed. Requeue abandoned claims so a retry
    # can resume from the deterministic Archive path after the grace period.
    store.requeue_stale_processing(stale_after=timedelta(seconds=stale_after_seconds))
    discover_pdf_jobs(
        paths, store, settle_seconds=settle_seconds, recursive=recursive
    )
    return process_one_pdf(
        paths,
        store,
        provider or CliAgentProvider(),
        extractor=extractor,
        max_attempts=max_attempts,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Process one stable PDF from the local Inbox")
    parser.add_argument("--once", action="store_true", help="discover and process at most one PDF")
    parser.add_argument("--vault")
    parser.add_argument("--archive")
    parser.add_argument("--inbox")
    parser.add_argument("--cards")
    parser.add_argument("--state-db")
    parser.add_argument("--settle-seconds", type=float, default=5.0)
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--stale-after-seconds", type=float, default=900.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.once:
        print("--once is currently required")
        return 2
    try:
        paths = resolve_pipeline_paths(
            args.vault,
            archive_value=args.archive,
            inbox_value=args.inbox,
            cards_value=args.cards,
            state_db_value=args.state_db,
        )
        result = run_once(
            paths,
            settle_seconds=args.settle_seconds,
            recursive=args.recursive,
            max_attempts=args.max_attempts,
            stale_after_seconds=args.stale_after_seconds,
        )
    except (ConfigError, ValueError) as error:
        print(str(error))
        return 2
    if result is None:
        print(json.dumps({"status": "idle"}, ensure_ascii=False))
        return 0
    print(
        json.dumps(
            {
                "status": result.status,
                "content_hash": result.content_hash,
                "archived_path": str(result.archived_path) if result.archived_path else None,
                "card_path": str(result.card_path) if result.card_path else None,
                "error_code": result.error_code,
            },
            ensure_ascii=False,
        )
    )
    return 0 if result.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
