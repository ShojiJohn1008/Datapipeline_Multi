# Datapipeline_Multi — 知識基盤「1つの置き場、複数のレンズ」

分散した記録（LINEメモ・Obsidian・AIチャット・メール…）を索引カードとしてVaultに集約し、
複数のレンズ（検索・タイムライン等）で引き出す個人知識基盤のデータパイプライン置き場。

- 現在地・次の一手: [STATUS.md](STATUS.md)
- バックログ（番号・状態・依存の一元管理）: [BACKLOG.md](BACKLOG.md)
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

## B-01: ボイスメモ→索引カード自動生成（音声レーン）

```bash
# 1パス実行（launchd/cronから数分毎に起動。iPhone側の操作はゼロ）
python3 -m knowledge_hub.voice_lane [--source PATH] [--archive PATH] [--vault PATH] [--no-llm]
```

iCloud同期でMacに届いたボイスメモを検知し、原本をDrive `/Archive/YYYY-MM/` に集約、
**ローカルで**文字起こし（`KH_ASR_CMD`、既定 whisper.cpp。医療用語モデルへ差し替え可）して
タイトル・要約・タグ付きカードを Vault `Cards/` に保存する。同じ録音は二度処理しない
（処理台帳＋カード実在チェック）。詳細は [b01-instruction.md](b01-instruction.md)。
