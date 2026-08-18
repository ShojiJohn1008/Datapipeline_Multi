"""設定と定数。タイムアウト等の根拠は docs/DESIGN.md §8。"""
from __future__ import annotations

import os
from pathlib import Path

# 既定Vault（Mac実機）。実パスは環境により異なるため起動時に存在チェックする
DEFAULT_VAULT = Path(
    "~/Library/Mobile Documents/iCloud~md~obsidian/Documents/JohnSecondBrain"
).expanduser()

RG_TIMEOUT = 5.0        # ripgrep 1回あたり
EXPAND_TIMEOUT = 10.0   # 語展開LLM
ANSWER_TIMEOUT = 20.0   # 回答生成LLM
TOTAL_BUDGET = 60.0     # 全体フェイルセーフ
MIN_ANSWER_BUDGET = 5.0  # 残予算がこれ未満なら回答生成をスキップ（縮退）

TOP_FILES = 5           # 返信に載せる上位ファイル数
SNIPPET_MARGIN = 10     # ヒット行の前後何行を読むか
SNIPPET_MAX_CHARS = 2000  # 1ファイルあたり抜粋の上限（トークン節約）

# --- B-01 音声レーン（b01-instruction.md）---
# ボイスメモのiCloud実体が同期されるAppleのコンテナ（読み取り専用で使う）
DEFAULT_VOICEMEMO_DIR = Path(
    "~/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings"
).expanduser()
DEFAULT_VOICE_STATE = Path("~/.kh_voice_state.json").expanduser()
# 実機では -m <モデル> 付きで KH_ASR_CMD を設定する（医療用語モデルへの差し替えもここ）
DEFAULT_ASR_CMD = "whisper-cli -nt -np -f"
CARD_TIMEOUT = 60.0       # カード生成LLM 1回あたり
CARD_INPUT_MAX = 12000    # LLMへ渡す文字起こしの上限文字数


class ConfigError(RuntimeError):
    """設定不備（vault不在等）。CLIは exit 2 で返す。"""


def resolve_vault(cli_value: str | None) -> Path:
    """優先順: --vault > $KH_VAULT_PATH > 既定。存在しなければ ConfigError。"""
    if cli_value:
        vault = Path(cli_value).expanduser()
    elif os.environ.get("KH_VAULT_PATH"):
        vault = Path(os.environ["KH_VAULT_PATH"]).expanduser()
    else:
        vault = DEFAULT_VAULT
    if not vault.is_dir():
        raise ConfigError(
            f"Vaultが見つからない: {vault}\n"
            "--vault か環境変数 KH_VAULT_PATH で実パスを指定してください。"
        )
    return vault


def resolve_voicememo(cli_value: str | None) -> Path:
    """優先順: --source > $KH_VOICEMEMO_PATH > Apple既定。存在しなければ ConfigError。"""
    if cli_value:
        source = Path(cli_value).expanduser()
    elif os.environ.get("KH_VOICEMEMO_PATH"):
        source = Path(os.environ["KH_VOICEMEMO_PATH"]).expanduser()
    else:
        source = DEFAULT_VOICEMEMO_DIR
    if not source.is_dir():
        raise ConfigError(
            f"ボイスメモの録音フォルダが見つからない: {source}\n"
            "--source か環境変数 KH_VOICEMEMO_PATH で実パスを指定してください。"
        )
    return source


def resolve_archive(cli_value: str | None) -> Path:
    """優先順: --archive > $KH_ARCHIVE_PATH。既定なし（A-02で確立した実パスが必須）。"""
    if cli_value:
        archive = Path(cli_value).expanduser()
    elif os.environ.get("KH_ARCHIVE_PATH"):
        archive = Path(os.environ["KH_ARCHIVE_PATH"]).expanduser()
    else:
        raise ConfigError(
            "原本ストアArchiveのパスが未設定。\n"
            "--archive か環境変数 KH_ARCHIVE_PATH でA-02のローカル実パスを指定してください。"
        )
    if not archive.is_dir():
        raise ConfigError(f"Archiveが見つからない: {archive}")
    return archive


def default_voice_state() -> Path:
    if os.environ.get("KH_VOICE_STATE"):
        return Path(os.environ["KH_VOICE_STATE"]).expanduser()
    return DEFAULT_VOICE_STATE
