"""設定と定数。タイムアウト等の根拠は docs/DESIGN.md §8。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# 既定Vault（Mac実機）。実パスは環境により異なるため起動時に存在チェックする
DEFAULT_VAULT = Path(
    "~/Library/Mobile Documents/iCloud~md~obsidian/Documents/JohnSecondBrain"
).expanduser()
# SQLiteはDrive/iCloud同期領域に置くと複数端末の更新が競合しうるため、Macローカルに置く。
DEFAULT_STATE_DB = Path(
    "~/Library/Application Support/Datapipeline_Multi/jobs.sqlite3"
).expanduser()

RG_TIMEOUT = 5.0        # ripgrep 1回あたり
EXPAND_TIMEOUT = 10.0   # 語展開LLM
ANSWER_TIMEOUT = 20.0   # 回答生成LLM
TOTAL_BUDGET = 60.0     # 全体フェイルセーフ
MIN_ANSWER_BUDGET = 5.0  # 残予算がこれ未満なら回答生成をスキップ（縮退）

# Recallの選択カード要約は最大10件の抜粋をCLIへ渡すため、通常の/find回答より
# 長い猶予を持たせる。PWA側はこれより長く待つ必要がある。
RECALL_ANSWER_TIMEOUT = 90.0
RECALL_HTTP_TIMEOUT = 30.0

TOP_FILES = 5           # 返信に載せる上位ファイル数
SNIPPET_MARGIN = 10     # ヒット行の前後何行を読むか
SNIPPET_MAX_CHARS = 2000  # 1ファイルあたり抜粋の上限（トークン節約）


class ConfigError(RuntimeError):
    """設定不備（vault不在等）。CLIは exit 2 で返す。"""


def positive_float_env(name: str, default: float) -> float:
    """正の秒数を環境変数から読む。秘密値を含まない設定エラーだけを返す。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} は数値で指定してください") from exc
    if value <= 0:
        raise ConfigError(f"{name} は正の数で指定してください")
    return value


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


@dataclass(frozen=True)
class PipelinePaths:
    """ローカルファースト取り込みで扱うパスの境界。

    `resolve_pipeline_paths` は読むだけで、ディレクトリを作らない。書き込み前に
    `ensure_pipeline_directories` を明示的に呼ぶこと。
    """

    vault: Path
    archive: Path
    inbox: Path
    cards: Path
    state_db: Path


def _configured_path(value: str | None, env_name: str) -> Path | None:
    """明示値を環境変数より優先して、展開済みPathへ変換する。"""
    configured = value or os.environ.get(env_name)
    return Path(configured).expanduser() if configured else None


def _absolute(path: Path) -> Path:
    """存在しない派生パスも扱える、リンクを解決した絶対パスを返す。"""
    return path.resolve(strict=False)


def _require_directory(path: Path, label: str, env_name: str) -> Path:
    if not path.is_dir():
        raise ConfigError(
            f"{label}が見つからない: {path}\n"
            f"環境変数 {env_name} で存在するローカルパスを指定してください。"
        )
    return path


def _validate_optional_directory(path: Path, label: str) -> None:
    """既存ならディレクトリであることだけを確認し、未作成は許容する。"""
    if path.exists() and not path.is_dir():
        raise ConfigError(f"{label}はディレクトリではない: {path}")


def resolve_pipeline_paths(
    vault_value: str | None = None,
    *,
    archive_value: str | None = None,
    inbox_value: str | None = None,
    cards_value: str | None = None,
    state_db_value: str | None = None,
) -> PipelinePaths:
    """取り込みパイプラインのパスを検証して返す（作成はしない）。

    優先順は各項目で「関数の明示値 > 対応する環境変数 > 派生既定値」。
    Vault は既存の ``resolve_vault`` 契約をそのまま利用する。Archiveだけは原本の
    保存先であり、誤ってVault内へ作らないよう明示設定を必須とする。

    - ``KH_INBOX_PATH`` 未指定: ``<archive>/Inbox``
    - ``KH_CARDS_PATH`` 未指定: ``<vault>/Cards``
    - ``KH_STATE_DB_PATH`` 未指定:
      ``~/Library/Application Support/Datapipeline_Multi/jobs.sqlite3``
    """
    vault = _absolute(resolve_vault(vault_value))

    archive_configured = _configured_path(archive_value, "KH_ARCHIVE_PATH")
    if archive_configured is None:
        raise ConfigError(
            "Archiveパスが未設定です。環境変数 KH_ARCHIVE_PATH で、"
            "原本を保存する既存ディレクトリを指定してください。"
        )
    archive = _require_directory(
        _absolute(archive_configured), "Archive", "KH_ARCHIVE_PATH"
    )

    inbox = _configured_path(inbox_value, "KH_INBOX_PATH")
    cards = _configured_path(cards_value, "KH_CARDS_PATH")
    state_db = _configured_path(state_db_value, "KH_STATE_DB_PATH")
    inbox = _absolute(inbox) if inbox is not None else archive / "Inbox"
    cards = _absolute(cards) if cards is not None else vault / "Cards"
    state_db = (
        _absolute(state_db)
        if state_db is not None
        else _absolute(DEFAULT_STATE_DB)
    )

    _validate_optional_directory(inbox, "Inbox")
    _validate_optional_directory(cards, "Cards")
    if state_db.exists() and state_db.is_dir():
        raise ConfigError(f"状態DBのパスがディレクトリです: {state_db}")
    if state_db.parent.exists() and not state_db.parent.is_dir():
        raise ConfigError(f"状態DBの親がディレクトリではない: {state_db.parent}")

    return PipelinePaths(vault, archive, inbox, cards, state_db)


def ensure_pipeline_directories(paths: PipelinePaths) -> PipelinePaths:
    """書き込み前にInbox/Cards/状態DBの親を作成する。

    VaultとArchiveは正本のルートなので、ここでは作成しない。呼び出し側は必ず先に
    ``resolve_pipeline_paths`` を実行し、既存のVault/Archiveを検証する。
    """
    _require_directory(paths.vault, "Vault", "KH_VAULT_PATH")
    _require_directory(paths.archive, "Archive", "KH_ARCHIVE_PATH")
    paths.inbox.mkdir(parents=True, exist_ok=True)
    paths.cards.mkdir(parents=True, exist_ok=True)
    paths.state_db.parent.mkdir(parents=True, exist_ok=True)
    return paths
