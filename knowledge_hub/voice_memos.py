"""Apple Voice Memos を Archive と Obsidian Cards に取り込む安全なレーン。

Voice Memos の同期ディレクトリは読むだけで扱う。初回は明示的な
``--baseline-existing`` または ``--backfill-existing`` が必要で、意図せず
過去の録音を大量投入しないようにしている。
"""
from __future__ import annotations

import argparse
import errno
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
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import ConfigError, resolve_vault


DEFAULT_SOURCE = Path(
    "~/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings"
).expanduser()
DEFAULT_STATE = Path("~/.local/state/knowledge_hub/voice_memos.json").expanduser()
STATE_VERSION = 1


class SourcePermissionError(RuntimeError):
    """Voice Memos のTCC/Full Disk Accessにより読めない。"""


@dataclass
class ScanReport:
    discovered: int = 0
    pending: int = 0
    archived: int = 0
    cards: int = 0
    retried: int = 0
    errors: list[str] = field(default_factory=list)


def _stat_signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def _key(relative: Path, size: int, mtime_ns: int) -> str:
    return "{}\0{}\0{}".format(relative.as_posix(), size, mtime_ns)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as stream:
            state = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError("Voice Memos の状態ファイルを読めません: {}".format(exc))
    if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
        raise ConfigError("Voice Memos の状態ファイル形式が不正です: {}".format(path))
    if not isinstance(state.get("items"), dict):
        raise ConfigError("Voice Memos の状態ファイル形式が不正です: {}".format(path))
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
    """m4aだけを列挙し、os.walkの隠された権限エラーも利用者に返す。"""
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


def _yaml_scalar(value: str) -> str:
    """recall._frontmatter が読める単一行の、控えめなYAML scalar。"""
    return '"{}"'.format(value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " "))


def _excerpt(transcript: str, limit: int = 160) -> str:
    compact = " ".join(transcript.split())
    if not compact:
        return "文字起こし結果なし"
    return compact[:limit - 1] + "…" if len(compact) > limit else compact


