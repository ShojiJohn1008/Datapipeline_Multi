"""Claude呼び出し（語展開・回答生成）。プロンプト文言は凍結（DESIGN.md §6）。

既存botのAPI経路に乗せ替える場合は KH_CLAUDE_CMD を差し替えるだけでよい。
どちらの関数も失敗時は必ず縮退値を返し、例外を上げない（フェイルセーフ）。
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path

from .vault_search import FileResult


class LlmInvocationError(RuntimeError):
    """秘密情報やプロンプト本文を含めずにCLI失敗種別を伝える。"""


class LlmTimeoutError(LlmInvocationError):
    """LLM CLIが制限時間内に終了しなかった。"""


class LlmCommandNotFoundError(LlmInvocationError):
    """設定されたLLM CLIを起動できなかった。"""


class LlmCommandFailedError(LlmInvocationError):
    """LLM CLIが非ゼロ終了した。"""

# プロンプト（凍結）
EXPAND_PROMPT = """あなたは検索語展開器。個人のObsidian Vault（日本語・英語混在の.mdノート）を
ripgrepでOR検索するための類語・言い換え・関連表現を3〜5個生成せよ。
- 元クエリの表記ゆれ（カタカナ/英語/漢語）を優先
- 説明文・番号・コードブロックは出力しない
- 出力はJSON配列のみ。例: ["語1", "語2", "語3"]

クエリ: {query}"""

ANSWER_PROMPT = """あなたは個人ノート検索の回答生成器。以下のノート抜粋のみを根拠に、
質問へ2〜4文で直接答えよ。
- 抜粋に無いことは書かない。推測で補わない
- 質問と無関係な抜粋は無視してよい
- 出典リストは書かない（システム側で付与する）
- 抜粋から答えが出せない場合は「ノート内に直接の答えは見つからなかった」とだけ書く

質問: {query}

{excerpts}"""


def call_claude(prompt: str, timeout: float) -> str:
    """`claude -p`（または KH_CLAUDE_CMD）に prompt を渡し stdout を返す。失敗は例外。"""
    cmd = shlex.split(os.environ.get("KH_CLAUDE_CMD", "claude -p")) + [prompt]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise LlmTimeoutError("LLM CLI timed out") from exc
    except FileNotFoundError as exc:
        raise LlmCommandNotFoundError("LLM CLI command was not found") from exc
    if proc.returncode != 0:
        # stderrにはCLIがプロンプトを再掲する場合があるため、終了コードだけを残す。
        raise LlmCommandFailedError(f"LLM CLI exited with code {proc.returncode}")
    return proc.stdout.strip()


def expand_query(query: str, timeout: float = 10.0) -> list[str]:
    """クエリ→検索語リスト。先頭は必ず原クエリ。失敗時は原クエリのみ。最大6語。"""
    expanded: list[str] = []
    try:
        raw = call_claude(EXPAND_PROMPT.format(query=query), timeout)
        m = re.search(r"\[.*?\]", raw, re.S)
        if m:
            for t in json.loads(m.group(0)):
                if isinstance(t, str) and len(t.strip()) >= 2:
                    expanded.append(t.strip())
    except Exception:
        expanded = []
    terms = [query] + [t for t in expanded if t.lower() != query.lower()]
    seen: set[str] = set()
    uniq = [t for t in terms if not (t.lower() in seen or seen.add(t.lower()))]
    return uniq[:6]


def generate_answer(query: str, results: list[FileResult], timeout: float = 20.0) -> str | None:
    """上位ファイルの抜粋から回答2〜4文を生成。失敗時は None（縮退モード）。"""
    blocks = []
    for r in results:
        blocks.append(f"--- {r.path} ---\n{r.snippet}")
    try:
        answer = call_claude(
            ANSWER_PROMPT.format(query=query, excerpts="\n\n".join(blocks)), timeout
        )
        return answer or None
    except Exception:
        return None
