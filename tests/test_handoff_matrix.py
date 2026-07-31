"""HANDOFF_GPT56.md のテストマトリクスと --json の受け入れテスト。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from knowledge_hub.find import normalize_query, relax_terms, run_find
from knowledge_hub.llm import expand_query
from knowledge_hub.vault_search import FileHits, RipgrepError, extract_snippet, run_ripgrep, score_files

ROOT = Path(__file__).resolve().parents[1]
VAULT = ROOT / "tests" / "fixtures" / "vault"


class TestRipgrepMatrix(unittest.TestCase):
    def test_multiple_terms_are_or(self):
        hits, _ = run_ripgrep(["naproxen", "reference point"], VAULT)
        self.assertIn("Clinical/腫瘍熱.md", hits)
        self.assertIn("Reading/行動経済学メモ.md", hits)

    def test_search_is_case_insensitive(self):
        hits, _ = run_ripgrep(["NAPROXEN"], VAULT)
        self.assertIn("Clinical/腫瘍熱.md", hits)

    def test_missing_rg_raises_friendly_error(self):
        with mock.patch.dict(os.environ, {"KH_RG_BIN": "/no/such/rg"}):
            with self.assertRaises(RipgrepError):
                run_ripgrep(["test"], VAULT)


class TestScoringMatrix(unittest.TestCase):
    def test_more_hits_win_with_equal_mtime(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            for name in ("one.md", "many.md"):
                path = vault / name
                path.write_text("x", encoding="utf-8")
                os.utime(path, (now, now))
            ranked = score_files({"one.md": FileHits([1]), "many.md": FileHits([1, 2, 3])}, vault, now)
            self.assertEqual(ranked[0].path.name, "many.md")


class TestSnippetMatrix(unittest.TestCase):
    def test_overlapping_ranges_are_merged_without_duplicate_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "note.md"
            path.write_text("\n".join(f"line-{n}" for n in range(1, 11)), encoding="utf-8")
            snippet = extract_snippet(path, [4, 6], margin=2)
            self.assertEqual(snippet.count("line-4"), 1)
            self.assertNotIn("\n…\n", snippet)

    def test_max_chars_adds_ellipsis(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "note.md"
            path.write_text("abcdefghij", encoding="utf-8")
            snippet = extract_snippet(path, [1], margin=0, max_chars=5)
            self.assertEqual(snippet, "abcde…")


class TestTermsAndLlmMatrix(unittest.TestCase):
    def test_relaxation_prefixes(self):
        terms = relax_terms(["腫瘍熱", "anchoring", "a"])
        self.assertIn("腫瘍", terms)
        self.assertIn("anchor", terms)
        self.assertNotIn("a", terms)

    def _script(self, directory: Path, body: str) -> Path:
        script = directory / "fake.sh"
        script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        script.chmod(0o755)
        return script

    def test_expand_query_uses_fake_command_and_caps_terms(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = self._script(Path(tmp), "echo '[\"one\",\"two\",\"three\",\"four\",\"five\",\"six\"]'\n")
            with mock.patch.dict(os.environ, {"KH_CLAUDE_CMD": str(script)}):
                terms = expand_query("original")
        self.assertEqual(terms[0], "original")
        self.assertEqual(len(terms), 6)

    def test_expand_query_degrades_on_invalid_or_nonzero_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = self._script(Path(tmp), "echo not-json\n")
            with mock.patch.dict(os.environ, {"KH_CLAUDE_CMD": str(bad)}):
                self.assertEqual(expand_query("original"), ["original"])
            bad.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"KH_CLAUDE_CMD": str(bad)}):
                self.assertEqual(expand_query("original"), ["original"])

    def test_normalize_query_variations(self):
        cases = {
            "腫瘍熱とは？": "腫瘍熱",
            "腫瘍熱の話探して": "腫瘍熱",
            "探して 腫瘍熱について": "腫瘍熱",
            "/find 腫瘍熱だっけ": "腫瘍熱",
            "腫瘍熱を調べた。": "腫瘍熱",
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(normalize_query(query), expected)

    def test_normalize_query_keeps_boilerplate_only_input(self):
        self.assertEqual(normalize_query("前に調べた"), "前に調べた")

    def test_obsidian_link_encodes_japanese_and_spaces(self):
        from knowledge_hub.vault_search import obsidian_link
        link = obsidian_link(Path("/tmp/私の Vault"), Path("診療 メモ/腫瘍熱.md"))
        self.assertIn("vault=%E7%A7%81%E3%81%AE%20Vault", link)
        self.assertIn("file=%E8%A8%BA%E7%99%82%20%E3%83%A1%E3%83%A2%2F", link)
        self.assertNotIn(".md", link)
class TestPipelineAndCliMatrix(unittest.TestCase):
    def test_no_hits_message(self):
        self.assertIn("見つからず。試した語：", run_find("ZZZUNIQUEQWERTY", VAULT, use_llm=False))

    def test_no_llm_has_sources(self):
        reply = run_find("腫瘍熱", VAULT, use_llm=False)
        self.assertIn("📚 出典:", reply)

    def test_icloud_placeholder_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            (vault / "x.md.icloud").write_text("", encoding="utf-8")
            with mock.patch("knowledge_hub.find.try_brctl_download", return_value=False):
                reply = run_find("nothing", vault, use_llm=False)
            self.assertIn("☁️", reply)

    def _cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-m", "knowledge_hub.find", *args], cwd=ROOT,
                              text=True, capture_output=True)

    def test_cli_success(self):
        proc = self._cli("腫瘍熱", "--vault", str(VAULT), "--no-llm")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("📚 出典:", proc.stdout)

    def test_cli_missing_vault(self):
        proc = self._cli("test", "--vault", "/definitely/not/a/vault", "--no-llm")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("Vault", proc.stderr)

    def test_json_success(self):
        proc = self._cli("腫瘍熱", "--vault", str(VAULT), "--no-llm", "--json")
        payload = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertGreater(payload["total_files"], 0)
        self.assertIn("file", payload["sources"][0])
        self.assertIn("hits", payload["sources"][0])

    def test_json_no_hits_has_empty_sources(self):
        proc = self._cli("ZZZUNIQUEQWERTY", "--vault", str(VAULT), "--no-llm", "--json")
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["sources"], [])
        self.assertEqual(payload["total_files"], 0)
        self.assertIsNone(payload["answer"])


if __name__ == "__main__":
    unittest.main()