def _atomic_copy(source: Path, destination: Path, expected_sha256: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if _sha256(destination) == expected_sha256:
            return
        raise RuntimeError("アーカイブ名の衝突を検出しました")
    fd, temporary = tempfile.mkstemp(prefix=".voice-memo-", suffix=".part", dir=str(destination.parent))
    try:
        with os.fdopen(fd, "wb") as target, source.open("rb") as origin:
            shutil.copyfileobj(origin, target, length=1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        if _sha256(Path(temporary)) != expected_sha256:
            raise RuntimeError("コピー中に録音の内容が変化しました。次回再試行します")
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if _sha256(destination) != expected_sha256:
                raise RuntimeError("アーカイブ名の衝突を検出しました")
        except OSError as exc:
            # 一部のクラウド File Provider は hard link を実装しない。通常の
            # launchd は単一プロセスなので、存在再確認の上で同じディレクトリ内の
            # atomic replace にフォールバックする。
            if exc.errno not in (errno.EOPNOTSUPP, errno.ENOTSUP, errno.EPERM):
                raise
            if destination.exists():
                if _sha256(destination) != expected_sha256:
                    raise RuntimeError("アーカイブ名の衝突を検出しました")
            else:
                os.replace(temporary, destination)
                temporary = ""
        else:
            os.unlink(temporary)
            temporary = ""
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _archive_path(archive: Path, source_stat_mtime_ns: int, sha256: str) -> Path:
    captured = datetime.fromtimestamp(source_stat_mtime_ns / 1_000_000_000)
    month = captured.strftime("%Y-%m")
    stem = captured.strftime("%m%d_%H%M_voice-memo_") + sha256[:10]
    return archive / month / (stem + ".m4a")


def _run_transcriber(command: str | None, audio: Path, timeout: float = 7200.0) -> str:
    if not command:
        raise RuntimeError("文字起こしコマンド未設定 (KH_AUDIO_TRANSCRIBE_CMD または --transcribe-cmd)")
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise RuntimeError("文字起こしコマンドを解釈できません: {}".format(exc))
    if not argv:
        raise RuntimeError("文字起こしコマンド未設定 (KH_AUDIO_TRANSCRIBE_CMD)")
    try:
        result = subprocess.run(argv + [str(audio)], text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        # timeout の例外文字列にはコマンドやパスが含まれ得るため載せない。
        raise RuntimeError("文字起こしコマンドが時間切れになりました。次回再試行します")
    except OSError as exc:
        raise RuntimeError("文字起こしコマンドを実行できません: {}".format(exc))
    if result.returncode != 0:
        # stderrは録音内容を含み得るので、状態・ログに載せない。
        raise RuntimeError("文字起こしコマンドが終了コード {} で失敗しました".format(result.returncode))
    return result.stdout.strip()


def _atomic_card(vault: Path, archived_rel: Path, sha256: str, source_mtime_ns: int,
                 transcript: str) -> Path:
    captured = datetime.fromtimestamp(source_mtime_ns / 1_000_000_000)
    date_part = captured.strftime("%Y-%m")
    filename = captured.strftime("%m%d_%H%M_voice-memo_") + sha256[:10] + ".md"
    destination = vault / "Cards" / "Audio" / date_part / filename
    title = "Voice Memo {}".format(captured.strftime("%Y-%m-%d %H:%M"))
    content = "\n".join((
        "---",
        "title: {}".format(_yaml_scalar(title)),
        "captured_at: {}".format(_yaml_scalar(captured.isoformat(timespec="seconds"))),
        "summary: {}".format(_yaml_scalar(_excerpt(transcript))),
        "source_type: audio",
        "source_app: voice_memos",
        "source_path: {}".format(_yaml_scalar(archived_rel.as_posix())),
        "source_sha256: {}".format(_yaml_scalar(sha256)),
        "---",
        "",
        "## Original",
        "",
        "`{}`".format(archived_rel.as_posix()),
        "",
        "## Transcript",
        "",
        transcript,
        "",
    ))
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        # short-id の理論上の衝突でも別カードを壊さない。
        existing = destination.read_text(encoding="utf-8", errors="replace")
        if "source_sha256: {}".format(_yaml_scalar(sha256)) in existing:
            return destination
        destination = destination.with_name(
            captured.strftime("%m%d_%H%M_voice-memo_") + sha256[:16] + ".md"
        )
        if destination.exists():
            existing = destination.read_text(encoding="utf-8", errors="replace")
            if "source_sha256: {}".format(_yaml_scalar(sha256)) in existing:
                return destination
            raise RuntimeError("カード名の衝突を検出しました")
    fd, temporary = tempfile.mkstemp(prefix=".voice-memo-", suffix=".md", dir=str(destination.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            pass
        except OSError as exc:
            if exc.errno not in (errno.EOPNOTSUPP, errno.ENOTSUP, errno.EPERM):
                raise
            if not destination.exists():
                os.replace(temporary, destination)
                temporary = ""
        else:
            os.unlink(temporary)
            temporary = ""
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
    return destination


class VoiceMemosIngestor:
    """状態を保持して、安定済みの録音だけを段階的に処理する。"""

    def __init__(self, source: Path, archive: Path, vault: Path, state_path: Path,
                 settle_seconds: float = 60.0, transcribe_command: str | None = None,
                 transcribe_timeout: float = 7200.0):
        self.source = source.expanduser()
        self.archive = archive.expanduser()
        self.vault = vault.expanduser()
        self.state_path = state_path.expanduser()
        self.settle_seconds = max(0.0, settle_seconds)
        self.transcribe_command = transcribe_command
        self.transcribe_timeout = transcribe_timeout

    def baseline_existing(self) -> ScanReport:
        if self.state_path.exists():
            raise ConfigError("状態ファイルが既にあります。--baseline-existing は初回だけ指定できます。")
        report = ScanReport()
        # 同期完了が遅れた旧録音も、この時点以前の更新日時なら基準化する。
        # これは防御層であり、初回iCloud同期を待つ運用は引き続き必須。
        state: dict[str, Any] = {
            "version": STATE_VERSION,
            "baseline_cutover_mtime_ns": time.time_ns(),
            "items": {},
        }
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
        report = ScanReport()
        now = time.time() if now is None else now
        items: dict[str, Any] = state["items"]
        # 文字起こしだけ失敗したものは、元の Voice Memo が後で変更・削除されても
        # Archive の確定コピーから再試行できる。
        for item in items.values():
            if item.get("status") == "archived" and not item.get("card_path"):
                self._complete_archived(item, report)
        for source_file in _iter_recordings(self.source):
            try:
                size, mtime_ns = _stat_signature(source_file)
            except (FileNotFoundError, PermissionError, OSError):
                continue
            relative = source_file.relative_to(self.source)
            item_key = _key(relative, size, mtime_ns)
            report.discovered += 1
            item = items.get(item_key)
            if item is None:
                cutover = state.get("baseline_cutover_mtime_ns")
                if isinstance(cutover, int) and mtime_ns <= cutover:
                    items[item_key] = {
                        "source_rel": relative.as_posix(), "size": size, "mtime_ns": mtime_ns,
                        "status": "baselined", "stable_scans": 2,
                    }
                    continue
                item = {"source_rel": relative.as_posix(), "size": size, "mtime_ns": mtime_ns,
                        "status": "pending", "stable_scans": 1}
                items[item_key] = item
                report.pending += 1
                continue
            if item.get("status") in ("baselined", "completed"):
                continue
            if item.get("status") == "pending":
                item["stable_scans"] = int(item.get("stable_scans", 0)) + 1
                age = now - (mtime_ns / 1_000_000_000)
                if item["stable_scans"] < 2 or age < self.settle_seconds:
                    report.pending += 1
                    continue
            self._process(source_file, item, report)
        _save_state(self.state_path, state)
        return report

    def _process(self, source_file: Path, item: dict[str, Any], report: ScanReport) -> None:
        try:
            # Hashの前後で同じstatであることを確認し、同期途中の内容を採用しない。
            before = _stat_signature(source_file)
            digest = item.get("sha256") or _sha256(source_file)
            after = _stat_signature(source_file)
            if before != after or before != (item["size"], item["mtime_ns"]):
                item["status"] = "pending"
                item["stable_scans"] = 1
                return
            if item.get("archive_rel"):
                archive_file = self.archive / item["archive_rel"]
            else:
                archive_file = _archive_path(self.archive, item["mtime_ns"], digest)
                if archive_file.exists() and _sha256(archive_file) != digest:
                    archive_file = archive_file.with_name(
                        archive_file.stem + "-" + digest[10:16] + archive_file.suffix
                    )
            _atomic_copy(source_file, archive_file, digest)
            if not item.get("archive_rel"):
                report.archived += 1
            item["sha256"] = digest
            item["archive_rel"] = archive_file.relative_to(self.archive).as_posix()
            item["status"] = "archived"
            self._complete_archived(item, report)
        except RuntimeError as exc:
            # RuntimeError はこのモジュールで要約した安全な状態だけを含む。
            report.errors.append("Voice Memos 処理保留: {}".format(exc))
        except OSError:
            # OSError 文字列には録音ファイル名が含まれ得るため出力しない。
            report.errors.append("Voice Memos 処理保留: ファイル操作に失敗しました。次回再試行します")

    def _complete_archived(self, item: dict[str, Any], report: ScanReport) -> None:
        """Archive 済みの1件を文字起こしし、カード作成だけを再試行する。"""
        if item.get("card_path"):
            item["status"] = "completed"
            return
        try:
            archive_file = self.archive / item["archive_rel"]
            if not archive_file.is_file():
                raise RuntimeError("Archive 済み原本が見つかりません")
            report.retried += 1
            transcript = _run_transcriber(self.transcribe_command, archive_file,
                                          self.transcribe_timeout)
            card = _atomic_card(self.vault, Path(item["archive_rel"]), item["sha256"],
                                item["mtime_ns"], transcript)
            item["card_path"] = str(card)
            item["status"] = "completed"
            report.cards += 1
        except RuntimeError as exc:
            report.errors.append("Voice Memos 処理保留: {}".format(exc))
        except OSError:
            report.errors.append("Voice Memos 処理保留: ファイル操作に失敗しました。次回再試行します")


def _resolve_source(value: str | None) -> Path:
    source = Path(value or os.environ.get("KH_VOICE_MEMOS_PATH") or DEFAULT_SOURCE).expanduser()
    try:
        if not source.is_dir():
            raise ConfigError("Voice Memos のソースが見つからない: {}".format(source))
        # is_dir() は権限拒否を False に畳むことがあるので、TCC を明示的に判定する。
        with os.scandir(source):
            pass
    except PermissionError as exc:
        raise SourcePermissionError(str(exc))
    return source


def _resolve_archive(value: str | None) -> Path:
    raw = value or os.environ.get("KH_ARCHIVE_PATH")
    if not raw:
        raise ConfigError("Archive を --archive または環境変数 KH_ARCHIVE_PATH で指定してください。")
    archive = Path(raw).expanduser()
    if archive.exists() and not archive.is_dir():
        raise ConfigError("Archive がディレクトリではありません: {}".format(archive))
    return archive


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Voice Memos を Archive と Obsidian Cards へ安全に取り込む")
    parser.add_argument("--once", action="store_true", help="1回だけ走査する")
    parser.add_argument("--watch", action="store_true", help="継続してポーリングする（既定）")
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--source")
    parser.add_argument("--archive")
    parser.add_argument("--vault")
    parser.add_argument("--state", default=os.environ.get("KH_VOICE_MEMOS_STATE"))
    parser.add_argument("--settle-seconds", type=float, default=60.0)
    parser.add_argument("--transcribe-timeout", type=float,
                        default=os.environ.get("KH_AUDIO_TRANSCRIBE_TIMEOUT", "7200"),
                        help="文字起こし1件の上限秒数（既定: 7200）")
    first_run = parser.add_mutually_exclusive_group()
    first_run.add_argument("--baseline-existing", action="store_true")
    first_run.add_argument("--backfill-existing", action="store_true")
    parser.add_argument("--transcribe-cmd", default=os.environ.get("KH_AUDIO_TRANSCRIBE_CMD"))
    args = parser.parse_args(argv)
    try:
        source = _resolve_source(args.source)
        archive = _resolve_archive(args.archive)
        vault = resolve_vault(args.vault)
        if args.transcribe_timeout <= 0:
            raise ConfigError("--transcribe-timeout は0より大きい秒数を指定してください。")
        state_path = Path(args.state).expanduser() if args.state else DEFAULT_STATE
        ingestor = VoiceMemosIngestor(source, archive, vault, state_path, args.settle_seconds,
                                      args.transcribe_cmd, args.transcribe_timeout)
        if args.baseline_existing:
            report = ingestor.baseline_existing()
            print("既存の Voice Memos {} 件を基準化しました。過去分は取り込みません。".format(report.discovered))
            return 0
        if args.backfill_existing and not state_path.exists():
            _save_state(state_path, {"version": STATE_VERSION, "items": {}})
            print("過去の Voice Memos の取り込みを開始します（安定確認後に処理）。", file=sys.stderr)
        while True:
            report = ingestor.scan_once()
            for error in report.errors:
                print(error, file=sys.stderr)
            if args.once:
                return 0
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
