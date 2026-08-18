"""ボイスメモ取り込み: Voice Memosコンテナから録音一覧を得る（B-01 取り込みステージ）。

iCloud同期でMacに届いた録音の実体(.m4a)とメタデータ(CloudRecordings.db)を読む。
DBはAppleの内部実装（半公式ルート）のため、スキーマが読めない場合は
フォルダスキャンへ必ず縮退する。コンテナへの書き込みは一切しない（読み取り専用）。
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

# Core DataのタイムスタンプはUnix epochでなく2001-01-01起点
_CORE_DATA_EPOCH = 978307200.0
_DB_NAME = "CloudRecordings.db"
_TITLE_COLUMNS = ("ZCUSTOMLABEL", "ZENCRYPTEDTITLE")  # OS版によりどちらか

# .qta は新しめのボイスメモが使う形式（中身はMPEG-4系。ffmpegは内容で判別するので読める）
AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".aac", ".qta"}


@dataclass
class Recording:
    source: Path        # 音声ファイル実体
    title: str | None   # ボイスメモ上のタイトル（取れなければNone）
    recorded_at: float  # 録音日時（Unix秒）


def _scan_folder(source_dir: Path) -> list[Recording]:
    """縮退経路: 音声ファイルを列挙。タイトルはファイル名・日時はmtimeから。"""
    recs = []
    for path in sorted(source_dir.iterdir()):
        if path.is_file() and path.suffix.lower() in AUDIO_EXTS:
            recs.append(Recording(path, path.stem, path.stat().st_mtime))
    return recs


def _read_db(source_dir: Path) -> list[Recording]:
    """CloudRecordings.dbからタイトル・録音日時を読む。失敗は例外（呼び元で縮退）。"""
    db = source_dir / _DB_NAME
    with tempfile.TemporaryDirectory() as tmp:
        # 本体がロック中・書き込み中でも安全なようWAL込みでコピーしてから開く
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(db) + suffix)
            if src.exists():
                shutil.copy2(src, Path(tmp) / src.name)
        conn = sqlite3.connect(Path(tmp) / _DB_NAME)
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(ZCLOUDRECORDING)")}
            if "ZPATH" not in cols or "ZDATE" not in cols:
                raise RuntimeError("既知のスキーマでない")
            title_col = next((c for c in _TITLE_COLUMNS if c in cols), None)
            recs = []
            query = f"SELECT ZPATH, ZDATE, {title_col or 'NULL'} FROM ZCLOUDRECORDING"
            for zpath, zdate, ztitle in conn.execute(query):
                if not zpath:
                    continue
                path = source_dir / Path(str(zpath)).name
                if not path.is_file():
                    continue  # 実体が未同期または削除済み
                recorded = float(zdate) + _CORE_DATA_EPOCH if zdate else path.stat().st_mtime
                title = str(ztitle).strip() if ztitle else ""
                recs.append(Recording(path, title or None, recorded))
            return recs
        finally:
            conn.close()


def discover_recordings(source_dir: Path) -> list[Recording]:
    """録音一覧。DBが読めればタイトル付き、読めなければフォルダスキャンに縮退。"""
    if (source_dir / _DB_NAME).exists():
        try:
            recs = _read_db(source_dir)
            if recs:
                return recs
        except Exception:
            pass
    return _scan_folder(source_dir)
