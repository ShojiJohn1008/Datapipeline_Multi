"""note記事取り込み（無料・購入済み有料）のユニットテスト。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from knowledge_hub.ingest_note import (
    NoteAuthRequiredError,
    NoteCardCollisionError,
    NoteCookieError,
    NoteUrlError,
    ensure_full_body,
    html_to_text,
    ingest_note_url,
    load_cookie_header,
    parse_note_key,
    render_note_card,
    write_note_card,
)

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


def sample_data(**overrides):
    data = {
        "name": "腫瘍熱の見分け方",
        "body": "<p>導入です。</p><h2>本論</h2><p>ナイキサンテスト。</p>",
        "price": 500,
        "is_limited": False,
        "can_read": True,
        "publish_at": "2026-08-01T00:00:00+09:00",
        "user": {"nickname": "Shoji", "urlname": "shoji"},
    }
    data.update(overrides)
    return data


class ParseNoteKeyTest(unittest.TestCase):
    def test_accepts_standard_article_url(self):
        self.assertEqual(
            parse_note_key("https://note.com/shoji/n/n1234abcd5678"), "n1234abcd5678"
        )

    def test_rejects_non_note_host_and_bad_paths(self):
        for url in (
            "https://evil.com/shoji/n/n1234abcd5678",
            "https://note.com.evil.com/x/n/n1234abcd5678",
            "https://note.com/shoji",
            "ftp://note.com/shoji/n/n1234abcd5678",
        ):
            with self.assertRaises(NoteUrlError):
                parse_note_key(url)


class CookieHeaderTest(unittest.TestCase):
    def test_reads_json_and_rejects_header_injection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cookies.json"
            path.write_text('{"note_gql_auth_token":"abc123"}', encoding="utf-8")
            os.chmod(path, 0o600)
            self.assertEqual(load_cookie_header(path), "note_gql_auth_token=abc123")
            path.write_text('{"a":"x\\r\\nInjected: y"}', encoding="utf-8")
            os.chmod(path, 0o600)
            with self.assertRaises(NoteCookieError):
                load_cookie_header(path)

    def test_rejects_world_readable_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cookies.json"
            path.write_text('{"a":"b"}', encoding="utf-8")
            os.chmod(path, 0o644)
            with self.assertRaises(NoteCookieError):
                load_cookie_header(path)

    def test_explicit_missing_file_is_an_error(self):
        with self.assertRaises(NoteCookieError):
            load_cookie_header(Path("/nonexistent/cookies.json"))


class HtmlToTextTest(unittest.TestCase):
    def test_blocks_headings_lists_and_script_removal(self):
        text = html_to_text(
            "<h2>見出し</h2><p>段落1<br>続き</p><ul><li>甲</li><li>乙</li></ul>"
            "<script>alert(1)</script>"
        )
        self.assertIn("## 見出し", text)
        self.assertIn("段落1\n続き", text)
        self.assertIn("- 甲", text)
        self.assertNotIn("alert", text)


class EnsureFullBodyTest(unittest.TestCase):
    def test_paid_and_readable_returns_text(self):
        self.assertIn("ナイキサンテスト", ensure_full_body(sample_data(), had_cookie=True))

    def test_paid_but_limited_raises_auth_error(self):
        for overrides in ({"is_limited": True}, {"can_read": False}):
            with self.assertRaises(NoteAuthRequiredError):
                ensure_full_body(sample_data(**overrides), had_cookie=False)

    def test_free_limited_flags_do_not_block(self):
        data = sample_data(price=0, is_limited=True)
        self.assertIn("導入です", ensure_full_body(data, had_cookie=False))


class CardWriteTest(unittest.TestCase):
    def test_render_and_write_then_idempotent_retry(self):
        data = sample_data()
        body = ensure_full_body(data, had_cookie=True)
        content = render_note_card(
            "n1234abcd5678", "https://note.com/shoji/n/n1234abcd5678", data, body, NOW
        )
        self.assertIn('source_type: "note"', content)
        self.assertIn('paid: true', content)
        self.assertIn("## 本文", content)
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            relative, created = write_note_card(vault, "n1234abcd5678", data["name"], content, NOW)
            self.assertTrue(created)
            self.assertTrue((vault / relative).is_file())
            relative2, created2 = write_note_card(vault, "n1234abcd5678", data["name"], content, NOW)
            self.assertEqual(relative, relative2)
            self.assertFalse(created2)
            # 同じパスを別記事のカードが占有していたら止める
            occupied = (vault / relative)
            occupied.write_text(
                occupied.read_text(encoding="utf-8").replace(
                    '"n1234abcd5678"', '"n9999beef0000"', 1
                ),
                encoding="utf-8",
            )
            with self.assertRaises(NoteCardCollisionError):
                write_note_card(vault, "n1234abcd5678", data["name"], content, NOW)


class IngestEndToEndTest(unittest.TestCase):
    def test_ingest_with_fake_fetcher(self):
        def fake_fetch(key, cookie_header, *, timeout):
            self.assertEqual(key, "n1234abcd5678")
            return sample_data()

        with tempfile.TemporaryDirectory() as tmp:
            result = ingest_note_url(
                "https://note.com/shoji/n/n1234abcd5678",
                Path(tmp),
                cookie_file=None,
                fetcher=fake_fetch,
                now=NOW,
            )
            self.assertEqual(result["status"], "completed")
            self.assertTrue(result["created"])
            card = Path(tmp) / result["card_path"]
            text = card.read_text(encoding="utf-8")
            self.assertIn("ナイキサンテスト", text)
            self.assertIn("note.com/shoji/n/n1234abcd5678", text)


if __name__ == "__main__":
    unittest.main()
