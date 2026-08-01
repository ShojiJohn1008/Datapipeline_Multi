# knowledge-hub STATUS

フェーズ: A-02完了／MVP-0本番判定待ち
完了定義: 会話中の実質問1件に本番botが回答

## 確定事項
- 戦略: 1つの置き場＋複数レンズ／出す側から着手
- 概念モデル: 索引カードv0.1（人が書くフィールドはゼロ）
- 作業ルーティング: 設計・計画・難所 = Claude ／ 実装量産・テストループ = GPT-5.6
  （指示書: docs/HANDOFF_GPT56.md、設計: docs/DESIGN.md）
- bot結合点はCLI 1本のみ: `python3 -m knowledge_hub.find "<クエリ>"`（exit 0のstdoutをそのまま返信）
- LLM経路は `KH_CLAUDE_CMD` で差し替え可能（既定 `claude -p`）

## 検証済み（このリポジトリ内）
- unittest 29本 green（HANDOFFテストマトリクスと既知クエリの受け入れ関門1を含む）
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

## 次の一手
- Shoji: 実利用済みの四関門4について「役立った / 要改善」を判定して記録
- 次期候補: [A-03実装指示書](a03-instruction.md) に沿ってRecall PWAのVault横断検索を計画
