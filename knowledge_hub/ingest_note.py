"""note.com記事（無料・購入済み有料）をVaultのMarkdownカードへ文字おこしする。

認証はブラウザからコピーしたセッションCookieで行う。ID・パスワードは扱わず、
Cookieの実値はGit管理外のローカルファイル（既定:
``~/.config/knowledge_hub/note_cookies.json``）にのみ置く。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

from .config import ConfigError, resolve_vault

DEFAULT_COOKIE_FILE = Path("~/.config/knowledge_hub/note_cookies.json").expanduser()
NOTE_API_TEMPLATE = "https://note.com/api/v3/notes/{key}"
HTTP_TIMEOUT = 30.0
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_BODY_CHARS = 200_000
USER_AGENT = "Mozilla/5.0 (Macintosh) knowledge_hub-note-ingest"

# CookieヘッダーはCR/LFを含むとヘッダーインジェクションになるため厳格に検証する
_COOKIE_TOKEN = re.compile(r"^[!#-+\--:<-\[\]-~]+$")


class NoteIngestError(RuntimeError):
    """note取り込みの決定的な失敗。CLIは分類コード付きで報告する。"""

    code = "note_ingest_failed"


class NoteUrlError(NoteIngestError):
    code = "invalid_url"


class NoteCookieError(NoteIngestError):
    code = "cookie_error"


class NoteFetchError(NoteIngestError):
    code = "fetch_failed"


class NoteAuthRequiredError(NoteIngestError):
    """有料部分が返ってこない＝未ログインかCookie期限切れか未購入。"""

    code = "auth_required"


class NoteCardCollisionError(NoteIngestError):
    code = "output_collision"


def parse_note_key(url: str) -> str:
    """記事URLからnote key（``n``で始まるID）を取り出す。"""
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise NoteUrlError("URLはhttpsのnote記事URLを指定してください")
    host = parsed.netloc.lower().split(":", 1)[0]
    if host != "note.com" and not host.endswith(".note.com"):
        raise NoteUrlError(f"note.comのURLではありません: {parsed.netloc}")
    match = re.search(r"/n/(n[0-9a-f]{8,})/?$", parsed.path)
    if not match:
        raise NoteUrlError(
            "記事URLの形式が想定外です（例: https://note.com/<user>/n/nXXXXXXXXXXXX）"
        )
    return match.group(1)


def load_cookie_header(path: Path | None) -> str:
    """JSONの ``{name: value}`` からCookieヘッダー文字列を組み立てる。

    ファイル未指定かつ既定ファイル不在は「Cookieなし」（無料記事のみ可）。
    """
    cookie_file = path or DEFAULT_COOKIE_FILE
    if not cookie_file.exists():
        if path is not None:
            raise NoteCookieError(f"Cookieファイルが見つからない: {cookie_file}")
        return ""
    if cookie_file.is_symlink() or not cookie_file.is_file():
        raise NoteCookieError(f"Cookieファイルが通常ファイルではない: {cookie_file}")
    mode = stat.S_IMODE(cookie_file.stat().st_mode)
    if mode & 0o077:
        raise NoteCookieError(
            f"Cookieファイルの権限が広すぎます（chmod 600 を推奨）: {cookie_file}"
        )
    try:
        data = json.loads(cookie_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NoteCookieError("Cookieファイルを読めません（JSONの{名前:値}形式）") from exc
    if not isinstance(data, dict) or not data:
        raise NoteCookieError("Cookieファイルは空でない{名前:値}のJSONにしてください")
    pairs: list[str] = []
    for name, value in data.items():
        if not isinstance(value, str):
            raise NoteCookieError(f"Cookie値は文字列で指定してください: {name}")
        if not _COOKIE_TOKEN.match(name) or not all(
            0x20 < ord(c) < 0x7F and c != ";" for c in value
        ):
            raise NoteCookieError(f"Cookieに使えない文字が含まれています: {name}")
        pairs.append(f"{name}={value}")
    return "; ".join(pairs)


_BLOCK_TAGS = {"p", "div", "section", "article", "figure", "table", "tr", "ul", "ol"}
_SKIP_TAGS = {"script", "style", "noscript"}


class _HtmlToText(HTMLParser):
    """note本文HTMLを読みやすいMarkdown風テキストへ落とす（標準ライブラリのみ）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0
        self._pending_prefix = ""

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "br":
            self._chunks.append("\n")
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._chunks.append("\n\n")
            self._pending_prefix = "#" * int(tag[1]) + " "
        elif tag == "li":
            self._chunks.append("\n")
            self._pending_prefix = "- "
        elif tag == "blockquote":
            self._chunks.append("\n\n")
            self._pending_prefix = "> "
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK_TAGS or tag in {"h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote"}:
            self._chunks.append("\n")
            self._pending_prefix = ""

    def handle_data(self, data: str) -> None:
        if self._skip_depth or not data.strip():
            return
        if self._pending_prefix:
            self._chunks.append(self._pending_prefix)
            self._pending_prefix = ""
        self._chunks.append(data)


