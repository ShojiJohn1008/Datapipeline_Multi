"""B-01 音声レーンの受け入れテスト（b01-instruction.md 関門1: 偽ASR・偽LLM）。

検知→取り込み→文字起こし→カード生成→保存→台帳の全経路と、
同期中保留・二重処理防止（台帳＋カード実在チェック）・縮退経路を検証する。
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from knowledge_hub.voice_ingest import discover_recordings
from knowledge_hub.voice_lane import run_pass, sanitize_title

ROOT = Path(__file__).resolve().parents[1]
_CORE_DATA_EPOCH = 978307200.0
# 2026-08-01 08:12:00（ローカル時刻）で固定
TS = time.mktime((2026, 8, 1, 8, 12, 0, 0, 0, -1))
TRANSCRIPT = "これは腫瘍熱についての音声メモの文字起こしです。"


def _script(directory: Path, name: str, body: str) -> Path:
    script = directory / name
    script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    script.chmod(0o755)
    return script


class VoiceLaneCase(unittest.TestCase):
    """一時ディレクトリ一式と偽ASR・偽LLMを持つ共通土台。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source = self.root / "memos"
        self.archive = self.root / "Archive"
        self.vault = self.root / "vault"
        for d in (self.source, self.archive, self.vault):
            d.mkdir()
        self.state = self.root / "state.json"
        self.asr_count = self.root / "asr_count"
        self.asr = _script(self.root, "asr.sh",
                           f"echo run >> '{self.asr_count}'\necho '{TRANSCRIPT}'\n")
        self.llm = _script(
            self.root, "llm.sh",
            'echo \'{"title":"腫瘍熱メモ","summary":"腫瘍熱について考えた。","tags":["医学","発熱"]}\'\n')
        env = mock.patch.dict(os.environ, {
            "KH_ASR_CMD": str(self.asr), "KH_CLAUDE_CMD": str(self.llm),
        })
        env.start()
        self.addCleanup(env.stop)

    def add_recording(self, name: str = "録音.m4a") -> Path:
        path = self.source / name
        path.write_bytes(b"fake-audio-bytes")
        os.utime(path, (TS, TS))
        return path

    def run_lane(self) -> list[str]:
        return run_pass(self.source, self.archive, self.vault / "Cards", self.state)

    def asr_runs(self) -> int:
        if not self.asr_count.exists():
            return 0
        return len(self.asr_count.read_text(encoding="utf-8").splitlines())


class TestDiscovery(VoiceLaneCase):
    def _make_db(self, rows):
        conn = sqlite3.connect(self.source / "CloudRecordings.db")
        conn.execute(
            "CREATE TABLE ZCLOUDRECORDING "
            "(Z_PK INTEGER PRIMARY KEY, ZCUSTOMLABEL TEXT, ZDATE REAL, ZPATH TEXT)")
        conn.executemany(
            "INSERT INTO ZCLOUDRECORDING (ZCUSTOMLABEL, ZDATE, ZPATH) VALUES (?, ?, ?)", rows)
        conn.commit()
        conn.close()

    def test_folder_scan_when_no_db(self):
        self.add_recording("朝の散歩.m4a")
        recs = discover_recordings(self.source)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].title, "朝の散歩")
        self.assertAlmostEqual(recs[0].recorded_at, TS, delta=2)

    def test_db_gives_title_and_date(self):
        self.add_recording("20260801 081200.m4a")
        self._make_db([("通勤メモ", TS - _CORE_DATA_EPOCH, "20260801 081200.m4a")])
        recs = discover_recordings(self.source)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].title, "通勤メモ")
        self.assertAlmostEqual(recs[0].recorded_at, TS, delta=2)

    def test_db_row_without_file_is_skipped(self):
        self.add_recording("real.m4a")
        self._make_db([
            ("実在", TS - _CORE_DATA_EPOCH, "real.m4a"),
            ("未同期", TS - _CORE_DATA_EPOCH, "ghost.m4a"),
        ])
        recs = discover_recordings(self.source)
        self.assertEqual([r.title for r in recs], ["実在"])

    def test_broken_db_falls_back_to_scan(self):
        self.add_recording("録音.m4a")
        (self.source / "CloudRecordings.db").write_bytes(b"not a sqlite database")
        recs = discover_recordings(self.source)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].title, "録音")


