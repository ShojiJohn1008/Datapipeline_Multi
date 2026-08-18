# knowledge-hub STATUS

フェーズ: MVP-0 Mac実機検証待ち（テスト量産・`--json`・テストループ完了）
完了定義: 会話中の実質問1件に本番botが回答
バックログ全体: [BACKLOG.md](BACKLOG.md)

## 確定事項
- 戦略: 1つの置き場＋複数レンズ／出す側から着手
- 概念モデル: 索引カードv0.1（人が書くフィールドはゼロ）
- 作業ルーティング: 設計・計画・難所 = Claude ／ 実装量産・テストループ = GPT-5.6
  （指示書: docs/HANDOFF_GPT56.md、設計: docs/DESIGN.md）
- bot結合点はCLI 1本のみ: `python3 -m knowledge_hub.find "<クエリ>"`（exit 0のstdoutをそのまま返信）
- LLM経路は `KH_CLAUDE_CMD` で差し替え可能（既定 `claude -p`）

## 検証済み（このリポジトリ内）
- unittest 44本 green（HANDOFFテストマトリクス・既知クエリの受け入れ関門1、
  B-01音声レーンの関門1〔偽ASR・偽LLM〕を含む）
- CLI end-to-end: ヒット系・0件系・vault不在系・偽LLM・`--json` の全経路
- B-01音声レーン: コア実装済み（`knowledge_hub/voice_lane.py`）。ボイスメモ自動取り込み・
  ローカル文字起こし（`KH_ASR_CMD`）・二重処理防止（台帳＋カード実在チェック）

## 四関門
- [x] 1. ユニット: HANDOFFマトリクスを網羅し全テストgreen
- [ ] 2. 結合: 実Telegramから `/find テスト`（確認日・所要時間: 未記録）
- [ ] 3. 実データ: 正しいノートが出典に入る（確認日・ノート名: 未記録）
- [ ] 4. 本番: 会話・会議で1回使いShojiが判定（未実施）

## 次の一手
- Mac担当: [実機チェックリスト](docs/MAC_DEPLOY_CHECKLIST.md) に沿ってDriveミラーリングと `Cards/` 作成（A-02残り）
- Mac担当: 同チェックリストで既存botへ検索分岐を追加し、四関門2・3の結果をこのファイルへ記録
- Mac担当: A-02完了後、[b01-instruction.md](b01-instruction.md) の実機手順でwhisper.cpp導入と
  launchd登録（5分毎）を行い、B-01関門2〜3を検証
- Shoji: 四関門4を1回実施し「役立った / 要改善」を記録
- 全員: B-01稼働開始前にC-05（バックアップ）へ着手
