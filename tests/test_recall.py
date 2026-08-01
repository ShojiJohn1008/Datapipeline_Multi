from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from knowledge_hub.recall import handle_request, search_cards, summarize_cards
from knowledge_hub.worker import process_once


class TestRecallSearch(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.vault = Path(self.temp.name) / "Vault"
        self.vault.mkdir()
        self.note = self.vault / "Cards" / "fever.md"
        self.note.parent.mkdir()
        self.note.write_text(
            "---\ntitle: 腫瘍熱カード\ncaptured_at: 2026-08-01\n"
            "summary: ナプロキセンテストの記録\n---\n腫瘍熱ではナプロキセンを検討。\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_search_returns_frontmatter_card_without_llm(self):
        with mock.patch("knowledge_hub.recall.call_claude") as llm:
            result = search_cards("腫瘍熱", self.vault)
        llm.assert_not_called()
        self.assertEqual(result["cards"][0]["title"], "腫瘍熱カード")
        self.assertEqual(result["cards"][0]["summary"], "ナプロキセンテストの記録")
        self.assertEqual(result["cards"][0]["filepath"], "Cards/fever.md")
        self.assertIn("obsidian://open", result["cards"][0]["link"])

    def test_summarize_uses_numbered_sources(self):
        with mock.patch("knowledge_hub.recall.call_claude", return_value="記録では有用です。[1]") as llm:
            result = summarize_cards("腫瘍熱", ["Cards/fever.md"], self.vault)
        self.assertEqual(result["answer"], "記録では有用です。[1]")
        self.assertIn("[1] Cards/fever.md", llm.call_args.args[0])

    def test_summarize_rejects_path_outside_vault(self):
        result = summarize_cards("secret", ["../secret.md"], self.vault)
        self.assertEqual(result["error"], "対象カードがありません")

    def test_handle_request_validates_shape(self):
        self.assertIn("error", handle_request({"type": "search"}, self.vault))
        self.assertIn("error", handle_request({"type": "unknown", "query": "x"}, self.vault))


class TestRecallWorker(unittest.TestCase):
    def test_process_once_posts_result(self):
        request = {"request": {"id": "request-1", "payload": {"type": "search", "query": "x"}}}
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "knowledge_hub.worker._request_json",
            side_effect=[request, {"ok": True}],
        ) as transport, mock.patch(
            "knowledge_hub.worker.handle_request", return_value={"cards": []}
        ):
            processed = process_once("https://example.test/exec", "token", Path(tmp))
        self.assertTrue(processed)
        posted = transport.call_args_list[1].kwargs["data"]
        self.assertEqual(posted["id"], "request-1")
        self.assertEqual(posted["result"], {"cards": []})


if __name__ == "__main__":
    unittest.main()
