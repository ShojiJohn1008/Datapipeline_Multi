"""Vault横断検索コア: ripgrep実行・集約・スコア・抜粋・iCloud検知・obsidianリンク。

インターフェースは凍結（docs/DESIGN.md §3）。変更時はDESIGN.mdとHANDOFF_GPT56.mdを同時更新。
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

# スコア係数（DESIGN.md §4。調整禁止）
RECENCY_HALF_LIFE_DAYS = 90.0
RECENCY_FLOOR = 0.25


class RipgrepError(RuntimeError):
    pass


@dataclass
class FileHits:
    """1ファイル分のヒット（相対パスをキーにした辞書の値として使う）。"""
    line_numbers: list[int] = field(default_factory=list)

    @property
    def hits(self) -> int:
        return len(self.line_numbers)


@dataclass
class FileResult:
    path: Path      # vaultからの相対パス
    hits: int
    mtime: float
    score: float
    snippet: str = ""


def _rg_bin() -> str:
    return os.environ.get("KH_RG_BIN", "rg")


def run_ripgrep(terms: list[str], vault: Path, timeout: float = 5.0) -> tuple[dict[str, FileHits], bool]:
    """OR・大文字小文字無視・.md限定・.obsidian除外で検索する。

    戻り値: (相対パス文字列→FileHits, タイムアウトしたか)。
    タイムアウト時は得られた部分stdoutをparseして返す（フェイルセーフ、DESIGN.md §5）。
    """
    cmd = [_rg_bin(), "-i", "-C", "2", "--json", "-g", "*.md", "-g", "!.obsidian/**"]
    for t in terms:
        cmd += ["-e", t]
    cmd.append(str(vault))

    timed_out = False
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        stdout = proc.stdout
        if proc.returncode not in (0, 1):  # 1 = マッチなし（正常）
            raise RipgrepError(f"ripgrep異常終了(code={proc.returncode}): {proc.stderr[:200]}")
    except FileNotFoundError as e:
        raise RipgrepError(
            "ripgrep(rg)が見つからない。`brew install ripgrep` するか KH_RG_BIN を設定。"
        ) from e
    except subprocess.TimeoutExpired as e:
        timed_out = True
        stdout = e.stdout if isinstance(e.stdout, str) else (e.stdout or b"").decode("utf-8", "replace")

    vault_resolved = vault.resolve()
    results: dict[str, FileHits] = {}
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue  # タイムアウト時の途切れ行など
        if ev.get("type") != "match":
            continue
        raw_path = Path(ev["data"]["path"]["text"])
        try:
            rel = str(raw_path.resolve().relative_to(vault_resolved))
        except (OSError, ValueError):
            rel = str(raw_path)
        results.setdefault(rel, FileHits()).line_numbers.append(ev["data"]["line_number"])
    return results, timed_out


def recency_weight(age_days: float) -> float:
    return RECENCY_FLOOR + (1.0 - RECENCY_FLOOR) * math.exp(
        -max(0.0, age_days) * math.log(2) / RECENCY_HALF_LIFE_DAYS
    )


def score_files(hits: dict[str, FileHits], vault: Path, now: float | None = None) -> list[FileResult]:
    """score降順、同点はmtimeの新しい順。"""
    now = time.time() if now is None else now
    out: list[FileResult] = []
    for rel, fh in hits.items():
        try:
            mtime = (vault / rel).stat().st_mtime
        except OSError:
            mtime = 0.0
        age_days = (now - mtime) / 86400.0
        out.append(FileResult(path=Path(rel), hits=fh.hits, mtime=mtime,
                              score=fh.hits * recency_weight(age_days)))
    out.sort(key=lambda r: (-r.score, -r.mtime))
    return out


def _merge_ranges(line_numbers: list[int], margin: int, total_lines: int) -> list[tuple[int, int]]:
    """ヒット行±marginを1始まり閉区間にし、重なり・隣接をマージする。"""
    ranges: list[tuple[int, int]] = []
    for n in sorted(set(line_numbers)):
        start, end = max(1, n - margin), min(total_lines, n + margin)
        if ranges and start <= ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
        else:
            ranges.append((start, end))
    return ranges


def extract_snippet(abs_path: Path, line_numbers: list[int],
                    margin: int = 10, max_chars: int = 2000) -> str:
    """該当箇所±margin行だけを読む（全文は読まない: トークン節約）。"""
    try:
        lines = abs_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    parts = []
    for start, end in _merge_ranges(line_numbers, margin, len(lines)):
        parts.append("\n".join(lines[start - 1:end]))
    snippet = "\n…\n".join(parts)
    if len(snippet) > max_chars:
        snippet = snippet[:max_chars] + "…"
    return snippet


def icloud_placeholders_exist(vault: Path) -> bool:
    """iCloudが実体を退避させた痕跡（.icloudプレースホルダ）があるか。"""
    return next(vault.rglob("*.icloud"), None) is not None


def try_brctl_download(vault: Path) -> bool:
    """brctlでVault全体のダウンロードを要求する（Mac以外・失敗時はFalse）。"""
    try:
        subprocess.run(["brctl", "download", str(vault)],
                       capture_output=True, timeout=10)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def obsidian_link(vault: Path, rel_path: Path) -> str:
    file_param = str(rel_path)
    if file_param.endswith(".md"):
        file_param = file_param[:-3]
    return (
        "obsidian://open?vault=" + urllib.parse.quote(vault.name, safe="")
        + "&file=" + urllib.parse.quote(file_param, safe="")
    )
