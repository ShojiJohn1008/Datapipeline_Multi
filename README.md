# Datapipeline_Multi — 知識基盤「1つの置き場、複数のレンズ」

分散した記録（LINEメモ・Obsidian・AIチャット・メール…）を索引カードとしてVaultに集約し、
複数のレンズ（検索・タイムライン等）で引き出す個人知識基盤のデータパイプライン置き場。

- 現在地・次の一手: [STATUS.md](STATUS.md)
- 設計: [docs/DESIGN.md](docs/DESIGN.md)
- GPT-5.6作業指示: [docs/HANDOFF_GPT56.md](docs/HANDOFF_GPT56.md)

## MVP-0: Telegram `/find` Vault横断検索

```bash
# 検索（botはexit 0のstdoutをそのままTelegramに返信する）
python3 -m knowledge_hub.find "腫瘍熱について前に調べた？" [--vault PATH] [--no-llm] [--json]

# テスト
python3 -m unittest discover -s tests -v
```

必要環境: Python 3.9+（標準ライブラリのみ）・ripgrep（`brew install ripgrep`）・
LLM経路は `claude -p`（`KH_CLAUDE_CMD` で差し替え可）。
Vaultパスは `--vault` → `$KH_VAULT_PATH` → iCloud既定パスの順で解決。
Mac実機への組み込み、Driveミラーリング、四関門2〜4は
[Mac実機組み込みチェックリスト](docs/MAC_DEPLOY_CHECKLIST.md) を参照。

## Voice Memos 取り込み（実装済み・MacBook Airデプロイ待ち）

iPhone Voice Memos の同期済み `.m4a` を、元ファイルに手を加えず共有Archive
へコピーし、ローカル文字起こし後に検索可能な共有Cardsカードにする。PDFと同じ
ローカルSQLite JobStoreでハッシュ重複、再試行、出力の完了状態を管理する。
初回は過去録音を誤投入しないため、必ず明示的な基準化が必要。

```bash
python3 -m knowledge_hub.voice_memos --baseline-existing --once
# 新規録音を監視するには（KH_ARCHIVE_PATH, KH_VAULT_PATH,
# KH_CARDS_PATH, KH_STATE_DB_PATH, KH_AUDIO_TRANSCRIBE_CMD を必要に応じ設定）
python3 -m knowledge_hub.voice_memos --watch
```

MacBook Air での同期パス・TCC・実機検証を含む手順は
[Voice Memos デプロイチェックリスト](docs/VOICE_MEMOS_DEPLOY_CHECKLIST.md) を参照。

## A-03: Recall PWA Vault横断検索

`recall/` は検索一覧を即時表示し、選択したカードだけを後からまとめるPWAフロント、
`gas/Code.gs` はGoogle Sheets中継、`knowledge_hub.worker` はMac側のポーリングワーカー。

```bash
export KH_RECALL_GAS_URL="<GASウェブアプリURL>"
export KH_RECALL_TOKEN="<共有トークン>"
export KH_VAULT_PATH="<Vaultパス>"
python3 -m knowledge_hub.worker
```

導入手順と受け入れ確認は [A-03デプロイチェックリスト](docs/A03_DEPLOY_CHECKLIST.md) を参照。

## 次段階: ローカルファースト入力パイプライン

原本はArchive、AIが整理した検索可能な知識カードはVaultの`Cards/`、未処理の入力は
Inbox、処理の重複防止・再試行状態はローカル状態DBに分離する。実パスやトークンはGitに
書かず、ローカル環境変数で設定する。実値の設定、Drive/iCloud/Vaultの作成・疎通は開発Macでは
行わず、MacBook Airへのデプロイ段階で行う。状態DBの既定場所は同期競合を避けるためMacローカルの
Application Support配下である。設定例とパスの責務は
[ローカル入力パイプライン](docs/LOCAL_PIPELINE.md) を参照。

最初の縦切りとして、安定したテキストPDFをInboxから1件発見し、SHA-256台帳登録、Poppler本文抽出、
差し替え可能なAgent CLIによる構造化、Archive原本確保、Obsidian Markdownカード生成まで処理できる。

```bash
# 本番パスの設定と実行はMacBook Airへ切り替えた後に行う
brew install poppler
python3 -m knowledge_hub.ingest_pdf --once
```

設定、安全上限、Agent差し替え、OCR未対応時の挙動は
[ローカル入力パイプライン](docs/LOCAL_PIPELINE.md#pdf一本の縦切りmvp)を参照。
