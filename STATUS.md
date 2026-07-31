# knowledge-hub STATUS

フェーズ: MVP-0 Mac実機検証待ち（テスト量産・`--json`・テストループ完了）
完了定義: 会話中の実質問1件に本番botが回答

## 確定事項
- 戦略: 1つの置き場＋複数レンズ／出す側から着手
- 概念モデル: 索引カードv0.1（人が書くフィールドはゼロ）
- 作業ルーティング: 設計・計画・難所 = Claude ／ 実装量産・テストループ = GPT-5.6
  （指示書: docs/HANDOFF_GPT56.md、設計: docs/DESIGN.md）
- bot結合点はCLI 1本のみ: `python3 -m knowledge_hub.find "<クエリ>"`（exit 0のstdoutをそのまま返信）
- `/find` は臨床知識を含む過去ノートの検索・引用を許可するが、患者識別情報・患者別臨床記録は
  Telegramへ出さず、最終判断はShojiが行う
- LLM経路は `KH_CLAUDE_CMD` で差し替え可能（既定 `claude -p`）
- 開発中のMacBookとTelegram bot稼働中のMacBook Airは別マシン。実機組み込み、iCloud実Vault設定、
  実Vault検証はすべてbot MacBook Air側で行う

## 検証済み（このリポジトリ内）
- unittest 29本 green（HANDOFFテストマトリクスと既知クエリの受け入れ関門1を含む）
- CLI end-to-end: ヒット系・0件系・vault不在系・偽LLM・`--json` の全経路

## 四関門
- [x] 1. ユニット: HANDOFFマトリクスを網羅し全テストgreen
- [x] 2. 結合: 実Telegramから `/find` 応答を確認（2026-07-31、所要時間: 未記録）
- [x] 3. 実データ: 正しいノートが出典に入ることを確認（2026-07-31、ノート名: 腫瘍熱の診断と治療（ナイキサンテスト）って何だっけ）
- [ ] 4. 本番: 会話・会議で1回使いShojiが判定（未実施）

## 次の一手
- 開発Mac担当: このブランチを共有リモートへpushし、bot MacBook Airへ渡す
- bot MacBook Air担当: [実機チェックリスト](docs/MAC_DEPLOY_CHECKLIST.md) に沿ってiCloud実Vaultを設定し、`Cards/` を作成。Drive移行は別途方針決定（A-02残り）
- bot MacBook Air担当: Obsidian URIのタップ不可を確認。MVP-0は長押しコピー運用とし、HTTPSリダイレクトは別タスク
- Shoji: 四関門4を1回実施し「役立った / 要改善」を記録
