#!/usr/bin/env python3
"""mlx-whisper を音声1ファイル用の stdout-only CLI にする任意のラッパー。"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    values = argv if argv is not None else sys.argv[1:]
    if len(values) != 1:
        print("使い方: transcribe_with_mlx_whisper.py AUDIO_FILE", file=sys.stderr)
        return 2
    audio = Path(values[0])
    if not audio.is_file():
        print("音声ファイルが見つかりません", file=sys.stderr)
        return 2
    try:
        from mlx_whisper import transcribe
    except ImportError:
        print("mlx-whisper が未導入です。venv内で `pip install mlx-whisper` を実行してください。",
              file=sys.stderr)
        return 2
    model = os.environ.get("KH_MLX_WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo")
    language = os.environ.get("KH_MLX_WHISPER_LANGUAGE", "ja")
    try:
        result = transcribe(str(audio), path_or_hf_repo=model, language=language)
        text = result["text"]
    except Exception as exc:
        print("mlx-whisper の文字起こしに失敗しました: {}".format(exc), file=sys.stderr)
        return 1
    print(text.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
