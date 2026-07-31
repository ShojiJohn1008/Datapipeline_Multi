"""シードテスト（受け入れ四関門の1を含む）。

GPT-5.6へ: これが書き方の見本。docs/HANDOFF_GPT56.md のマトリクスに沿って量産すること。
実行: リポジトリrootで `python3 -m unittest discover -s tests -v`
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from knowledge_hub.find import normalize_query, relax_terms
from knowledge_hub.vault_search import (
    FileHits,
    extract_snippet,
    obsidian_link,
    run_ripgrep,
    score_files,
)

FIXTURE_VAULT = Path(__file__).parent / "fixtures" / "vault"


class TestRunRipgrep(unittest.TestCase):
    def test_known_query_returns_expected_file(self):
        """受け入れ関門1: 既知クエリで期待ファイルが返る。"""
        hits, timed_out = run_ripgrep(["腫瘍熱"], FIXTURE_VAULT)
        self.assertFalse(timed_out)
        self.assertIn("Clinical/腫瘍熱.md", hits)
        self.assertIn("Daily/2026-07-10.md", hits)

    def test_obsidian_dir_is_excluded(self):
        hits, _ = run_ripgrep(["腫瘍熱"], FIXTURE_VAULT)
        self.assertNotIn(".obsidian/hidden.md", hits)

    def test_no_match_returns_empty(self):
        hits, timed_out = run_ripgrep(["存在しないはずの語XYZQ"], FIXTURE_VAULT)
        self.assertEqual(hits, {})
        self.assertFalse(timed_out)

    def test_hit_line_numbers_recorded(self):
        hits, _ = run_ripgrep(["ナプロキセン"], FIXTURE_VAULT)
        fh = hits["Clinical/腫瘍熱.md"]
        self.assertGreaterEqual(fh.hits, 2)
        self.assertTrue(all(n >= 1 for n in fh.line_numbers))


class TestScoring(unittest.TestCase):
    def test_newer_file_wins_with_same_hits(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as d:
            vault = Path(d)
            old, new = vault / "old.md", vault / "new.md"
            old.write_text("x", encoding="utf-8")
            new.write_text("x", encoding="utf-8")
            os.utime(old, (now - 365 * 86400, now - 365 * 86400))
            os.utime(new, (now - 86400, now - 86400))
            hits = {"old.md": FileHits([1]), "new.md": FileHits([1])}
            ranked = score_files(hits, vault, now=now)
            self.assertEqual(ranked[0].path.name, "new.md")


class TestSnippet(unittest.TestCase):
    def test_reads_only_around_hits(self):
        path = FIXTURE_VAULT / "Clinical" / "腫瘍熱.md"
        snippet = extract_snippet(path, [6], margin=1)
        self.assertIn("ナプロキセンテスト", snippet)
        self.assertNotIn("参考", snippet)  # 遠い行は含まれない


class TestRelaxTerms(unittest.TestCase):
    def test_cjk_prefix_and_split(self):
        relaxed = relax_terms(["腫瘍熱", "reference point"])
        self.assertIn("腫瘍", relaxed)          # CJK接頭辞
        self.assertIn("reference", relaxed)     # 空白分割
        self.assertIn("腫瘍熱", relaxed)        # 元の語は維持
        self.assertTrue(all(len(t) >= 2 for t in relaxed))


class TestNormalizeQuery(unittest.TestCase):
    def test_strips_colloquial_boilerplate(self):
        self.assertEqual(normalize_query("腫瘍熱について前に調べた？"), "腫瘍熱")
        self.assertEqual(normalize_query("探して 参照点とは"), "参照点")

    def test_keeps_original_when_stripping_too_much(self):
        self.assertEqual(normalize_query("前に調べた"), "前に調べた")


class TestObsidianLink(unittest.TestCase):
    def test_encodes_and_drops_extension(self):
        link = obsidian_link(Path("/x/JohnSecondBrain"), Path("Clinical/腫瘍熱.md"))
        self.assertTrue(link.startswith("obsidian://open?vault=JohnSecondBrain&file="))
        self.assertNotIn(".md", link)
        self.assertIn("Clinical%2F%E8%85%AB%E7%98%8D%E7%86%B1", link)


if __name__ == "__main__":
    unittest.main()
