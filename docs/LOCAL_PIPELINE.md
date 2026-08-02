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

## PDF一本の縦切りMVP

安定したテキストPDFを一度に1件だけ処理するCLIを用意している。開発Macでは一時ディレクトリを使う。
本番のGoogle Drive、iCloud、Vaultのパス設定と実PDFでの確認は、利用者がPCを切り替えた後の
**MacBook Airデプロイ段階**で行う。

```bash
# MacBook Airで後から導入する依存
brew install poppler

# 上記のKH_*_PATHを設定したシェルで、一度に最大1件処理
python3 -m knowledge_hub.ingest_pdf --once
```

処理順は次のとおり。

1. Inbox直下のhidden、テンポラリ、書き込み直後のファイルを除き、`.pdf`だけを発見する
2. SHA-256を計算してローカルJobStoreへ登録し、重複処理を防ぐ
3. `pdftotext`で埋め込みテキストを抽出する
4. AgentProviderへ上限付き本文をstdinで渡し、厳格なJSONカード案を受け取る
5. 原本を`Archive/YYYY/MM/<stem>--<hash8>.pdf`へ確保する
6. `Cards/YYYY/MM/<stem>--<hash8>.md`をatomic writeする
7. ArchiveとCardの両方が成功した後だけJobStoreを`completed`にする

Archive/Cardのパスはジョブ作成時刻とcontent hashから決まる。原本移動後にworkerが停止しても、
stale claimが再投入された後は同じArchive原本から再処理できる。既存Archiveが別hash、または既存Cardが
別content hashなら上書きせず`output_collision`で失敗する。抽出全文はDBにもカードにも保存しない。
AIへ渡す文字数を超えた場合は黙って切り捨てず、`truncated: true`と抽出文字数をカードfrontmatterへ残す。

Archive/Cardは出力先と同じディレクトリのhidden一時ファイルへ書き、`flush`と`fsync`の後、出力先を
直前に再検証して`os.replace`で公開する。hard linkを使わないため、iCloud/Google DriveのFile Provider
領域でも動かせる。既存Cardが同じcontent hashなら、生成後のユーザー編集を守るためbyte単位でそのまま
保持する。ArchiveとCardが両方とも正しいhashで既に存在するクラッシュ復旧では、Agentを再実行せず
`completed`へ短絡する。

JobStoreのclaimはMIME種別で分離され、PDF workerは`application/pdf`だけを取得する。将来同じ台帳へ
音声・動画workerを追加しても、互いのpending jobを誤って処理しない。

Agent CLIは次の優先順で選ぶ。プロンプトはコマンドライン引数ではなくstdinで渡す。metadataと本文は
一つのJSON envelopeにし、本文内のタグ風文字列で境界が変わらないようにする。出力は余分な説明や
コードフェンスを許さないJSON objectとし、必須5項目（`title`, `summary`, `category`, `tags`,
`key_points`）を検証する。

```bash
export KH_AGENT_CMD="claude -p"       # 最優先。将来Codex wrapper等へ差替可能
# export KH_CLAUDE_CMD="claude -p"    # 既存設定との互換fallback
```

主な安全上限は環境変数で調整できる。

| 環境変数 | 既定 | 内容 |
|---|---:|---|
| `KH_PDFTOTEXT_CMD` | `pdftotext` | Popplerコマンド |
| `KH_PDFTOTEXT_TIMEOUT` | 60秒 | 抽出timeout |
| `KH_PDF_MAX_BYTES` | 100 MiB | PDF入力上限 |
| `KH_PDF_MAX_OUTPUT_BYTES` | 16 MiB | 抽出stdout上限 |
| `KH_PDF_MAX_AGENT_CHARS` | 100,000文字 | Agentへ渡す本文上限 |
| `KH_PDF_MIN_TEXT_CHARS` | 40文字 | 埋め込み本文の最低有効文字数 |
| `KH_AGENT_TIMEOUT` | 120秒 | Agent CLI timeout |

スキャンPDFのOCR fallbackはこのMVPでは未実装である。埋め込み本文が閾値未満なら空カードを作らず、
`PdfNeedsOcrError`を`pdf_needs_ocr`として台帳へ記録する。OCRは次段階で独立adapterとして追加する。
常駐監視とlaunchd設定もこの段階には含めない。

## Voice Memos adapter

`knowledge_hub.voice_memos` はiCloud同期済みVoice Memosの `Recordings` を**読み取り専用**で監視するadapterである。共有の `resolve_pipeline_paths()` と `ensure_pipeline_directories()` を使うため、PDFと同じ既存Archive、Cards、ローカル状態DBを使う。Archiveはworkerが作らず、既存の `KH_ARCHIVE_PATH` が必須である。

安定した新規または更新済み `.m4a` はSHA-256で `JobStore` に `media_type='audio/mp4'` として登録される。audio workerはこのMIME種別だけをclaimするので、PDF pending jobには触れない。原本は `Archive/YYYY/MM/voice-memo--<hash8>.m4a`、カードは `Cards/YYYY/MM/voice-memo--<hash8>.md` にatomicに公開し、両方がある後だけcompletedにする。文字起こし失敗時は安全な短いエラーcodeだけをfailed jobへ記録し、Archive原本から再試行する。transcriptや録音内容はSQLiteへ保存しない。

Voice Memos固有の `KH_VOICE_MEMOS_STATE` JSON はJobStoreとは別である。既定は共有SQLiteの隣、`~/Library/Application Support/Datapipeline_Multi/voice_memos_observations.json` である。これは初回baseline、同期遅延cutover、同一statを二回観測する安定性だけを保持し、content hash、attempt、failed/completed、出力パスを保存しない。初回には `--baseline-existing`（推奨）または明示的な `--backfill-existing` が必要である。baseline済みstateへのbackfill指定は拒否するため、意図的な過去分再投入では先にJSONを退避する。

文字起こしコマンドは `KH_AUDIO_TRANSCRIBE_CMD`（または `--transcribe-cmd`）をshlexでargv化し、Archiveコピーのパスを最後の引数として渡す。stdoutだけをtranscriptとして使い、既定7200秒のtimeoutは `KH_AUDIO_TRANSCRIBE_TIMEOUT` または `--transcribe-timeout` で調整できる。`--max-attempts` は失敗jobの最大claim回数（既定3）、`--stale-after-seconds` は強制終了したprocessing claimを再投入する猶予（既定900秒）である。TCC/Full Disk Accessとlaunchdの導入手順は [Voice Memos デプロイチェックリスト](VOICE_MEMOS_DEPLOY_CHECKLIST.md) を参照。

テストは実際のAIや本番Cloudフォルダを使用せず、fake extractor/providerと一時ディレクトリだけで行う。

```bash
python3 -m unittest discover -s tests -v
python3 -W error::ResourceWarning -m unittest discover -s tests -v
```
