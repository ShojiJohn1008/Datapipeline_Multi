# knowledge-hub STATUS

フェーズ: A-03実装済み／実機デプロイ待ち
完了定義: 会話中の実質問1件にRecall PWAの一覧が数秒で出て、役立ったと判定

## 確定事項
- 戦略: 1つの置き場＋複数レンズ／出す側から着手
- 概念モデル: 索引カードv0.1（人が書くフィールドはゼロ）
- 作業ルーティング: 設計・計画・難所 = Claude ／ 実装量産・テストループ = GPT-5.6
  （指示書: docs/HANDOFF_GPT56.md、設計: docs/DESIGN.md）
- bot結合点はCLI 1本のみ: `python3 -m knowledge_hub.find "<クエリ>"`（exit 0のstdoutをそのまま返信）
- LLM経路は `KH_CLAUDE_CMD` で差し替え可能（既定 `claude -p`）
- 開発中のMacBookとTelegram bot稼働中のMacBook Airは別マシン。実機組み込み、Driveミラーリング、
  実Vault検証はすべてbot MacBook Air側で行う

## 検証済み（このリポジトリ内）
- unittest 34本 green（A-03ワーカーとHANDOFFマトリクス、既知クエリの受け入れ関門1を含む）
- CLI end-to-end: ヒット系・0件系・vault不在系・偽LLM・`--json` の全経路

## 四関門
- [x] 1. ユニット: HANDOFFマトリクスを網羅し全テストgreen
- [x] 2. 結合: 実Telegramから `/find テスト`（確認日: 2026-08-01、所要時間: 未計測）
- [x] 3. 実データ: 正しいノートが出典に入る（確認日: 2026-08-01、ノート名: 腫瘍熱の診断と治療（ナイキサンテスト）って何だっけ）
- [ ] 4. 本番: 会話・会議で利用済み、Shojiの「役立った / 要改善」判定は未記録

## できたこと
- A-02完了（2026-08-01）
  - Google Drive `Archive/2026-08/`へのスマホ投入とMacへのリアルタイム同期を確認
  - 同期したM4A音声を `file` が認識し、ローカル実体として読み取り可能であることを確認
  - Obsidian Vault直下の `Cards/` とripgrepの動作を確認
  - 監視対象の実パスはMacローカルの `$KH_ARCHIVE_PATH` に設定（実値はコミットしない）
- A-03ローカル実装（2026-08-01）
  - LLMを呼ばないfrontmatterカード検索と、選択カードだけを対象にした出典番号付き要約
  - GAS・Google Sheets中継と3秒ポーリングのMac常駐ワーカー
  - 検索、Obsidian直リンク、対象選択、二段目要約を備えたRecall PWA

## 次の一手
- Shoji: 実利用済みの四関門4について「役立った / 要改善」を判定して記録
- Mac担当: [A-03デプロイチェックリスト](docs/A03_DEPLOY_CHECKLIST.md) に沿ってGAS・ワーカー・PWAを実機接続
- Shoji: Recall PWAで四関門2〜4を実施して結果を記録
