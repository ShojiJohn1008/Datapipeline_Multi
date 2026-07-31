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
Mac実機への組み込み、iCloud実Vault設定、任意のDrive移行、四関門2〜4は
[Mac実機組み込みチェックリスト](docs/MAC_DEPLOY_CHECKLIST.md) を参照。
開発用MacBookとTelegram botが稼働するMacBook Airは別マシンであり、実機組み込み作業は
ブランチをMacBook Airへ渡してから同機上で行う。

GitHubへ現在のブランチを渡す場合は、clone済みの**開発Mac**でURL設定込みの
スクリプトを実行する。URLを `<` `>` で囲む必要はない。リポジトリがまだ存在しない
bot MacBook Airではこのスクリプトや `git remote add` を使わず、チェックリストの
`git clone` 手順を使う。ブランチ名は `work` に固定せず、開発Macの
`git branch --show-current`（今回の例では `codex`）に合わせる。最初は `--branch` を付けずにcloneし、
チェックリストの判定コマンドでリモートブランチの公開を確認してから切り替える。

```bash
./scripts/push_to_github.sh
```
