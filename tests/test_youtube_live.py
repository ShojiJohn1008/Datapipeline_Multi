from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from knowledge_hub import youtube_live as yl

SRT = """1
00:00:01,000 --> 00:00:04,500
こんにちは、今日は腫瘍熱の話です。

2
00:00:05,000 --> 00:00:08,000
ナプロキセンテストについて。

3
00:00:40,000 --> 00:00:42,000
ご視聴ありがとうございました。

4
00:00:43,000 --> 00:00:44,000
ご視聴ありがとうございました。

5
00:00:45,000 --> 00:00:46,000
ご視聴ありがとうございました。

6
00:00:47,000 --> 00:00:48,000
ご視聴ありがとうございました。
"""

INFO = yl.VideoInfo(
    video_id="abcDEF12345",
    url="https://www.youtube.com/watch?v=abcDEF12345",
    title="総合診療ライブ #12 <script>",
    channel="Test Channel",
    started_at="2026-11-24T19:00:00",
    was_live=True,
)


class ParsingTests(unittest.TestCase):
    def test_video_id_variants(self) -> None:
        for url in (
            "https://www.youtube.com/watch?v=abcDEF12345&t=3s",
            "https://youtu.be/abcDEF12345",
            "https://www.youtube.com/live/abcDEF12345?si=x",
        ):
            self.assertEqual(yl.video_id_from_url(url), "abcDEF12345")
        with self.assertRaises(yl.LiveError):
            yl.video_id_from_url("https://example.com/")

    def test_parse_and_clean(self) -> None:
        segments = yl.drop_repetitions(yl.parse_srt(SRT))
        self.assertEqual(segments[0].start, 1.0)
        self.assertEqual(len(segments), 5)  # 4回目の繰り返しを除去
        merged = yl.merge_segments(segments)
        self.assertEqual(len(merged), 2)
        self.assertIn("ナプロキセン", merged[0].text)

    def test_linkify(self) -> None:
        out = yl.linkify_timestamps("要点 [00:01:05] と [already](x)", INFO.url)
        self.assertIn(f"[00:01:05]({INFO.url}&t=65s)", out)
        self.assertEqual(yl.linkify_timestamps(out, INFO.url), out)  # 二重リンクしない


class RenderTests(unittest.TestCase):
    def test_note_without_summary_keeps_transcript(self) -> None:
        segs = yl.parse_srt(SRT)
        note = yl.render_note(INFO, segs, None, media_path=None,
                              transcript_path=Path("/a/transcript.srt"), summary_error="x")
        self.assertTrue(note.startswith("---\nid: \"youtube-abcDEF12345\""))
        self.assertIn("要約は未作成", note)
        self.assertIn(f"[00:00:01]({INFO.url}&t=1s)", note)
        self.assertNotIn("<script>", note.split("\n---\n", 1)[1])

    def test_note_path(self) -> None:
        path = yl.note_path_for(INFO, Path("/v/Cards"))
        self.assertEqual(path.parent, Path("/v/Cards/2026/11"))
        self.assertTrue(path.name.endswith("--abcDEF12345.md"))


class EndToEndTests(unittest.TestCase):
    def test_run_from_transcript_with_fake_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault, archive = root / "vault", root / "archive"
            vault.mkdir(); archive.mkdir()
            srt = root / "transcript.srt"
            srt.write_text(SRT, encoding="utf-8")
            agent = root / "agent.py"
            agent.write_text(
                "import sys; sys.stdin.read(); print('## 概要\\n腫瘍熱の回 [00:00:05]')\n",
                encoding="utf-8",
            )
            with patch.object(yl, "fetch_info", return_value=INFO), \
                 patch.dict(os.environ, {"KH_CARDS_PATH": ""}, clear=False):
                code = yl.run([
                    INFO.url, "--vault", str(vault), "--archive", str(archive),
                    "--transcript", str(srt), "--agent-cmd", f"{sys.executable} {agent}",
                ])
            self.assertEqual(code, 0)
            notes = list((vault / "Cards").rglob("*.md"))
            self.assertEqual(len(notes), 1)
            body = notes[0].read_text(encoding="utf-8")
            self.assertIn(f"[00:00:05]({INFO.url}&t=5s)", body)
            self.assertIn("## 文字起こし", body)

    def test_full_pipeline_with_fake_tools(self) -> None:
        """yt-dlp / ffmpeg / mlx_whisper を偽コマンドに差し替えて全段を通す。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault, archive, bindir = root / "vault", root / "archive", root / "bin"
            for d in (vault, archive, bindir):
                d.mkdir()
            fakes = {
                "yt-dlp": "import sys,re\nargs=sys.argv[1:]\nout=args[args.index('-o')+1].replace('%(ext)s','mp4')\nopen(out,'wb').write(b'x'*10)\n",
                "ffmpeg": "import sys\nopen(sys.argv[-1],'wb').write(b'\\0'*4096)\n",
                "mlx_whisper": (
                    "import sys,os\na=sys.argv\nd=a[a.index('--output-dir')+1]\n"
                    f"open(os.path.join(d,'transcript.srt'),'w').write({SRT!r})\n"
                ),
            }
            for name, src in fakes.items():
                p = bindir / name
                p.write_text(f"#!{sys.executable}\n{src}", encoding="utf-8")
                p.chmod(0o755)
            env = {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}
            with patch.object(yl, "fetch_info", return_value=INFO), patch.dict(os.environ, env):
                code = yl.run([INFO.url, "--vault", str(vault), "--archive", str(archive), "--no-summary"])
            self.assertEqual(code, 0)
            work = archive / "YouTube" / "2026-11-24-abcDEF12345"
            self.assertTrue((work / "video.mp4").is_file())
            self.assertTrue((work / "transcript.txt").is_file())
            self.assertFalse((work / "audio16k.wav").exists())
            self.assertEqual(len(list((vault / "Cards").rglob("*.md"))), 1)


if __name__ == "__main__":
    unittest.main()
