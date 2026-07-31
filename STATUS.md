# knowledge-hub STATUS

フェーズ: MVP-0 実装中（設計・コア実装・シードテスト完了 → GPT-5.6量産フェーズへ）
完了定義: 会話中の実質問1件に本番botが回答

## 確定事項
- 戦略: 1つの置き場＋複数レンズ／出す側から着手
- 概念モデル: 索引カードv0.1（人が書くフィールドはゼロ）
- 作業ルーティング: 設計・計画・難所 = Claude ／ 実装量産・テストループ = GPT-5.6
  （指示書: docs/HANDOFF_GPT56.md、設計: docs/DESIGN.md）
- bot結合点はCLI 1本のみ: `python3 -m knowledge_hub.find "<クエリ>"`（exit 0のstdoutをそのまま返信）
- LLM経路は `KH_CLAUDE_CMD` で差し替え可能（既定 `claude -p`）

## 検証済み（このリポジトリ内）
- unittest 10本 green（既知クエリ→期待ファイルの受け入れ関門1を含む）
- CLI end-to-end: ヒット系・0件系・vault不在系・偽LLMでの全経路

## 次の一手
- GPT-5.6: HANDOFF T1〜T5（テスト量産→--json→ループ→Mac実機組み込み）
- Shoji: Mac側で clone → `rg` 確認 → 四関門2（実Telegram疎通）→ 3（実データ）→ 4（本番1回で役立ち判定）
- A-02残り: Drive for Desktopミラーリング設定・Vault `Cards/` 作成（Mac手作業）
