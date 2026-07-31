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
