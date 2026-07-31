"""/find パイプライン統括とCLI。

使い方（botからの結合点はこの1コマンドのみ・DESIGN.md §9）:
    python3 -m knowledge_hub.find "<クエリ>" [--vault PATH] [--no-llm]
exit 0: stdoutがそのままTelegram返信文 / exit 2: 設定エラー（stderr参照）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import config
from .llm import expand_query, generate_answer
from .vault_search import (
    FileResult,
    RipgrepError,
    extract_snippet,
    icloud_placeholders_exist,
    obsidian_link,
    run_ripgrep,
    score_files,
    try_brctl_download,
)

_SPLIT_RE = re.compile(r"[\s・/／\-－–]+")

# クエリ正規化: 口語の定型句を剥がして中身の語を残す（DESIGN.md §5）
_TAIL_PHRASES = [
    "について前に調べた", "について調べた", "前に調べた", "について", "に関して",
    "を調べた", "調べた", "って何だっけ", "って何", "とは何か", "とは",
    "だっけ", "教えて", "はある", "ってある", "探して", "の話",
]
_LEAD_PHRASES = ["探して", "前に調べた", "/find"]
_PUNCT = "？?！!。、,．.：: 　"


def normalize_query(query: str) -> str:
    """「腫瘍熱について前に調べた？」→「腫瘍熱」。剥がしすぎたら原文を返す。"""
    q = query.strip().strip(_PUNCT)
    for lead in _LEAD_PHRASES:
        if q.startswith(lead):
            q = q[len(lead):].strip().strip(_PUNCT)
    changed = True
    while changed:
        changed = False
        for tail in _TAIL_PHRASES:
            if q.endswith(tail) and len(q) - len(tail) >= 2:
                q = q[: -len(tail)].strip(_PUNCT)
                changed = True
    q = q.strip().strip(_PUNCT)
    return q if len(q) >= 2 else query.strip()


def _is_cjk(term: str) -> bool:
    return any("぀" <= c <= "ヿ" or "一" <= c <= "鿿" for c in term)


def relax_terms(terms: list[str]) -> list[str]:
    """0件時の緩和語生成（DESIGN.md §5）。元の語を維持した上位集合を返す。"""
    out = [term for term in terms if len(term) >= 2]

    def add(t: str) -> None:
        if len(t) >= 2 and t not in out:
            out.append(t)

    for t in terms:
        for part in _SPLIT_RE.split(t):
            part = part.strip()
            if not part:
                continue
            add(part)
            if _is_cjk(part) and len(part) >= 3:
                add(part[:-1])  # 例: 腫瘍熱→腫瘍
            elif part.isascii() and len(part) >= 6:
                add(part[: max(4, len(part) * 2 // 3)])  # 例: anchoring→anchor
    return out


def _format_sources(vault: Path, top: list[FileResult]) -> list[str]:
    lines = ["📚 出典:"]
    for i, r in enumerate(top, 1):
        date = datetime.fromtimestamp(r.mtime).strftime("%Y-%m-%d") if r.mtime else "日付不明"
        lines.append(f"{i}. {r.path.stem}（{date}）")
        lines.append(f"   {obsidian_link(vault, r.path)}")
    return lines


@dataclass
class _FindOutcome:
    query: str
    answer: str | None
    sources: list[FileResult]
    total_files: int
    notes: list[str]
    tried: list[str]


def _search(query: str, vault: Path, use_llm: bool) -> _FindOutcome:
    """検索を実行し、Telegram/JSONの両形式で共有する内部データを返す。"""
    t0 = time.monotonic()
    notes: list[str] = []

    # ①' クエリ正規化 → ② 語展開（LLM失敗時も正規化済みの語で検索は成立する）
    base = normalize_query(query)
    terms = expand_query(base, timeout=config.EXPAND_TIMEOUT) if use_llm else [base]

    # ③ 本検索（＋0件なら1回だけ緩和リトライ）
    hits, timed_out = run_ripgrep(terms, vault, timeout=config.RG_TIMEOUT)
    tried = terms
    if not hits:
        relaxed = relax_terms(terms)
        if len(relaxed) > len(terms):
            hits, timed_out2 = run_ripgrep(relaxed, vault, timeout=config.RG_TIMEOUT)
            timed_out = timed_out or timed_out2
            tried = relaxed
    if timed_out:
        notes.append("⚠️ 検索がタイムアウトしたため部分結果です")

    # 0件（iCloud退避の可能性を確認）
    if not hits:
        if icloud_placeholders_exist(vault):
            try_brctl_download(vault)
            notes.append("☁️ iCloud同期待ちの可能性あり（ダウンロード要求済み。数分後に再検索を）")
        return _FindOutcome(query, None, [], 0, notes, tried)

    # ④ 集約・スコア → 上位5ファイル
    scored = score_files(hits, vault)
    top = scored[: config.TOP_FILES]

    # ⑤ 該当箇所±10行のみ読む
    for r in top:
        r.snippet = extract_snippet(
            vault / r.path, hits[str(r.path)].line_numbers,
            margin=config.SNIPPET_MARGIN, max_chars=config.SNIPPET_MAX_CHARS,
        )

    # ⑥ 回答生成（残り予算が無ければ縮退）
    answer = None
    remaining = config.TOTAL_BUDGET - (time.monotonic() - t0)
    if use_llm and remaining > config.MIN_ANSWER_BUDGET:
        answer = generate_answer(query, top, timeout=min(config.ANSWER_TIMEOUT, remaining))

    return _FindOutcome(query, answer, top, len(scored), notes, tried)


def _format_reply(vault: Path, outcome: _FindOutcome) -> str:
    if not outcome.sources:
        reply = [f"見つからず。試した語：{' / '.join(outcome.tried)}"]
        return "\n".join(reply + outcome.notes)

    # ⑦ Telegram返信整形（DESIGN.md §7）
    query, answer, top = outcome.query, outcome.answer, outcome.sources
    lines = [f"🔎 「{query}」", ""]
    lines.append(answer if answer else "（要約なし：ヒット箇所のみ提示）")
    lines.append("")
    lines += _format_sources(vault, top)
    if outcome.total_files > len(top):
        lines.append(f"（他にも{outcome.total_files - len(top)}件ヒット）")
    lines += outcome.notes
    return "\n".join(lines)


def _format_json(vault: Path, outcome: _FindOutcome) -> str:
    sources = []
    for result in outcome.sources:
        date = datetime.fromtimestamp(result.mtime).strftime("%Y-%m-%d") if result.mtime else "日付不明"
        sources.append({
            "file": str(result.path),
            "date": date,
            "link": obsidian_link(vault, result.path),
            "hits": result.hits,
        })
    payload = {
        "query": outcome.query,
        "answer": outcome.answer,
        "sources": sources,
        "total_files": outcome.total_files,
        "notes": outcome.notes,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def run_find(query: str, vault: Path, use_llm: bool = True) -> str:
    """検索結果をTelegram返信文として返す（公開契約）。"""
    return _format_reply(vault, _search(query, vault, use_llm))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="knowledge_hub.find", description="Vault横断検索 (/find)")
    parser.add_argument("query", help="検索クエリ")
    parser.add_argument("--vault", help="Vaultパス（既定: $KH_VAULT_PATH → iCloud既定パス）")
    parser.add_argument("--no-llm", action="store_true", help="LLMを使わない（テスト・縮退確認用）")
    parser.add_argument("--json", action="store_true", help="機械可読なJSONを1行で出力")
    args = parser.parse_args(argv)

    try:
        vault = config.resolve_vault(args.vault)
    except config.ConfigError as e:
        print(str(e), file=sys.stderr)
        return 2

    try:
        outcome = _search(args.query, vault, use_llm=not args.no_llm)
        print(_format_json(vault, outcome) if args.json else _format_reply(vault, outcome))
        return 0
    except RipgrepError as e:
        print(str(e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
