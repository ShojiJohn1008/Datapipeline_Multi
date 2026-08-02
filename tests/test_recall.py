from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from knowledge_hub.llm import LlmCommandFailedError, LlmCommandNotFoundError, LlmTimeoutError
from knowledge_hub.recall import handle_request, search_cards, summarize_cards
from knowledge_hub.worker import RelayTransportError, process_once


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

    def test_search_defaults_to_cards_and_can_restore_whole_vault(self):
        source = self.vault / "Sources" / "raw.md"
        source.parent.mkdir()
        source.write_text("腫瘍熱\n腫瘍熱\n腫瘍熱\n", encoding="utf-8")
        result = search_cards("腫瘍熱", self.vault)
        self.assertTrue(all(card["filepath"].startswith("Cards/") for card in result["cards"]))
        with mock.patch.dict("os.environ", {"KH_RECALL_SEARCH_SCOPE": "vault"}):
            restored = search_cards("腫瘍熱", self.vault)
        self.assertTrue(any(card["filepath"].startswith("Sources/") for card in restored["cards"]))

    def test_capture_writes_one_intent_card_and_retry_preserves_edits(self):
        payload = {
            "type": "capture",
            "card": {
                "id": "supabase-card-1",
                "question": "ARNIの開始時に確認することは？",
                "answer": "血圧、腎機能、カリウムを確認する。",
                "intent": "外来で開始前チェックを忘れないため",
                "category": "治療",
                "tags": ["心不全", "ARNI"],
                "aliases": ["サクビトリルバルサルタン"],
                "source_name": "example.test",
                "source_url": "https://example.test/guideline.pdf",
                "source_type": "pdf",
                "captured_at": "2026-08-02T00:00:00+09:00",
            },
        }
        created = handle_request(payload, self.vault)
        self.assertTrue(created["created"])
        path = self.vault / str(created["filepath"])
        content = path.read_text(encoding="utf-8")
        self.assertIn("## Why I saved this", content)
        self.assertIn("外来で開始前チェックを忘れないため", content)
        self.assertIn("## Answer", content)
        path.write_text(content + "\nuser edit\n", encoding="utf-8")
        repeated = handle_request(payload, self.vault)
        self.assertFalse(repeated["created"])
        self.assertTrue(path.read_text(encoding="utf-8").endswith("user edit\n"))

    def test_capture_requires_intent_and_rejects_unsafe_source_url(self):
        base = {
            "id": "capture-2",
            "question": "質問",
            "answer": "回答",
            "intent": "",
            "source_url": "https://example.test",
        }
        missing = handle_request({"type": "capture", "card": base}, self.vault)
        self.assertEqual(missing["error"], "intentが必要です")
        unsafe = dict(base, intent="理由", source_url="file:///etc/passwd")
        rejected = handle_request({"type": "capture", "card": unsafe}, self.vault)
        self.assertIn("httpまたはhttps", rejected["error"])
        injected = dict(base, intent="理由", source_url="https://example.test/ok\n## injected")
        rejected = handle_request({"type": "capture", "card": injected}, self.vault)
        self.assertIn("制御文字", rejected["error"])

    def test_summarize_uses_numbered_sources(self):
        # The default-value assertion must not inherit a developer's worker timeout.
        clean_env = os.environ.copy()
        clean_env.pop("KH_RECALL_ANSWER_TIMEOUT", None)
        with mock.patch.dict("os.environ", clean_env, clear=True), mock.patch(
            "knowledge_hub.recall.call_claude", return_value="記録では有用です。[1]"
        ) as llm:
            result = summarize_cards("腫瘍熱", ["Cards/fever.md"], self.vault)
        self.assertEqual(result["answer"], "記録では有用です。[1]")
        self.assertIn("[1] Cards/fever.md", llm.call_args.args[0])
        self.assertEqual(llm.call_args.kwargs["timeout"], 90.0)

    def test_summarize_timeout_can_be_overridden(self):
        with mock.patch.dict("os.environ", {"KH_RECALL_ANSWER_TIMEOUT": "45"}), mock.patch(
            "knowledge_hub.recall.call_claude", return_value="記録です。[1]"
        ) as llm:
            summarize_cards("腫瘍熱", ["Cards/fever.md"], self.vault)
        self.assertEqual(llm.call_args.kwargs["timeout"], 45.0)

    def test_summarize_returns_safe_cli_failure_reasons(self):
        cases = (
            (LlmTimeoutError("private detail"), "要約CLIがタイムアウトしました"),
            (LlmCommandNotFoundError("private detail"), "要約CLIが見つかりません"),
            (LlmCommandFailedError("private detail"), "要約CLIがエラー終了しました"),
        )
        for failure, expected in cases:
            with self.subTest(expected=expected), mock.patch(
                "knowledge_hub.recall.call_claude", side_effect=failure
            ):
                result = summarize_cards("腫瘍熱", ["Cards/fever.md"], self.vault)
            self.assertEqual(result["error"], expected)
            self.assertNotIn("private detail", result["error"])

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
        self.assertEqual(transport.call_args_list[0].kwargs["timeout"], 30.0)
        self.assertEqual(transport.call_args_list[1].kwargs["timeout"], 30.0)

    def test_process_once_reports_safe_fetch_phase(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "knowledge_hub.worker._request_json", side_effect=TimeoutError("secret URL")
        ):
            with self.assertRaisesRegex(RelayTransportError, "依頼取得に失敗") as caught:
                process_once("https://example.test/exec", "token", Path(tmp))
        self.assertNotIn("secret URL", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