def html_to_text(html: str) -> str:
    parser = _HtmlToText()
    parser.feed(html)
    parser.close()
    text = "".join(parser._chunks)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def fetch_note(
    key: str,
    cookie_header: str,
    *,
    timeout: float = HTTP_TIMEOUT,
    opener: urllib.request.OpenerDirector | None = None,
) -> dict:
    """note APIから記事メタと本文HTMLを取得して検証済みdictを返す。"""
    request = urllib.request.Request(
        NOTE_API_TEMPLATE.format(key=urllib.parse.quote(key, safe="")),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    if cookie_header:
        request.add_header("Cookie", cookie_header)
    open_func = opener.open if opener is not None else urllib.request.urlopen
    try:
        with open_func(request, timeout=timeout) as response:  # type: ignore[arg-type]
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise NoteAuthRequiredError(
                "noteが認証を拒否しました。Cookieを更新してください"
            ) from exc
        if exc.code == 404:
            raise NoteFetchError("記事が見つかりません（削除済みか非公開）") from exc
        raise NoteFetchError(f"note APIがHTTP {exc.code}を返しました") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise NoteFetchError(f"note APIへ接続できません: {exc}") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise NoteFetchError("note APIの応答が大きすぎます")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NoteFetchError("note APIの応答を解釈できません") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise NoteFetchError("note APIの応答形式が想定外です")
    return data


def _require_str(data: dict, field: str) -> str:
    value = data.get(field)
    return value.strip() if isinstance(value, str) else ""


def ensure_full_body(data: dict, *, had_cookie: bool) -> str:
    """有料記事の本文が全文か検証し、Markdown風テキストを返す。"""
    body_html = _require_str(data, "body")
    price = data.get("price")
    is_paid = isinstance(price, (int, float)) and price > 0
    # noteは未購入・未ログイン時、is_limited=true か can_read=false の途中までの
    # 本文を返す。全文が取れていないまま保存すると気づけないため明示的に止める。
    limited = data.get("is_limited") is True or data.get("can_read") is False
    if is_paid and limited:
        hint = (
            "Cookieの期限切れの可能性があります。ブラウザで再ログインしてCookieを更新してください"
            if had_cookie
            else "Cookieファイルが未設定です（既定: ~/.config/knowledge_hub/note_cookies.json）"
        )
        raise NoteAuthRequiredError(f"有料部分を取得できませんでした。{hint}")
    if not body_html:
        raise NoteFetchError("本文が空でした")
    text = html_to_text(body_html)
    if not text:
        raise NoteFetchError("本文の抽出結果が空でした")
    return text[:MAX_BODY_CHARS]


def _yaml_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _markdown_inline(value: str) -> str:
    compact = " ".join(value.splitlines())
    for character in "\\`*_{}[]<>#":
        compact = compact.replace(character, "\\" + character)
    return compact


def _safe_stem(value: str, max_length: int = 72) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    characters = [c if c.isalnum() or c in " -_()" else "-" for c in normalized]
    stem = "".join(characters).strip(" -_")
    while "--" in stem:
        stem = stem.replace("--", "-")
    while "  " in stem:
        stem = stem.replace("  ", " ")
    return stem[:max_length].rstrip(" -_") or "note-article"


def render_note_card(key: str, url: str, data: dict, body_text: str, captured_at: datetime) -> str:
    title = _require_str(data, "name") or key
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    author = _require_str(user, "nickname") or _require_str(user, "urlname")
    price = data.get("price")
    frontmatter = {
        "id": "note-" + hashlib.sha256(key.encode("utf-8")).hexdigest(),
        "note_key": key,
        "title": title,
        "source_type": "note",
        "source_url": url,
        "author": author,
        "paid": bool(isinstance(price, (int, float)) and price > 0),
        "published_at": _require_str(data, "publish_at") or _require_str(data, "created_at"),
        "captured_at": captured_at.isoformat(),
    }
    lines = ["---"]
    lines.extend(f"{k}: {_yaml_value(v)}" for k, v in frontmatter.items())
    lines.extend(
        [
            "---",
            "",
            "# " + _markdown_inline(title),
            "",
            "## Source",
            "",
            f"URL: <{url.replace('>', '%3E')}>",
        ]
    )
    if author:
        lines.append("Author: " + _markdown_inline(author))
    lines.extend(["", "## 本文", "", body_text, ""])
    return "\n".join(lines)


def _existing_note_key(path: Path) -> str | None:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        return None
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            for index, line in enumerate(stream):
                if index > 40 or (index > 0 and line.strip() == "---"):
                    break
                if line.startswith("note_key:"):
                    value = json.loads(line.split(":", 1)[1].strip())
                    return value if isinstance(value, str) else None
    except (OSError, json.JSONDecodeError):
        return None
    return None


def write_note_card(
    vault: Path, key: str, title: str, content: str, captured_at: datetime
) -> tuple[Path, bool]:
    """カードをVault/Cards配下へアトミックに書き、(相対パス, 新規作成か)を返す。"""
    vault_root = vault.resolve(strict=True)
    cards_root = (vault_root / "Cards").resolve(strict=False)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    relative = Path("Cards") / f"{captured_at.year:04d}" / f"{captured_at.month:02d}" / (
        f"{_safe_stem(title)}--{digest[:8]}.md"
    )
    destination = (vault_root / relative).resolve(strict=False)
    try:
        destination.relative_to(cards_root)
    except ValueError as exc:
        raise NoteCardCollisionError("カードの保存先がCards外です") from exc

    if destination.exists() or destination.is_symlink():
        if _existing_note_key(destination) != key:
            raise NoteCardCollisionError("同名の別カードが既に存在します")
        # 再実行時、ユーザー編集済みの同一記事カードは上書きしない
        return relative, False

    destination.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=".note-",
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
            if _existing_note_key(destination) != key:
                raise NoteCardCollisionError("カードの保存先が競合しました")
        else:
            os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return relative, True


def ingest_note_url(
    url: str,
    vault: Path,
    *,
    cookie_file: Path | None = None,
    timeout: float = HTTP_TIMEOUT,
    fetcher=fetch_note,
    now: datetime | None = None,
) -> dict:
    key = parse_note_key(url)
    cookie_header = load_cookie_header(cookie_file)
    data = fetcher(key, cookie_header, timeout=timeout)
    body_text = ensure_full_body(data, had_cookie=bool(cookie_header))
    captured_at = now or datetime.now(timezone.utc)
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    canonical_url = (
        f"https://note.com/{urllib.parse.quote(_require_str(user, 'urlname') or 'notes')}/n/{key}"
    )
    title = _require_str(data, "name") or key
    content = render_note_card(key, canonical_url, data, body_text, captured_at)
    relative, created = write_note_card(vault, key, title, content, captured_at)
    return {
        "status": "completed",
        "created": created,
        "note_key": key,
        "title": title,
        "card_path": str(relative),
        "char_count": len(body_text),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="note記事（無料・購入済み有料）をVaultカードへ文字おこしする"
    )
    parser.add_argument("url", help="note記事URL（例: https://note.com/<user>/n/nXXXX...）")
    parser.add_argument("--vault")
    parser.add_argument(
        "--cookie-file",
        help="ログインCookieのJSONファイル（既定: $KH_NOTE_COOKIE_FILE か "
        "~/.config/knowledge_hub/note_cookies.json）",
    )
    parser.add_argument("--timeout", type=float, default=HTTP_TIMEOUT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cookie_value = args.cookie_file or os.environ.get("KH_NOTE_COOKIE_FILE")
    try:
        vault = resolve_vault(args.vault)
        result = ingest_note_url(
            args.url,
            vault,
            cookie_file=Path(cookie_value).expanduser() if cookie_value else None,
            timeout=args.timeout,
        )
    except ConfigError as error:
        print(str(error))
        return 2
    except NoteIngestError as error:
        print(json.dumps({"status": "failed", "error_code": error.code, "message": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
