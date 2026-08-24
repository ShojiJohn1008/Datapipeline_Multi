from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from knowledge_hub import config
from knowledge_hub.youtube_download import (
    DownloadFailedError,
    DownloadTimeoutError,
    InvalidUrlError,
    NoOutputError,
    YoutubeDownloadError,
    YtdlpMissingError,
    canonical_url,
    download_video,
    main,
    parse_video_id,
)

VIDEO_ID = "dQw4w9WgXcQ"


class TestParseVideoId(unittest.TestCase):
    def test_accepts_every_youtube_link_shape(self) -> None:
        urls = (
            f"https://www.youtube.com/watch?v={VIDEO_ID}",
            f"https://www.youtube.com/watch?v={VIDEO_ID}&list=PL123&t=42s",
            f"https://m.youtube.com/watch?v={VIDEO_ID}",
            f"https://music.youtube.com/watch?v={VIDEO_ID}",
            f"https://youtu.be/{VIDEO_ID}",
            f"https://youtu.be/{VIDEO_ID}?si=abcdef",
            f"https://www.youtube.com/shorts/{VIDEO_ID}",
            f"https://www.youtube.com/live/{VIDEO_ID}",
            f"https://www.youtube-nocookie.com/embed/{VIDEO_ID}",
            f"  https://www.youtube.com/watch?v={VIDEO_ID}  ",
            f"youtu.be/{VIDEO_ID}",
            VIDEO_ID,
        )
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(parse_video_id(url), VIDEO_ID)

    def test_rejects_non_youtube_and_malformed_input(self) -> None:
        urls = (
            "",
            "   ",
            "https://vimeo.com/123456",
            "https://youtube.com.evil.example/watch?v=" + VIDEO_ID,
            "https://www.youtube.com/watch?v=short",
            "https://www.youtube.com/watch",
            "https://www.youtube.com/results?search_query=test",
            f"javascript:alert(1)#youtube.com/watch?v={VIDEO_ID}",
            f"file:///etc/passwd?v={VIDEO_ID}",
            "--exec=rm -rf /",
        )
        for url in urls:
            with self.subTest(url=url), self.assertRaises(InvalidUrlError):
                parse_video_id(url)

    def test_canonical_url_is_rebuilt_not_echoed(self) -> None:
        source = f"https://youtu.be/{VIDEO_ID}?si=tracking&list=PL9"
        self.assertEqual(
            canonical_url(parse_video_id(source)),
            f"https://www.youtube.com/watch?v={VIDEO_ID}",
        )


