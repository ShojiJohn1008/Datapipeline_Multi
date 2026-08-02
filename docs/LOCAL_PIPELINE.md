# ローカルファースト入力パイプラインのパス設定

この段階ではObsidian Vaultを知識の正本、Archiveを元ファイルの正本として扱う。実パス、
トークン、ノート本文をGitへ追加しない。SupabaseやRecallの既存判断はこの設定では変更しない。
本番のGoogle Drive、iCloud、Vault実体の設定・作成・疎通確認は**MacBook Airのデプロイ段階**で
行う。開発Macでは実パスを探索・作成せず、テスト用の一時ディレクトリだけを使う。

## 責務

| 場所 | 内容 | 作成責務 |
|---|---|---|
| Vault (`KH_VAULT_PATH`) | Obsidian全体。既存ディレクトリが必須 | 利用者 |
| Cards (`KH_CARDS_PATH`) | AIが生成する検索可能なMarkdownカード。既定は`<Vault>/Cards` | workerが明示的に作成 |
| Archive (`KH_ARCHIVE_PATH`) | PDF、画像、音声、動画などの原本。既存ディレクトリが必須 | 利用者 / Drive同期 |
| Inbox (`KH_INBOX_PATH`) | 未処理の入力。既定は`<Archive>/Inbox` | workerが明示的に作成 |
| 状態DB (`KH_STATE_DB_PATH`) | 処理済み判定、再試行などのローカル状態。既定は`~/Library/Application Support/Datapipeline_Multi/jobs.sqlite3` | workerが明示的に親を作成 |

`resolve_pipeline_paths()` は設定確認だけを行い、ディレクトリを作成しない。書き込みする
workerは確認後に `ensure_pipeline_directories()` を呼ぶ。これにより、タイプミスしたVaultや
Archiveを勝手に新規作成しない。

## ローカル設定例

シェル起動時に読み込むローカル専用ファイル（例: `~/.zshrc` またはLaunchAgentの環境設定）へ
設定する。値をこのリポジトリの`.env`やGit管理ファイルへ書かない。以下の実値設定は
**MacBook Airへデプロイするときだけ**行う。

```bash
export KH_VAULT_PATH="$HOME/Library/Mobile Documents/iCloud~md~obsidian/Documents/JohnSecondBrain"
export KH_ARCHIVE_PATH="$HOME/Library/CloudStorage/GoogleDrive-<account>/My Drive/Archive"

# 次の3つは上の既定場所でよければ不要
# export KH_INBOX_PATH="$KH_ARCHIVE_PATH/Inbox"
# export KH_CARDS_PATH="$KH_VAULT_PATH/Cards"
# export KH_STATE_DB_PATH="$HOME/Library/Application Support/Datapipeline_Multi/jobs.sqlite3"
```

`KH_VAULT_PATH` は既存の検索CLIと同じ優先順（明示`--vault` > 環境変数 > iCloud既定）で解決する。
取り込みパイプラインでは`KH_ARCHIVE_PATH`を必須にしている。Archiveは原本の保存先なので、
設定漏れでVault内などに作られることを防ぐためである。

状態DBはSQLiteの書き込み状態を保持するため、既定ではGoogle DriveやiCloudなどの同期領域には
置かない。複数端末の同期中にSQLiteファイルが競合・破損するリスクを避けるため、Macローカルの
Application Support配下を使う。別の場所が必要な場合だけ`KH_STATE_DB_PATH`で明示指定する。

## ローカルジョブ台帳

`knowledge_hub.job_store.JobStore` はInboxを走査するworkerのためのSQLite台帳である。DB親の作成は
行わないので、書き込み前に必ず`ensure_pipeline_directories(paths)`を呼ぶ。内容のSHA-256を主キーに
するため、同じファイルが別名で再びInboxへ届いても二重処理しない。

```text
pending --claim--> processing --success--> completed
                         |\
                         | \--failure--> failed --claim (attempts < 上限)--> processing
                         |
                         \--stale worker recovery--> pending
```

`completed` は再claimされない。claimはSQLiteの`BEGIN IMMEDIATE`で直列化されるため、複数workerが
同じジョブを処理するのを防ぐ。台帳にはハッシュ、パス、ファイル名、サイズ、MIME種別、状態、試行回数、
短いエラー、Archive/Cardの出力パス、時刻だけを保存する。**原本バイト列、PDF抽出本文、Markdown本文、
LLMトークンや認証情報は保存しない。**

通常の`pending`は`failed`の再試行より先にclaimするため、一時失敗が新しい入力を塞がない。`mark_failed()`へ
渡すエラーにはトークン、Bearer認証値、APIキー、秘密情報、原文を含めてはならない。典型的な credential
表現は台帳側でも`[REDACTED]`に置換するが、これは呼び出し側の秘匿契約を補助するための防御である。

## 開発時の利用

```python
from knowledge_hub.config import ensure_pipeline_directories, resolve_pipeline_paths

paths = resolve_pipeline_paths()  # 検証のみ。作成しない
ensure_pipeline_directories(paths)  # 実際に書き込む直前に呼ぶ
```

明示値を渡す場合は、各項目で「関数引数 > 対応する環境変数 > 派生既定値」の順に優先される。
VaultとArchiveが存在しない場合、またはInbox/Cardsが既存ファイルだった場合は`ConfigError`で停止する。
