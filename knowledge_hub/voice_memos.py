"""Read-only Apple Voice Memos adapter for the shared local job pipeline.

The small JSON state file deliberately records only observations needed to
protect the Voice Memos source (baseline and two-scan stability).  Job status,
retries, output paths, and content-hash deduplication belong to ``JobStore``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import (
    ConfigError,
    PipelinePaths,
    ensure_pipeline_directories,
    resolve_pipeline_paths,
)
from .agent_provider import AgentCommandError, AgentProvider, CardDraft, CliAgentProvider
from .ingest_pdf import (
    OutputCollisionError,
    SourceUnavailableError,
    UnsafePathError,
    _ensure_parent,
    _is_within,
    _markdown_inline,
    _markdown_text,
    _verify_hash,
    _write_card_atomic,
)
from .job_store import JobRecord, JobStore, sha256_file


DEFAULT_SOURCE = Path(
    "~/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings"
).expanduser()
DEFAULT_STATE = Path(
    "~/Library/Application Support/Datapipeline_Multi/voice_memos_observations.json"
).expanduser()
STATE_VERSION = 2
AUDIO_MEDIA_TYPE = "audio/mp4"
TRANSCRIPT_SCHEMA = "knowledge_hub.voice_memos.transcript"
TRANSCRIPT_VERSION = 1
MAX_TRANSCRIPT_SIDECAR_BYTES = 32 * 1024 * 1024


class SourcePermissionError(RuntimeError):
    """Voice Memos のTCC/Full Disk Accessにより読めない。"""


class AudioTranscriptionError(RuntimeError):
    """A safe, retryable transcription failure."""


class TranscriptSidecarError(RuntimeError):
    """A deterministic transcript sidecar is malformed or unsafe."""


class AudioSummaryError(RuntimeError):
    """A safe retryable failure at the optional semantic-summary boundary."""


@dataclass(frozen=True)
class AudioOutputPaths:
    archive: Path
    transcript: Path
    card: Path


@dataclass(frozen=True)
class AudioProcessResult:
    content_hash: str
    status: str
    archived_path: Path | None = None
    card_path: Path | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class TranscriptRecord:
    transcript: str
    generated_at: str
    transcriber: str
    model: str
    language: str


@dataclass
class ScanReport:
    discovered: int = 0
    pending: int = 0
    registered: int = 0
    archived: int = 0
    cards: int = 0
    retried: int = 0
    errors: list[str] = field(default_factory=list)


def _stat_signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def _key(relative: Path, size: int, mtime_ns: int) -> str:
    return "{}\0{}\0{}".format(relative.as_posix(), size, mtime_ns)


def _empty_state(*, baseline_cutover_mtime_ns: int | None = None) -> dict[str, Any]:
    state: dict[str, Any] = {"version": STATE_VERSION, "items": {}}
    if baseline_cutover_mtime_ns is not None:
        state["baseline_cutover_mtime_ns"] = baseline_cutover_mtime_ns
    return state


def _load_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as stream:
            raw = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError("Voice Memos の観測状態ファイルを読めません: {}".format(exc))
    if not isinstance(raw, dict) or raw.get("version") not in (1, STATE_VERSION):
        raise ConfigError("Voice Memos の観測状態ファイル形式が不正です: {}".format(path))
    raw_items = raw.get("items")
    if not isinstance(raw_items, dict):
        raise ConfigError("Voice Memos の観測状態ファイル形式が不正です: {}".format(path))
    # v1 stored processing fields before the shared JobStore existed.  Drop
    # those fields on read; this JSON must never be a second processing ledger.
    state = _empty_state()
    cutover = raw.get("baseline_cutover_mtime_ns")
    if isinstance(cutover, int):
        state["baseline_cutover_mtime_ns"] = cutover
    for item_key, item in raw_items.items():
        if not isinstance(item_key, str) or not isinstance(item, dict):
            continue
        source_rel = item.get("source_rel")
        size = item.get("size")
        mtime_ns = item.get("mtime_ns")
        if not isinstance(source_rel, str) or not isinstance(size, int) or not isinstance(mtime_ns, int):
            continue
        state["items"][item_key] = {
            "source_rel": source_rel,
            "size": size,
            "mtime_ns": mtime_ns,
            "status": "baselined" if item.get("status") == "baselined" else "observing",
            "stable_scans": int(item.get("stable_scans", 2)),
        }
    return state


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".voice-memos-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _iter_recordings(source: Path) -> list[Path]:
    try:
        if not source.is_dir():
            raise ConfigError("Voice Memos のソースが見つからない: {}".format(source))
    except PermissionError as exc:
        raise SourcePermissionError(str(exc))
    problems: list[OSError] = []

    def onerror(error: OSError) -> None:
        problems.append(error)

    recordings: list[Path] = []
    for root, _, names in os.walk(source, onerror=onerror):
        for name in names:
            if name.lower().endswith(".m4a"):
                recordings.append(Path(root) / name)
    if problems:
        error = problems[0]
        if isinstance(error, PermissionError) or error.errno in (1, 13):
            raise SourcePermissionError(str(error))
        raise ConfigError("Voice Memos のソースを走査できません: {}".format(error))
    return sorted(recordings)


def _yaml_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _excerpt(transcript: str, limit: int = 160) -> str:
    compact = " ".join(transcript.split())
    if not compact:
        return "文字起こし結果なし"
    return compact[:limit - 1] + "…" if len(compact) > limit else compact


def _transcript_sha256(transcript: str) -> str:
    """Hash the exact UTF-8 transcript persisted in the sidecar."""
    try:
        return hashlib.sha256(transcript.encode("utf-8")).hexdigest()
    except UnicodeEncodeError as exc:
        raise TranscriptSidecarError("transcript_sidecar_invalid") from exc


def _transcriber_provenance(command: str) -> tuple[str, str, str]:
    """Keep useful provenance without persisting a possibly sensitive command line."""
    argv = shlex.split(command)
    return (
        Path(argv[0]).name,
        os.environ.get("KH_MLX_WHISPER_MODEL", "unspecified"),
        os.environ.get("KH_MLX_WHISPER_LANGUAGE", "unspecified"),
    )


def _run_transcriber(command: str | None, audio: Path, timeout: float = 7200.0) -> TranscriptRecord:
    if not command:
        raise AudioTranscriptionError("audio_transcriber_not_configured")
    try:
        argv = shlex.split(command)
    except ValueError:
        raise AudioTranscriptionError("audio_transcriber_invalid_command")
    if not argv:
        raise AudioTranscriptionError("audio_transcriber_not_configured")
    try:
        result = subprocess.run(argv + [str(audio)], text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise AudioTranscriptionError("audio_transcription_timeout")
    except OSError:
        raise AudioTranscriptionError("audio_transcriber_unavailable")
    if result.returncode != 0:
        # Child stderr can contain sensitive transcript/source details; never retain it.
        raise AudioTranscriptionError("audio_transcription_failed")
    transcriber, model, language = _transcriber_provenance(command)
    return TranscriptRecord(
        transcript=result.stdout.strip(),
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        transcriber=transcriber,
        model=model,
        language=language,
    )


def audio_output_paths(job: JobRecord, paths: PipelinePaths) -> AudioOutputPaths:
    try:
        created = datetime.fromisoformat(job.created_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("audio_job_invalid_created_at") from exc
    # Do not expose a Voice Memo's private recording name in output paths.
    filename = "voice-memo--{}".format(job.content_hash[:8])
    archive = paths.archive / "{:04d}".format(created.year) / "{:02d}".format(created.month) / (filename + ".m4a")
    transcript = archive.with_suffix(".transcript.json")
    card = paths.cards / "{:04d}".format(created.year) / "{:02d}".format(created.month) / (filename + ".md")
    if not _is_within(archive.resolve(strict=False), paths.archive.resolve(strict=True)):
        raise UnsafePathError("audio archive destination crossed its configured boundary")
    if not _is_within(card.resolve(strict=False), paths.cards.resolve(strict=True)):
        raise UnsafePathError("audio card destination crossed its configured boundary")
    if not _is_within(transcript.resolve(strict=False), paths.archive.resolve(strict=True)):
        raise UnsafePathError("audio transcript destination crossed its configured boundary")
    return AudioOutputPaths(archive, transcript, card)


def _copy_to_archive(source: Path, destination: Path, expected_hash: str, archive_root: Path) -> Path:
    """Atomically copy without ever unlinking, renaming, or editing the source."""
    _ensure_parent(destination, archive_root)
    if destination.exists() or destination.is_symlink():
        _verify_hash(destination, expected_hash)
        return destination
    source_stat = source.stat()
    handle = tempfile.NamedTemporaryFile(mode="w+b", prefix=".audio-ingest-", suffix=".tmp",
                                         dir=destination.parent, delete=False)
    temporary = Path(handle.name)
    try:
        with handle, source.open("rb") as source_file:
            shutil.copyfileobj(source_file, handle)
            handle.flush()
            os.fsync(handle.fileno())
        # Set metadata only after all buffered content is durable: a buffered
        # flush can otherwise overwrite the preserved source mtime. The Archive
        # copy is the retry source, so this keeps captured_at stable on retry.
        os.utime(temporary, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
        _verify_hash(temporary, expected_hash)
        if destination.exists() or destination.is_symlink():
            _verify_hash(destination, expected_hash)
        else:
            os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _read_transcript_sidecar(path: Path, expected_hash: str) -> TranscriptRecord:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_TRANSCRIPT_SIDECAR_BYTES:
            raise TranscriptSidecarError("transcript_sidecar_invalid")
        with path.open(encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise TranscriptSidecarError("transcript_sidecar_invalid") from exc
    if not isinstance(payload, dict):
        raise TranscriptSidecarError("transcript_sidecar_invalid")
    if payload.get("schema") != TRANSCRIPT_SCHEMA or payload.get("version") != TRANSCRIPT_VERSION:
        raise TranscriptSidecarError("transcript_sidecar_invalid")
    if payload.get("content_hash") != expected_hash or payload.get("source_sha256") != expected_hash:
        raise OutputCollisionError("transcript_sidecar_hash_collision")
    transcript = payload.get("transcript")
    transcript_sha256 = payload.get("transcript_sha256")
    generated_at = payload.get("generated_at")
    transcriber = payload.get("transcriber")
    model = payload.get("model")
    language = payload.get("language")
    if not all(isinstance(value, str) for value in
               (transcript, transcript_sha256, generated_at, transcriber, model, language)):
        raise TranscriptSidecarError("transcript_sidecar_invalid")
    if transcript_sha256 != _transcript_sha256(transcript):
        raise TranscriptSidecarError("transcript_sidecar_invalid")
    return TranscriptRecord(transcript, generated_at, transcriber, model, language)


def _write_transcript_sidecar(destination: Path, record: TranscriptRecord, expected_hash: str,
                              archive_root: Path) -> TranscriptRecord:
    """Atomically publish or validate the deterministic sidecar for one audio hash."""
    _ensure_parent(destination, archive_root)
    if destination.exists() or destination.is_symlink():
        return _read_transcript_sidecar(destination, expected_hash)
    payload = {
        "schema": TRANSCRIPT_SCHEMA,
        "version": TRANSCRIPT_VERSION,
        "content_hash": expected_hash,
        "source_sha256": expected_hash,
        "transcriber": record.transcriber,
        "model": record.model,
        "language": record.language,
        "generated_at": record.generated_at,
        "transcript": record.transcript,
        "transcript_sha256": _transcript_sha256(record.transcript),
    }
    try:
        serialized = (
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise TranscriptSidecarError("transcript_sidecar_invalid") from exc
    if len(serialized) > MAX_TRANSCRIPT_SIDECAR_BYTES:
        raise TranscriptSidecarError("transcript_sidecar_invalid")
    handle = tempfile.NamedTemporaryFile(mode="wb", prefix=".transcript-",
                                         suffix=".tmp", dir=destination.parent, delete=False)
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        if destination.exists() or destination.is_symlink():
            return _read_transcript_sidecar(destination, expected_hash)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return _read_transcript_sidecar(destination, expected_hash)


def _locate_source(job: JobRecord, paths: PipelinePaths, source_root: Path,
                   output: AudioOutputPaths) -> Path:
    if output.archive.exists() or output.archive.is_symlink():
        _verify_hash(output.archive, job.content_hash)
        return output.archive
    source = Path(job.source_path)
    if source.is_symlink() or not source.is_file():
        raise SourceUnavailableError("audio_source_unavailable")
    try:
        resolved = source.resolve(strict=True)
    except OSError as exc:
        raise SourceUnavailableError("audio_source_unavailable") from exc
    if not _is_within(resolved, source_root.resolve(strict=True)):
        raise UnsafePathError("audio source crossed its configured boundary")
    try:
        if sha256_file(source) != job.content_hash:
            raise SourceUnavailableError("audio_source_changed")
    except OSError as exc:
        raise SourceUnavailableError("audio_source_unavailable") from exc
    return source


def _fallback_draft(transcript: str, captured_at: str) -> CardDraft:
    summary = _excerpt(transcript)
    return CardDraft(
        title="Voice Memo " + captured_at.replace("T", " ")[:16],
        summary=summary,
        category="audio",
        tags=["voice_memos", "audio"],
        key_points=[summary],
    )


def render_audio_card(job: JobRecord, archive_relative: Path, transcript: str,
                      captured_at: str, draft: CardDraft) -> str:
    transcript_relative = archive_relative.with_suffix(".transcript.json")
    frontmatter = {
        "id": "audio-" + job.content_hash,
        "content_hash": job.content_hash,
        "title": draft.title,
        "captured_at": captured_at,
        "summary": draft.summary,
        "category": draft.category,
        "tags": draft.tags,
        "key_points": draft.key_points,
        "source_type": "audio",
        "source_app": "voice_memos",
        "source_path": archive_relative.as_posix(),
        "source_sha256": job.content_hash,
        "transcript_path": transcript_relative.as_posix(),
    }
    lines = ["---"]
    lines.extend("{}: {}".format(key, _yaml_value(value)) for key, value in frontmatter.items())
    lines.extend([
        "---",
        "",
        "# " + _markdown_inline(draft.title),
        "",
        "## Summary",
        "",
        _markdown_text(draft.summary.strip()),
        "",
        "## Key points",
        "",
    ])
    lines.extend("- " + _markdown_inline(point) for point in draft.key_points)
    lines.extend([
        "",
        "## Original",
        "",
        "Archive: `{}`".format(archive_relative.as_posix()),
        "",
        "Transcript sidecar: `{}`".format(transcript_relative.as_posix()),
        "",
        "## Transcript",
        "",
        _markdown_text(transcript),
        "",
    ])
    return "\n".join(lines)


def _audio_failure_code(error: Exception) -> str:
    if isinstance(error, AudioTranscriptionError):
        return str(error)
    if isinstance(error, TranscriptSidecarError):
        return str(error)
    if isinstance(error, AudioSummaryError):
        return "audio_summary_failed"
    if isinstance(error, OutputCollisionError):
        return "output_collision"
    if isinstance(error, UnsafePathError):
        return "unsafe_path"
    if isinstance(error, SourceUnavailableError):
        return "audio_source_unavailable"
    return "audio_processing_failed"


def process_one_audio(paths: PipelinePaths, store: JobStore, source_root: Path, *,
                      transcribe_command: str | None, transcribe_timeout: float = 7200.0,
                      max_attempts: int = 3,
                      summary_provider: AgentProvider | None = None) -> AudioProcessResult | None:
    """Claim exactly one ``audio/mp4`` job; PDF jobs remain untouched."""
    job = store.claim_next(max_attempts=max_attempts, media_type=AUDIO_MEDIA_TYPE)
    if job is None:
        return None
    try:
        output = audio_output_paths(job, paths)
        if output.archive.exists() or output.archive.is_symlink():
            _verify_hash(output.archive, job.content_hash)
            if output.card.exists() or output.card.is_symlink():
                from .ingest_pdf import _existing_card_hash
                # A legacy/incomplete card is not completion: require the
                # deterministic sidecar before accepting crash recovery.
                _read_transcript_sidecar(output.transcript, job.content_hash)
                if _existing_card_hash(output.card) != job.content_hash:
                    raise OutputCollisionError("audio_card_collision")
                store.mark_completed(job.content_hash, output.archive, output.card)
                return AudioProcessResult(job.content_hash, "completed", output.archive, output.card)
        source = _locate_source(job, paths, source_root, output)
        archived = _copy_to_archive(source, output.archive, job.content_hash, paths.archive)
        if output.transcript.exists() or output.transcript.is_symlink():
            transcript_record = _read_transcript_sidecar(output.transcript, job.content_hash)
        else:
            transcript_record = _write_transcript_sidecar(
                output.transcript,
                _run_transcriber(transcribe_command, archived, transcribe_timeout),
                job.content_hash,
                paths.archive,
            )
        if output.card.exists() or output.card.is_symlink():
            from .ingest_pdf import _existing_card_hash
            if _existing_card_hash(output.card) != job.content_hash:
                raise OutputCollisionError("audio_card_collision")
            store.mark_completed(job.content_hash, archived, output.card)
            return AudioProcessResult(job.content_hash, "completed", archived, output.card)
        captured_at = datetime.fromtimestamp(archived.stat().st_mtime, timezone.utc).isoformat(
            timespec="seconds"
        )
        if summary_provider is None:
            draft = _fallback_draft(transcript_record.transcript, captured_at)
        else:
            try:
                draft = summary_provider.create_card(transcript_record.transcript, {
                    "content_hash": job.content_hash,
                    "source_type": "audio",
                    "source_app": "voice_memos",
                    "captured_at": captured_at,
                })
            except Exception as exc:
                raise AudioSummaryError() from exc
        content = render_audio_card(job, archived.relative_to(paths.archive),
                                    transcript_record.transcript, captured_at, draft)
        _write_card_atomic(output.card, content, job.content_hash, paths.cards)
        store.mark_completed(job.content_hash, archived, output.card)
        return AudioProcessResult(job.content_hash, "completed", archived, output.card)
    except Exception as error:
        code = _audio_failure_code(error)
        # Codes are deliberately fixed vocabulary; no source name, child stderr,
        # transcript, or exception text enters SQLite.
        store.mark_failed(job.content_hash, code)
        return AudioProcessResult(job.content_hash, "failed", error_code=code)


class VoiceMemosIngestor:
    """Observe the source and submit stable recordings to the shared JobStore."""

    def __init__(self, source: Path, paths: PipelinePaths, state_path: Path,
                 settle_seconds: float = 60.0, transcribe_command: str | None = None,
                 transcribe_timeout: float = 7200.0, max_attempts: int = 3,
                 stale_after_seconds: float = 900.0,
                 summary_provider: AgentProvider | None = None):
        self.source = source.expanduser()
        self.paths = paths
        self.state_path = state_path.expanduser()
        self.settle_seconds = max(0.0, settle_seconds)
        self.transcribe_command = transcribe_command
        self.transcribe_timeout = transcribe_timeout
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if stale_after_seconds < 0:
            raise ValueError("stale_after_seconds must not be negative")
        self.max_attempts = max_attempts
        self.stale_after_seconds = stale_after_seconds
        self.summary_provider = summary_provider

    def baseline_existing(self) -> ScanReport:
        if self.state_path.exists():
            raise ConfigError("観測状態ファイルが既にあります。--baseline-existing は初回だけ指定できます。")
        report = ScanReport()
        state = _empty_state(baseline_cutover_mtime_ns=time.time_ns())
        for item in _iter_recordings(self.source):
            try:
                size, mtime_ns = _stat_signature(item)
            except OSError:
                continue
            relative = item.relative_to(self.source)
            state["items"][_key(relative, size, mtime_ns)] = {
                "source_rel": relative.as_posix(), "size": size, "mtime_ns": mtime_ns,
                "status": "baselined", "stable_scans": 2,
            }
            report.discovered += 1
        _save_state(self.state_path, state)
        return report

    def scan_once(self, now: float | None = None) -> ScanReport:
        state = _load_state(self.state_path)
        if state is None:
            raise ConfigError(
                "Voice Memos 初回設定です。過去の録音を取り込まない場合は "
                "--baseline-existing（推奨）、過去分も取り込む場合だけ --backfill-existing を明示してください。"
            )
        ensure_pipeline_directories(self.paths)
        store = JobStore(self.paths.state_db)
        # A killed long-running transcription cannot mark_failed. Return its
        # shared claim to pending before the audio-only claim below; MIME
        # filtering still prevents this adapter from claiming PDF work.
        store.requeue_stale_processing(
            stale_after=timedelta(seconds=self.stale_after_seconds)
        )
        report = ScanReport()
        observed_at = time.time() if now is None else now
        items: dict[str, Any] = state["items"]
        for source_file in _iter_recordings(self.source):
            try:
                size, mtime_ns = _stat_signature(source_file)
            except OSError:
                continue
            relative = source_file.relative_to(self.source)
            item_key = _key(relative, size, mtime_ns)
            report.discovered += 1
            item = items.get(item_key)
            if item is None:
                cutover = state.get("baseline_cutover_mtime_ns")
                status = "baselined" if isinstance(cutover, int) and mtime_ns <= cutover else "observing"
                item = {"source_rel": relative.as_posix(), "size": size, "mtime_ns": mtime_ns,
                        "status": status, "stable_scans": 2 if status == "baselined" else 1}
                items[item_key] = item
                if status == "baselined":
                    continue
                report.pending += 1
                continue
            if item.get("status") == "baselined":
                continue
            item["stable_scans"] = min(2, int(item.get("stable_scans", 0)) + 1)
            age = observed_at - (mtime_ns / 1_000_000_000)
            if item["stable_scans"] < 2 or age < self.settle_seconds:
                report.pending += 1
                continue
            try:
                before = _stat_signature(source_file)
                digest = sha256_file(source_file)
                after = _stat_signature(source_file)
            except OSError:
                report.pending += 1
                continue
            if before != after or before != (size, mtime_ns):
                item["stable_scans"] = 1
                report.pending += 1
                continue
            _, created = store.register(digest, source_file, original_name="voice-memo.m4a",
                                       size_bytes=size, media_type=AUDIO_MEDIA_TYPE)
            if created:
                report.registered += 1
        result = process_one_audio(self.paths, store, self.source,
                                   transcribe_command=self.transcribe_command,
                                   transcribe_timeout=self.transcribe_timeout,
                                   max_attempts=self.max_attempts,
                                   summary_provider=self.summary_provider)
        if result is not None:
            if result.status == "completed":
                report.archived += 1
                report.cards += 1
            else:
                report.retried += 1
                report.errors.append("Voice Memos 処理保留: {}".format(result.error_code))
        _save_state(self.state_path, state)
        return report


def _resolve_source(value: str | None) -> Path:
    source = Path(value or os.environ.get("KH_VOICE_MEMOS_PATH") or DEFAULT_SOURCE).expanduser()
    try:
        if not source.is_dir():
            raise ConfigError("Voice Memos のソースが見つからない: {}".format(source))
        with os.scandir(source):
            pass
    except PermissionError as exc:
        raise SourcePermissionError(str(exc))
    return source


def _summary_provider(command: str | None) -> AgentProvider | None:
    """Use the shared JSON-card contract only when audio summarization is opted in."""
    if not command:
        return None
    try:
        return CliAgentProvider(command=command)
    except (AgentCommandError, ValueError) as exc:
        raise ConfigError("Voice Memos 要約コマンドの設定が不正です: {}".format(exc))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Voice Memos を共有ArchiveとObsidian Cardsへ安全に取り込む")
    parser.add_argument("--once", action="store_true", help="1回だけ走査する")
    parser.add_argument("--watch", action="store_true", help="継続してポーリングする（既定）")
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--source")
    parser.add_argument("--archive")
    parser.add_argument("--vault")
    parser.add_argument("--cards")
    parser.add_argument("--state-db")
    parser.add_argument("--state", default=os.environ.get("KH_VOICE_MEMOS_STATE"))
    parser.add_argument("--settle-seconds", type=float, default=60.0)
    parser.add_argument("--transcribe-timeout", type=float,
                        default=os.environ.get("KH_AUDIO_TRANSCRIBE_TIMEOUT", "7200"))
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--stale-after-seconds", type=float, default=900.0)
    first_run = parser.add_mutually_exclusive_group()
    first_run.add_argument("--baseline-existing", action="store_true")
    first_run.add_argument("--backfill-existing", action="store_true")
    parser.add_argument("--transcribe-cmd", default=os.environ.get("KH_AUDIO_TRANSCRIBE_CMD"))
    parser.add_argument("--summary-cmd", default=os.environ.get("KH_AUDIO_SUMMARY_CMD"),
                        help="共有AgentProvider JSON契約の要約コマンド（未設定ならローカルfallback）")
    args = parser.parse_args(argv)
    try:
        source = _resolve_source(args.source)
        paths = resolve_pipeline_paths(args.vault, archive_value=args.archive,
                                       cards_value=args.cards, state_db_value=args.state_db)
        if args.transcribe_timeout <= 0:
            raise ConfigError("--transcribe-timeout は0より大きい秒数を指定してください。")
        if args.max_attempts <= 0:
            raise ConfigError("--max-attempts は1以上を指定してください。")
        if args.stale_after_seconds < 0:
            raise ConfigError("--stale-after-seconds は0以上を指定してください。")
        state_path = Path(args.state).expanduser() if args.state else DEFAULT_STATE
        summary_provider = _summary_provider(args.summary_cmd)
        ingestor = VoiceMemosIngestor(source, paths, state_path, args.settle_seconds,
                                      args.transcribe_cmd, args.transcribe_timeout,
                                      args.max_attempts, args.stale_after_seconds,
                                      summary_provider)
        if args.baseline_existing:
            report = ingestor.baseline_existing()
            print("既存の Voice Memos {} 件を基準化しました。過去分は取り込みません。".format(report.discovered))
            return 0
        if args.backfill_existing:
            if state_path.exists():
                raise ConfigError(
                    "既存の観測状態には --backfill-existing を使えません。"
                    "意図的に過去分を再投入する場合は、状態JSONを退避してから実行してください。"
                )
            _save_state(state_path, _empty_state())
            print("過去の Voice Memos の取り込みを開始します（安定確認後に処理）。", file=sys.stderr)
        while True:
            report = ingestor.scan_once()
            for error in report.errors:
                print(error, file=sys.stderr)
            if args.once:
                return 1 if report.errors else 0
            time.sleep(max(0.5, args.interval))
    except SourcePermissionError:
        print("Voice Memos を読めません。実行する Python/launchd にフルディスクアクセスを付与し、"
              "TCC の Voice Memos/iCloud 同期完了を確認してください。", file=sys.stderr)
        return 2
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
