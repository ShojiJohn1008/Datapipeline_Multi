"""Intent-scoped Recall cards written into the local Obsidian Vault."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unicodedata
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from .vault_search import obsidian_link


class CaptureError(RuntimeError):
    """A capture payload is invalid or cannot be published safely."""


def _text(value: object, field: str, limit: int, *, required: bool = False) -> str:
    if not isinstance(value, str):
        if required:
            raise CaptureError(f"{field}が必要です")
        return ""
    cleaned = value.replace("\x00", "").strip()
    if required and not cleaned:
        raise CaptureError(f"{field}が必要です")
    return cleaned[:limit]


def _strings(value: object, field: str, *, limit: int = 20) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise CaptureError(f"{field}は文字列の配列で指定してください")
    result: list[str] = []
    for item in value[:limit]:
        cleaned = item.replace("\x00", "").strip()[:100]
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return result


def _safe_url(value: object) -> str:
    url = _text(value, "source_url", 2048)
    if not url:
        return ""
    if any(ord(character) < 0x20 for character in url):
        raise CaptureError("source_urlに制御文字は使用できません")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CaptureError("source_urlはhttpまたはhttpsで指定してください")
    return url


def _captured_at(value: object) -> datetime:
    raw = _text(value, "captured_at", 40)
    if not raw:
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CaptureError("captured_atが正しくありません") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _safe_stem(value: str, max_length: int = 72) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    characters = [c if c.isalnum() or c in " -_()" else "-" for c in normalized]
    stem = "".join(characters).strip(" -_")
    while "--" in stem:
        stem = stem.replace("--", "-")
    while "  " in stem:
        stem = stem.replace("  ", " ")
    return stem[:max_length].rstrip(" -_") or "recall-card"


def _yaml(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _markdown_inline(value: str) -> str:
    compact = " ".join(value.splitlines())
    for character in "\\`*_{}[]<>#":
        compact = compact.replace(character, "\\" + character)
    return compact


def _markdown_text(value: str) -> str:
    return "\n".join(_markdown_inline(line) if line.strip() else "" for line in value.splitlines())


def _existing_external_id(path: Path) -> str | None:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        return None
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            for index, line in enumerate(stream):
                if index > 40 or (index > 0 and line.strip() == "---"):
                    break
                if line.startswith("external_id:"):
                    value = json.loads(line.split(":", 1)[1].strip())
                    return value if isinstance(value, str) else None
    except (OSError, json.JSONDecodeError):
        return None
    return None


def capture_intent_card(card: object, vault: Path) -> dict[str, object]:
    """Write one intent-scoped Q&A card and preserve same-id user edits on retry."""
    if not isinstance(card, dict):
        raise CaptureError("cardが必要です")
    external_id = _text(card.get("id"), "card.id", 200, required=True)
    question = _text(card.get("question"), "question", 500, required=True)
    answer = _text(card.get("answer"), "answer", 12000, required=True)
    intent = _text(card.get("intent"), "intent", 1000, required=True)
    category = _text(card.get("category"), "category", 100) or "メモ"
    tags = _strings(card.get("tags"), "tags")
    aliases = _strings(card.get("aliases"), "aliases")
    source_name = _text(card.get("source_name"), "source_name", 300)
    source_url = _safe_url(card.get("source_url"))
    source_type = _text(card.get("source_type"), "source_type", 30) or "web"
    captured = _captured_at(card.get("captured_at"))

    vault_root = vault.resolve(strict=True)
    cards_root = (vault_root / "Cards").resolve(strict=False)
    try:
        cards_root.relative_to(vault_root)
    except ValueError as exc:
        raise CaptureError("Cardsの保存先がVault外です") from exc
    cards_root.mkdir(parents=True, exist_ok=True)

    digest = hashlib.sha256(external_id.encode("utf-8")).hexdigest()
    relative = Path("Cards") / f"{captured.year:04d}" / f"{captured.month:02d}" / (
        f"{_safe_stem(question)}--{digest[:8]}.md"
    )
    destination = (vault_root / relative).resolve(strict=False)
    try:
        destination.relative_to(cards_root)
    except ValueError as exc:
        raise CaptureError("カードの保存先がCards外です") from exc

    if destination.exists() or destination.is_symlink():
        if _existing_external_id(destination) != external_id:
            raise CaptureError("同名の別カードが既に存在します")
        return {
            "created": False,
            "id": external_id,
            "filepath": str(relative),
            "link": obsidian_link(vault_root, relative),
        }

    frontmatter = {
        "id": "recall-" + digest,
        "external_id": external_id,
        "title": question,
        "question": question,
        "summary": answer[:500],
        "intent": intent,
        "category": category,
        "tags": tags,
        "aliases": aliases,
        "source_type": source_type,
        "source_name": source_name,
        "source_url": source_url,
        "captured_at": captured.isoformat(),
    }
    lines = ["---"]
    lines.extend(f"{key}: {_yaml(value)}" for key, value in frontmatter.items())
    lines.extend([
        "---",
        "",
        "# " + _markdown_inline(question),
        "",
        "## Why I saved this",
        "",
        _markdown_text(intent),
        "",
        "## Answer",
        "",
        _markdown_text(answer),
        "",
        "## Source",
        "",
    ])
    if source_name:
        lines.append("Source: " + _markdown_inline(source_name))
    if source_url:
        lines.append("URL: <" + source_url.replace(">", "%3E") + ">")
    lines.append("")
    content = "\n".join(lines)

    destination.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=".capture-",
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
        if destination.exists() or destination.is_symlink():
            if _existing_external_id(destination) != external_id:
                raise CaptureError("カードの保存先が競合しました")
        else:
            os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)

    return {
        "created": True,
        "id": external_id,
        "filepath": str(relative),
        "link": obsidian_link(vault_root, relative),
    }