class TestDownloadVideo(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.destination = self.root / "Video"
        self.url = f"https://www.youtube.com/watch?v={VIDEO_ID}"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def command(self, body: str) -> str:
        script = self.root / f"fake_{len(list(self.root.glob('fake_*')))}.py"
        script.write_text(body, encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        return f'{sys.executable} "{script}"'

    def fake_ytdlp(self, filename: str, size: int = 32) -> str:
        """--paths の下に1本だけ完成ファイルを置く、成功するyt-dlpの代役。"""
        return (
            "import sys\n"
            "from pathlib import Path\n"
            "argv = sys.argv[1:]\n"
            "work = Path(argv[argv.index('--paths') + 1])\n"
            f"(work / {filename!r}).write_bytes(b'v' * {size})\n"
        )

    def test_saves_one_file_named_by_title_and_video_id(self) -> None:
        command = self.command(self.fake_ytdlp(f"配信メモ--{VIDEO_ID}.mp4", size=100))
        result = download_video(self.url, self.destination, command=command)
        self.assertEqual(result.status, "downloaded")
        self.assertEqual(result.video_id, VIDEO_ID)
        self.assertEqual(result.size_bytes, 100)
        self.assertEqual(result.path.name, f"配信メモ--{VIDEO_ID}.mp4")
        self.assertEqual(result.path.parent, self.destination)
        self.assertTrue(result.path.is_file())

    def test_hostile_title_is_sanitized_and_stays_in_destination(self) -> None:
        command = self.command(
            self.fake_ytdlp(f"..hoge; rm -rf ~ $(id)--{VIDEO_ID}.mp4")
        )
        result = download_video(self.url, self.destination, command=command)
        self.assertEqual(result.path.parent, self.destination)
        # 共有の safe_stem と同じ基準（英数字と ` -_().` だけを残す）で無害化する。
        for character in ";$~/":
            self.assertNotIn(character, result.path.name)
        self.assertFalse(result.path.name.startswith("."))
        self.assertTrue(result.path.name.endswith(f"--{VIDEO_ID}.mp4"))

    def test_unknown_extension_is_replaced(self) -> None:
        command = self.command(self.fake_ytdlp(f"clip--{VIDEO_ID}.exe.evilext"))
        result = download_video(self.url, self.destination, command=command)
        self.assertTrue(result.path.name.endswith(".bin"))

    def test_same_link_pasted_again_does_not_download_twice(self) -> None:
        command = self.command(self.fake_ytdlp(f"clip--{VIDEO_ID}.mp4"))
        first = download_video(self.url, self.destination, command=command)
        failing = self.command("import sys\nsys.exit(1)\n")
        second = download_video(
            f"https://youtu.be/{VIDEO_ID}", self.destination, command=failing
        )
        self.assertEqual(first.status, "downloaded")
        self.assertEqual(second.status, "exists")
        self.assertEqual(second.path, first.path)
        self.assertEqual(len(list(self.destination.glob(f"*--{VIDEO_ID}.*"))), 1)

    def test_work_directory_is_removed_on_success_and_failure(self) -> None:
        command = self.command(self.fake_ytdlp(f"clip--{VIDEO_ID}.mp4"))
        download_video(self.url, self.destination, command=command)
        failing = self.command("import sys\nsys.exit(1)\n")
        with self.assertRaises(DownloadFailedError):
            download_video(
                f"https://youtu.be/{'a' * 11}", self.destination, command=failing
            )
        self.assertEqual(list(self.destination.glob(".ytdl-*")), [])

    def test_partial_files_are_never_published(self) -> None:
        command = self.command(self.fake_ytdlp(f"clip--{VIDEO_ID}.mp4.part"))
        with self.assertRaises(NoOutputError):
            download_video(self.url, self.destination, command=command)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_private_video_gets_a_dedicated_error_code(self) -> None:
        command = self.command(
            "import sys\n"
            "sys.stderr.write('ERROR: Private video. Sign in if you have been "
            "granted access to this video\\n')\n"
            "sys.exit(1)\n"
        )
        with self.assertRaises(DownloadFailedError) as caught:
            download_video(self.url, self.destination, command=command)
        self.assertEqual(caught.exception.code, "private_video")

    def test_unavailable_and_network_failures_are_classified(self) -> None:
        cases = (
            ("ERROR: Video unavailable", "video_unavailable"),
            ("ERROR: Unable to download webpage: timed out", "network_error"),
            ("ERROR: Requested format is not available", "format_unavailable"),
            ("ERROR: something nobody predicted", "download_failed"),
        )
        for message, expected in cases:
            with self.subTest(message=message):
                command = self.command(
                    f"import sys\nsys.stderr.write({message!r})\nsys.exit(1)\n"
                )
                with self.assertRaises(DownloadFailedError) as caught:
                    download_video(self.url, self.destination, command=command)
                self.assertEqual(caught.exception.code, expected)

    def test_size_limit_abort_is_reported_as_too_large(self) -> None:
        command = self.command(
            "import sys\n"
            "sys.stderr.write('File is larger than max-filesize, skipping\\n')\n"
        )
        with self.assertRaises(DownloadFailedError) as caught:
            download_video(self.url, self.destination, command=command)
        self.assertEqual(caught.exception.code, "too_large")

    def test_timeout_and_missing_tool_are_dedicated_errors(self) -> None:
        slow = self.command("import time\ntime.sleep(5)\n")
        with self.assertRaises(DownloadTimeoutError):
            download_video(self.url, self.destination, command=slow, timeout=0.2)
        with self.assertRaises(YtdlpMissingError):
            download_video(
                self.url, self.destination, command=str(self.root / "missing-tool")
            )

    def test_audio_mode_drops_the_video_merge_option(self) -> None:
        recorder = self.root / "argv.txt"
        command = self.command(
            "import sys\n"
            "from pathlib import Path\n"
            "argv = sys.argv[1:]\n"
            f"Path({str(recorder)!r}).write_text('\\n'.join(argv), encoding='utf-8')\n"
            "work = Path(argv[argv.index('--paths') + 1])\n"
            f"(work / 'talk--{VIDEO_ID}.m4a').write_bytes(b'a' * 8)\n"
        )
        result = download_video(self.url, self.destination, command=command, audio=True)
        recorded = recorder.read_text(encoding="utf-8").splitlines()
        self.assertTrue(result.audio_only)
        self.assertTrue(result.path.name.endswith(".m4a"))
        self.assertNotIn("--merge-output-format", recorded)
        self.assertIn("--no-playlist", recorded)
        self.assertEqual(recorded[-1], f"https://www.youtube.com/watch?v={VIDEO_ID}")
        self.assertEqual(recorded[-2], "--")

    def test_explicit_nonpositive_limits_are_never_replaced_by_defaults(self) -> None:
        command = self.command(self.fake_ytdlp(f"clip--{VIDEO_ID}.mp4"))
        for argument in ({"timeout": 0}, {"max_bytes": 0}, {"max_bytes": -1}):
            with self.subTest(argument=argument), self.assertRaises(YoutubeDownloadError):
                download_video(self.url, self.destination, command=command, **argument)

    def test_missing_cookies_file_fails_before_running_the_tool(self) -> None:
        command = self.command("import sys\nsys.exit(1)\n")
        with self.assertRaises(YoutubeDownloadError):
            download_video(
                self.url,
                self.destination,
                command=command,
                cookies=self.root / "absent-cookies.txt",
            )


class TestVideoDirResolution(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.saved = {
            name: os.environ.pop(name, None)
            for name in ("KH_VIDEO_PATH", "KH_ARCHIVE_PATH")
        }

    def tearDown(self) -> None:
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.temporary.cleanup()

    def test_priority_is_argument_then_env_then_archive(self) -> None:
        explicit = self.root / "explicit"
        explicit.mkdir()
        os.environ["KH_VIDEO_PATH"] = str(self.root / "from-env")
        os.environ["KH_ARCHIVE_PATH"] = str(self.root)
        self.assertEqual(config.resolve_video_dir(str(explicit)), explicit.resolve())
        self.assertEqual(
            config.resolve_video_dir(), (self.root / "from-env").resolve()
        )
        os.environ.pop("KH_VIDEO_PATH")
        self.assertEqual(
            config.resolve_video_dir(), (self.root / "Video").resolve()
        )

    def test_unset_configuration_and_bad_targets_fail_loudly(self) -> None:
        with self.assertRaises(config.ConfigError):
            config.resolve_video_dir()
        file_path = self.root / "not-a-dir"
        file_path.write_text("x", encoding="utf-8")
        with self.assertRaises(config.ConfigError):
            config.resolve_video_dir(str(file_path))
        with self.assertRaises(config.ConfigError):
            config.resolve_video_dir(str(self.root / "missing" / "deep" / "video"))


class TestCli(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.destination = self.root / "Video"
        self.saved_command = os.environ.pop("KH_YTDLP_CMD", None)

    def tearDown(self) -> None:
        if self.saved_command is None:
            os.environ.pop("KH_YTDLP_CMD", None)
        else:
            os.environ["KH_YTDLP_CMD"] = self.saved_command
        self.temporary.cleanup()

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        import io
        from contextlib import redirect_stderr, redirect_stdout

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue().strip(), err.getvalue().strip()

    def test_success_prints_json_for_machines(self) -> None:
        import json

        script = self.root / "fake.py"
        script.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "argv = sys.argv[1:]\n"
            "work = Path(argv[argv.index('--paths') + 1])\n"
            f"(work / 'clip--{VIDEO_ID}.mp4').write_bytes(b'v' * 16)\n",
            encoding="utf-8",
        )
        os.environ["KH_YTDLP_CMD"] = f'{sys.executable} "{script}"'
        code, stdout, _ = self.run_cli(
            [f"https://youtu.be/{VIDEO_ID}", "--out", str(self.destination), "--json"]
        )
        payload = json.loads(stdout)
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "downloaded")
        self.assertEqual(payload["video_id"], VIDEO_ID)
        self.assertEqual(payload["bytes"], 16)
        self.assertTrue(payload["path"].endswith(f"clip--{VIDEO_ID}.mp4"))

    def test_bad_url_is_exit_2_and_download_failure_is_exit_1(self) -> None:
        code, _, stderr = self.run_cli(
            ["https://vimeo.com/1", "--out", str(self.destination)]
        )
        self.assertEqual(code, 2)
        self.assertTrue(stderr)
        os.environ["KH_YTDLP_CMD"] = f"{sys.executable} -c \"raise SystemExit(1)\""
        code, _, stderr = self.run_cli(
            [f"https://youtu.be/{VIDEO_ID}", "--out", str(self.destination)]
        )
        self.assertEqual(code, 1)
        self.assertTrue(stderr)

    def test_failure_json_carries_a_safe_error_code(self) -> None:
        import json

        script = self.root / "private.py"
        script.write_text(
            "import sys\n"
            "sys.stderr.write('ERROR: Private video. Sign in if you have been "
            "granted access to this video\\n')\n"
            "sys.exit(1)\n",
            encoding="utf-8",
        )
        os.environ["KH_YTDLP_CMD"] = f'{sys.executable} "{script}"'
        code, stdout, stderr = self.run_cli(
            [f"https://youtu.be/{VIDEO_ID}", "--out", str(self.destination), "--json"]
        )
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout)["error_code"], "private_video")
        self.assertTrue(stderr)

    def test_missing_ytdlp_is_a_setup_error(self) -> None:
        os.environ["KH_YTDLP_CMD"] = str(self.root / "no-such-yt-dlp")
        code, _, stderr = self.run_cli(
            [f"https://youtu.be/{VIDEO_ID}", "--out", str(self.destination)]
        )
        self.assertEqual(code, 2)
        self.assertIn("yt-dlp", stderr)

    def test_unset_destination_is_a_configuration_error(self) -> None:
        saved = {
            name: os.environ.pop(name, None)
            for name in ("KH_VIDEO_PATH", "KH_ARCHIVE_PATH")
        }
        try:
            code, _, stderr = self.run_cli([f"https://youtu.be/{VIDEO_ID}"])
        finally:
            for name, value in saved.items():
                if value is not None:
                    os.environ[name] = value
        self.assertEqual(code, 2)
        self.assertIn("KH_VIDEO_PATH", stderr)


if __name__ == "__main__":
    unittest.main()