class TestPipeline(VoiceLaneCase):
    def test_sync_guard_then_process(self):
        self.add_recording()
        first = self.run_lane()
        self.assertTrue(any("⏳" in line for line in first))
        self.assertEqual(self.asr_runs(), 0)  # 1パス目では文字起こししない
        second = self.run_lane()
        self.assertTrue(any("✅" in line for line in second))
        self.assertEqual(self.asr_runs(), 1)

    def test_card_content_and_archive_naming(self):
        self.add_recording()
        self.run_lane()
        self.run_lane()
        archived = self.archive / "2026-08" / "0801_0812_録音.m4a"
        self.assertTrue(archived.is_file())
        card = self.vault / "Cards" / "2026-08-01_腫瘍熱メモ.md"
        self.assertTrue(card.is_file(), list((self.vault / "Cards").glob("*")))
        text = card.read_text(encoding="utf-8")
        self.assertIn("type: voice", text)
        self.assertIn("captured_at: 2026-08-01 08:12", text)
        self.assertIn('source: "Archive/2026-08/0801_0812_録音.m4a"', text)
        self.assertIn("腫瘍熱について考えた。", text)
        self.assertIn("## 全文文字起こし", text)
        self.assertIn(TRANSCRIPT, text)
        self.assertIn('tags: ["医学", "発熱"]', text)
        # ボイスメモ側の原本は不変
        self.assertTrue((self.source / "録音.m4a").is_file())

    def test_repeated_passes_do_not_duplicate(self):
        self.add_recording()
        self.run_lane()
        self.run_lane()
        third = self.run_lane()
        self.assertEqual(third, [])
        self.assertEqual(self.asr_runs(), 1)
        self.assertEqual(len(list((self.vault / "Cards").glob("*.md"))), 1)

    def test_lost_ledger_does_not_retranscribe(self):
        """台帳が消えても、既存カードがあれば文字起こしを再実行しない（重複防止の保険）。"""
        self.add_recording()
        self.run_lane()
        self.run_lane()
        self.state.unlink()
        self.run_lane()  # seenが空になるので同期待ちから再開
        lines = self.run_lane()
        self.assertTrue(any("↩️" in line for line in lines))
        self.assertEqual(self.asr_runs(), 1)  # 増えていない
        self.assertEqual(len(list((self.vault / "Cards").glob("*.md"))), 1)

    def test_llm_failure_degrades_but_saves(self):
        self.llm.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
        self.add_recording()
        self.run_lane()
        lines = self.run_lane()
        self.assertTrue(any("✅" in line for line in lines))
        card = self.vault / "Cards" / "2026-08-01_音声メモ0812.md"
        self.assertTrue(card.is_file())
        text = card.read_text(encoding="utf-8")
        self.assertIn("（要約なし：縮退保存）", text)
        self.assertIn(TRANSCRIPT, text)  # 文字起こしは失わない

    def test_asr_failure_retries_next_pass(self):
        self.asr.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
        self.add_recording()
        self.run_lane()
        lines = self.run_lane()
        self.assertTrue(any("⚠️" in line for line in lines))
        self.assertEqual(list((self.vault / "Cards").glob("*.md")), [])
        # ASRを復旧させると次パスで成功する
        self.asr.write_text(f"#!/bin/sh\necho '{TRANSCRIPT}'\n", encoding="utf-8")
        lines = self.run_lane()
        self.assertTrue(any("✅" in line for line in lines))

    def test_same_title_gets_numbered(self):
        self.add_recording("録音A.m4a")
        self.add_recording("録音B.m4a")
        self.run_lane()
        self.run_lane()
        names = sorted(p.name for p in (self.vault / "Cards").glob("*.md"))
        self.assertEqual(names, ["2026-08-01_腫瘍熱メモ-2.md", "2026-08-01_腫瘍熱メモ.md"])

    def test_custom_cards_dir(self):
        """カード保存先はVault/Cards固定でなく任意フォルダを指定できる（Vault構成変更対応）。"""
        cards = self.vault / "2_Cards" / "voice"
        self.add_recording()
        run_pass(self.source, self.archive, cards, self.state)
        run_pass(self.source, self.archive, cards, self.state)
        self.assertTrue((cards / "2026-08-01_腫瘍熱メモ.md").is_file())

    def test_sanitize_title(self):
        self.assertEqual(sanitize_title("鑑別/腫瘍熱:メモ", "fb"), "鑑別 腫瘍熱 メモ")
        self.assertEqual(sanitize_title('***"???"', "fb"), "fb")
        self.assertEqual(len(sanitize_title("あ" * 50, "fb")), 30)


class TestCli(VoiceLaneCase):
    def _cli(self, *args: str) -> subprocess.CompletedProcess:
        env = {**os.environ,
               "KH_ASR_CMD": str(self.asr), "KH_CLAUDE_CMD": str(self.llm)}
        return subprocess.run(
            [sys.executable, "-m", "knowledge_hub.voice_lane", *args],
            cwd=ROOT, env=env, text=True, capture_output=True)

    def test_cli_two_passes_end_to_end(self):
        self.add_recording()
        common = ["--source", str(self.source), "--archive", str(self.archive),
                  "--vault", str(self.vault), "--state", str(self.state)]
        first = self._cli(*common)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("⏳", first.stdout)
        second = self._cli(*common)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("✅", second.stdout)
        self.assertTrue((self.vault / "Cards" / "2026-08-01_腫瘍熱メモ.md").is_file())

    def test_cli_no_new_recordings(self):
        proc = self._cli("--source", str(self.source), "--archive", str(self.archive),
                         "--vault", str(self.vault), "--state", str(self.state))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("新着なし", proc.stdout)

    def test_cli_missing_archive_is_config_error(self):
        proc = self._cli("--source", str(self.source), "--archive", "/no/such/archive",
                         "--vault", str(self.vault))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("Archive", proc.stderr)


if __name__ == "__main__":
    unittest.main()
