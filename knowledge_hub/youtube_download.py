"""YouTubeリンク1本を受け取り、動画（または音声）をローカルへ保存する縦切り。

使い方（bot結合点はこの1コマンドのみ・`/find` と同じ契約）:
    python3 -m knowledge_hub.youtube_download "<URL>" [--out DIR] [--audio]
exit 0: stdoutがそのまま返信文 / exit 1: ダウンロード失敗 / exit 2: 設定・URLエラー

限定公開（unlisted）はリンクさえあれば通常どおり取得できる。非公開（private）や
年齢制限はログインが要るため `--cookies` / `--cookies-from-browser` を使う。

URLは受け取った文字列をそのまま渡さず、動画IDだけを抜き出して正規URLを組み直す。
これで `-` 始まりの文字列がyt-dlpのオプションとして解釈される経路を塞ぐ。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config
from .ingest_pdf import safe_stem

DEFAULT_TIMEOUT = 1800.0            # 1本あたり30分
DEFAULT_MAX_BYTES = 4 * 1024 * 1024 * 1024
DEFAULT_VIDEO_FORMAT = "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b"
DEFAULT_AUDIO_FORMAT = "ba[ext=m4a]/ba/b"
MAX_STDERR_BYTES = 8_192
# yt-dlpが書き込み途中に使う拡張子。完了ファイルの判定から除く。
PARTIAL_SUFFIXES = (".part", ".ytdl", ".temp", ".tmp")
ALLOWED_SUFFIX_RE = re.compile(r"^\.[A-Za-z0-9]{1,5}$")

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_ALLOWED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
    "youtu.be",
    "www.youtu.be",
}
_ID_PATH_PREFIXES = ("/shorts/", "/live/", "/embed/", "/v/")


class YoutubeDownloadError(RuntimeError):
    """利用者に見せて安全な、YouTube取得の失敗。"""


class InvalidUrlError(YoutubeDownloadError):
    """YouTubeの動画URLとして解釈できない。"""


class YtdlpMissingError(YoutubeDownloadError):
    """yt-dlp が見つからない。"""


class DownloadTimeoutError(YoutubeDownloadError):
    """yt-dlp が制限時間を超えた。"""


class DownloadFailedError(YoutubeDownloadError):
    """yt-dlp が異常終了した。分類済みコードと、目視用の末尾を持つ。

    ``detail`` はyt-dlpの出力そのままなので、CLIのstderrに出すだけに留め、
    JSONやカードなど後段が読む値には決して混ぜない。
    """

    def __init__(self, message: str, code: str, detail: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


class NoOutputError(YoutubeDownloadError):
    """yt-dlp は成功したのに保存ファイルが見つからない。"""


@dataclass(frozen=True)
class DownloadResult:
    """保存結果。``status`` は ``downloaded`` か ``exists``。"""

    video_id: str
    url: str
    path: Path
    size_bytes: int
    status: str
    audio_only: bool


def parse_video_id(raw_url: str) -> str:
    """YouTubeのURL・ID文字列から11文字の動画IDだけを取り出す。

    watch / youtu.be / shorts / live / embed / music / nocookie を受け付け、
    それ以外のホストやスキームは拒否する。
    """
    candidate = (raw_url or "").strip()
    if not candidate:
        raise InvalidUrlError("URLが空です")
    if _VIDEO_ID_RE.match(candidate):
        return candidate
    if "://" not in candidate:
        # 「youtu.be/xxxx」のような貼り付けを、ホスト判定できる形に整える。
        candidate = "https://" + candidate.lstrip("/")
    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https"):
        raise InvalidUrlError("http(s) のURLだけを受け付けます")
    host = (parsed.hostname or "").lower()
    if host not in _ALLOWED_HOSTS:
        raise InvalidUrlError(f"YouTubeのURLではありません: {host or '(ホスト不明)'}")
    path = parsed.path or ""
    video_id = ""
    if host in ("youtu.be", "www.youtu.be"):
        video_id = path.lstrip("/").split("/", 1)[0]
    elif path == "/watch":
        values = parse_qs(parsed.query).get("v", [])
        video_id = values[0] if values else ""
    else:
        for prefix in _ID_PATH_PREFIXES:
            if path.startswith(prefix):
                video_id = path[len(prefix):].split("/", 1)[0]
                break
    if not _VIDEO_ID_RE.match(video_id):
        raise InvalidUrlError("URLから動画IDを取り出せません")
    return video_id


def canonical_url(video_id: str) -> str:
    """検証済みIDから、こちらで組み立てた正規URLを返す。"""
    if not _VIDEO_ID_RE.match(video_id):
        raise InvalidUrlError("動画IDの形式が不正です")
    return f"https://www.youtube.com/watch?v={video_id}"


def _positive_float(value: float | None, name: str, default: float) -> float:
    if value is None:
        return default
    if value <= 0:
        raise YoutubeDownloadError(f"{name} は正の数で指定してください")
    return float(value)


def _positive_int(value: int | None, name: str, default: int) -> int:
    if value is None:
        return default
    if value <= 0:
        raise YoutubeDownloadError(f"{name} は正の整数で指定してください")
    return int(value)


DETAIL_TAIL_CHARS = 300


def _sanitized_text(raw: bytes) -> str:
    """yt-dlpのstderrから制御文字を落とす。分類はこの全文に対して行う。"""
    text = raw.decode("utf-8", errors="replace")
    return "".join(character if character.isprintable() else " " for character in text).strip()


def _classify_failure(return_code: int, stderr: str) -> tuple[str, str]:
    """終了コードとstderrから、安全な失敗コードと日本語の説明を決める。"""
    lowered = stderr.lower()
    rules = (
        ("private video", "private_video", "非公開の動画です（--cookies でログイン情報が要ります）"),
        ("sign in if you've been granted access", "private_video", "非公開の動画です（--cookies でログイン情報が要ります）"),
        ("members-only", "members_only", "メンバー限定の動画です"),
        ("sign in to confirm your age", "sign_in_required", "年齢確認が要る動画です（--cookies が要ります）"),
        ("confirm you're not a bot", "sign_in_required", "YouTubeがログインを要求しています（--cookies が要ります）"),
        ("age-restricted", "sign_in_required", "年齢制限のある動画です（--cookies が要ります）"),
        ("video unavailable", "video_unavailable", "動画が視聴できません（削除・地域制限など）"),
        ("has been removed", "video_unavailable", "動画が削除されています"),
        ("requested format", "format_unavailable", "指定した形式が見つかりません（--format を見直してください）"),
        ("max-filesize", "too_large", "上限サイズを超えています（--max-bytes を見直してください）"),
        ("unable to download webpage", "network_error", "YouTubeへ接続できません"),
        ("unable to download api page", "network_error", "YouTubeへ接続できません"),
        ("unable to connect to proxy", "network_error", "プロキシに接続できません"),
        ("urlopen error", "network_error", "YouTubeへ接続できません"),
        ("temporary failure in name resolution", "network_error", "名前解決に失敗しました"),
    )
    for needle, code, message in rules:
        if needle in lowered:
            return code, message
    return "download_failed", f"yt-dlpが失敗しました（終了コード {return_code}）"


def _run_bounded(argv: list[str], timeout: float) -> tuple[int, bytes]:
    """yt-dlpを1回だけ実行し、時間を区切る。stderr末尾だけを持ち帰る。

    子プロセスは新しいセッションで起動し、時間切れではプロセスグループごと落とす。
    ffmpeg等の孫プロセスを残したままにしないための措置。
    """
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise YtdlpMissingError("yt-dlp コマンドが見つかりません") from exc
    try:
        _, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(process)
        _, stderr = process.communicate()
        raise DownloadTimeoutError("yt-dlp が制限時間を超えました")
    finally:
        if process.poll() is None:
            _kill_process_group(process)
            process.wait()
    return process.returncode, bytes(stderr or b"")[-MAX_STDERR_BYTES:]


def _kill_process_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        process.kill()


def _existing_download(destination: Path, video_id: str) -> Path | None:
    """同じ動画IDの完了ファイルが既にあれば返す（貼り直しで再取得しない）。"""
    if not destination.is_dir():
        return None
    for candidate in sorted(destination.glob(f"*--{video_id}.*")):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        if candidate.suffix.lower() in PARTIAL_SUFFIXES:
            continue
        if candidate.stat().st_size > 0:
            return candidate
    return None


def _completed_files(work_dir: Path) -> list[Path]:
    files = [
        item
        for item in work_dir.iterdir()
        if item.is_file()
        and not item.is_symlink()
        and item.suffix.lower() not in PARTIAL_SUFFIXES
        and item.stat().st_size > 0
    ]
    return sorted(files, key=lambda item: item.stat().st_size, reverse=True)


def _final_name(produced: Path, video_id: str) -> str:
    """タイトル部分だけを無害化し、``<タイトル>--<動画ID>.<拡張子>`` に整える。"""
    stem = produced.stem
    marker = f"--{video_id}"
    if stem.endswith(marker):
        stem = stem[: -len(marker)]
    suffix = produced.suffix
    if not ALLOWED_SUFFIX_RE.match(suffix):
        suffix = ".bin"
    return f"{safe_stem(stem)}--{video_id}{suffix}"


def _ytdlp_argv(
    command: str | None,
    *,
    url: str,
    work_dir: Path,
    format_selector: str,
    audio: bool,
    max_bytes: int,
    cookies: Path | None,
    cookies_from_browser: str | None,
) -> list[str]:
    configured = command if command is not None else os.environ.get("KH_YTDLP_CMD", "yt-dlp")
    argv = shlex.split(configured)
    if not argv:
        raise YtdlpMissingError("yt-dlp コマンドの指定が空です")
    argv += [
        "--no-playlist",       # 再生リスト付きリンクでも1本だけ
        "--no-progress",
        "--no-color",
        "--retries", "3",
        "--fragment-retries", "3",
        "--max-filesize", str(max_bytes),
        "--paths", str(work_dir),
        "--output", "%(title).80B--%(id)s.%(ext)s",
        "--format", format_selector,
    ]
    if not audio:
        argv += ["--merge-output-format", "mp4"]
    if cookies is not None:
        argv += ["--cookies", str(cookies)]
    if cookies_from_browser:
        argv += ["--cookies-from-browser", cookies_from_browser]
    # 以降を必ずURLとして扱わせる。
    argv += ["--", url]
    return argv


def download_video(
    raw_url: str,
    destination: Path,
    *,
    audio: bool = False,
    format_selector: str | None = None,
    cookies: Path | None = None,
    cookies_from_browser: str | None = None,
    command: str | None = None,
    timeout: float | None = None,
    max_bytes: int | None = None,
) -> DownloadResult:
    """YouTubeリンク1本を ``destination`` 直下へ保存する。

    作業用の一時ディレクトリを保存先の中に作り、完了後に同一ディレクトリ内で
    ``os.replace`` して公開する。途中終了しても中途半端なファイルを残さない。
    """
    video_id = parse_video_id(raw_url)
    url = canonical_url(video_id)
    deadline = _positive_float(timeout, "timeout", DEFAULT_TIMEOUT)
    size_limit = _positive_int(max_bytes, "max_bytes", DEFAULT_MAX_BYTES)
    selector = format_selector or (DEFAULT_AUDIO_FORMAT if audio else DEFAULT_VIDEO_FORMAT)
    if cookies is not None:
        cookies = Path(cookies).expanduser()
        if not cookies.is_file():
            raise YoutubeDownloadError(f"cookiesファイルが見つかりません: {cookies}")

    destination = Path(destination).expanduser()
    destination.mkdir(parents=True, exist_ok=True)
    existing = _existing_download(destination, video_id)
    if existing is not None:
        return DownloadResult(
            video_id, url, existing, existing.stat().st_size, "exists", audio
        )

    work_dir = Path(tempfile.mkdtemp(prefix=".ytdl-", dir=destination))
    try:
        argv = _ytdlp_argv(
            command,
            url=url,
            work_dir=work_dir,
            format_selector=selector,
            audio=audio,
            max_bytes=size_limit,
            cookies=cookies,
            cookies_from_browser=cookies_from_browser,
        )
        return_code, stderr = _run_bounded(argv, deadline)
        diagnostics = _sanitized_text(stderr)
        tail = diagnostics[-DETAIL_TAIL_CHARS:]
        if return_code != 0:
            code, message = _classify_failure(return_code, diagnostics)
            raise DownloadFailedError(message, code, tail)
        produced = _completed_files(work_dir)
        if not produced:
            if "max-filesize" in diagnostics.lower():
                raise DownloadFailedError(
                    "上限サイズを超えたため取得を中止しました", "too_large", tail
                )
            raise NoOutputError("yt-dlpは成功しましたが保存ファイルがありません")
        source = produced[0]
        final = destination / _final_name(source, video_id)
        if final.exists() or final.is_symlink():
            # 併走した実行が先に公開した場合は、そちらを正とする。
            return DownloadResult(
                video_id, url, final, final.stat().st_size, "exists", audio
            )
        os.replace(source, final)
        return DownloadResult(
            video_id, url, final, final.stat().st_size, "downloaded", audio
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _human_size(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def _format_reply(result: DownloadResult) -> str:
    label = "保存しました" if result.status == "downloaded" else "すでに保存済み"
    kind = "音声" if result.audio_only else "動画"
    return f"{label}（{kind}）: {result.path} ({_human_size(result.size_bytes)})"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="knowledge_hub.youtube_download",
        description="YouTubeリンク1本をローカルへ保存する（限定公開リンク可）",
    )
    parser.add_argument("url", help="YouTubeのURL（watch / youtu.be / shorts / live / embed）")
    parser.add_argument("--out", help="保存先（既定: $KH_VIDEO_PATH → $KH_ARCHIVE_PATH/Video）")
    parser.add_argument("--audio", action="store_true", help="音声だけを取得する")
    parser.add_argument("--format", dest="format_selector", help="yt-dlpのフォーマット指定")
    parser.add_argument("--cookies", help="cookies.txt のパス（非公開・年齢制限用）")
    parser.add_argument("--cookies-from-browser", help="ブラウザ名（例: chrome, safari）")
    parser.add_argument("--timeout", type=float, help=f"秒（既定: {DEFAULT_TIMEOUT:.0f}）")
    parser.add_argument("--max-bytes", type=int, help=f"上限バイト（既定: {DEFAULT_MAX_BYTES}）")
    parser.add_argument("--json", action="store_true", help="機械可読なJSONを1行で出力")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        destination = config.resolve_video_dir(args.out)
    except config.ConfigError as error:
        print(str(error), file=sys.stderr)
        return 2

    try:
        result = download_video(
            args.url,
            destination,
            audio=args.audio,
            format_selector=args.format_selector,
            cookies=Path(args.cookies) if args.cookies else None,
            cookies_from_browser=args.cookies_from_browser,
            timeout=args.timeout,
            max_bytes=args.max_bytes,
        )
    except (InvalidUrlError, YtdlpMissingError) as error:
        # URLの取り違えとyt-dlp未導入はどちらも設定の問題なので、失敗と分けて返す。
        print(str(error), file=sys.stderr)
        return 2
    except (DownloadFailedError, DownloadTimeoutError, NoOutputError) as error:
        code = error.code if isinstance(error, DownloadFailedError) else _error_code(error)
        print(str(error), file=sys.stderr)
        if isinstance(error, DownloadFailedError) and error.detail:
            # yt-dlpの生の出力は人の目視用。stdoutの契約には入れない。
            print(f"yt-dlp: {error.detail}", file=sys.stderr)
        if args.json:
            print(json.dumps({"status": "failed", "error_code": code}, ensure_ascii=False))
        return 1
    except YoutubeDownloadError as error:
        print(str(error), file=sys.stderr)
        return 2
    except OSError as error:
        print(f"保存に失敗しました: {error.strerror or error}", file=sys.stderr)
        return 1

    if args.json:
        print(
            json.dumps(
                {
                    "status": result.status,
                    "video_id": result.video_id,
                    "url": result.url,
                    "path": str(result.path),
                    "bytes": result.size_bytes,
                    "audio_only": result.audio_only,
                },
                ensure_ascii=False,
            )
        )
    else:
        print(_format_reply(result))
    return 0


def _error_code(error: YoutubeDownloadError) -> str:
    """DownloadFailedError以外の失敗を、外に出して安全なコードへ写す。"""
    if isinstance(error, DownloadTimeoutError):
        return "download_timeout"
    if isinstance(error, NoOutputError):
        return "no_output"
    return "download_failed"


if __name__ == "__main__":
    sys.exit(main())
