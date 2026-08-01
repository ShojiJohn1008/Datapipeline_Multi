"""Recall PWA向けの二段検索（高速一覧とオンデマンド要約）。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import config
from .find import normalize_query, relax_terms
from .llm import call_claude
from .vault_search import extract_snippet, obsidian_link, run_ripgrep, score_files

SUMMARY_PROMPT = """あなたは個人ノート検索の要約器。以下はユーザー自身の過去の記録です。
質問に対して、記録から分かることだけを日本語で簡潔にまとめてください。
- 各主張に対応する出典番号 [1] [2] を必ず付ける
- 「あなたの過去の記録によれば」という範囲を越えない
- 記録から答えられない一般知識の質問なら、次の一文だけを返す:
  これは記録でなく一般知識の質問です。OpenEvidence等の外部の医学情報源を確認してください。

質問: {query}

{excerpts}"""


def _frontmatter(path: Path) -> dict[str, str]:
    """依存ライブラリなしで、先頭frontmatterの単純なscalarだけを読む。"""
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            if stream.readline().strip() != "---":
                return {}
            values: dict[str, str] = {}
            for line in stream:
                line = line.rstrip("\n")
                if line.strip() == "---":
                    return values
                if ":" not in line or line[:1].isspace():
                    continue
                key, value = line.split(":", 1)
                value = value.strip().strip('"\'')
                if value:
                    values[key.strip().lower()] = value
    except OSError:
        return {}
    return {}


def _date(metadata: dict[str, str], mtime: float) -> str:
    for key in ("captured_at", "date", "created", "created_at"):
        if metadata.get(key):
            return metadata[key]
    return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d") if mtime else "日付不明"


def search_cards(query: str, vault: Path, limit: int = 10) -> dict[str, object]:
    """LLMを一切呼ばず、検索上位のfrontmatterだけをPWA用カードにする。"""
    base = normalize_query(query)
    terms = relax_terms([base])
    hits, timed_out = run_ripgrep(terms, vault, timeout=config.RG_TIMEOUT)
    ranked = score_files(hits, vault)[:limit]
    cards = []
    for result in ranked:
        metadata = _frontmatter(vault / result.path)
        cards.append({
            "id": str(result.path),
            "title": metadata.get("title", result.path.stem),
            "date": _date(metadata, result.mtime),
            "summary": metadata.get("summary", metadata.get("description", "")),
            "filepath": str(result.path),
            "link": obsidian_link(vault, result.path),
        })
    return {"query": query, "cards": cards, "total": len(hits), "partial": timed_out}


def _safe_selected(vault: Path, ids: list[str]) -> list[Path]:
    root = vault.resolve()
    selected: list[Path] = []
    for value in ids[:10]:
        candidate = (vault / value).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        if candidate.is_file() and candidate.suffix.lower() == ".md":
            selected.append(candidate)
    return selected


def summarize_cards(query: str, ids: list[str], vault: Path) -> dict[str, object]:
    """選択されたカードのヒット周辺だけをClaudeへ渡し、番号付き要約を返す。"""
    selected = _safe_selected(vault, ids)
    if not selected:
        return {"query": query, "answer": None, "error": "対象カードがありません"}

    terms = relax_terms([normalize_query(query)])
    hits, _ = run_ripgrep(terms, vault, timeout=config.RG_TIMEOUT)
    blocks = []
    sources = []
    for index, path in enumerate(selected, 1):
        rel = str(path.relative_to(vault.resolve()))
        line_numbers = hits[rel].line_numbers if rel in hits else [1]
        snippet = extract_snippet(path, line_numbers, margin=20, max_chars=4000)
        blocks.append(f"[{index}] {rel}\n{snippet}")
        sources.append({"id": rel, "title": _frontmatter(path).get("title", path.stem)})
    try:
        answer = call_claude(
            SUMMARY_PROMPT.format(query=query, excerpts="\n\n".join(blocks)),
            timeout=config.ANSWER_TIMEOUT,
        )
    except Exception:
        return {"query": query, "answer": None, "sources": sources, "error": "要約機能でエラー"}
    return {"query": query, "answer": answer, "sources": sources}


def handle_request(payload: dict[str, object], vault: Path) -> dict[str, object]:
    request_type = payload.get("type")
    query = payload.get("query")
    if not isinstance(query, str) or not query.strip():
        return {"error": "queryが必要です"}
    if request_type == "search":
        return search_cards(query, vault)
    if request_type == "summarize":
        ids = payload.get("ids", [])
        if not isinstance(ids, list) or not all(isinstance(value, str) for value in ids):
            return {"error": "idsは文字列の配列で指定してください"}
        return summarize_cards(query, ids, vault)
    return {"error": "typeはsearchまたはsummarizeを指定してください"}


def dumps_result(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
