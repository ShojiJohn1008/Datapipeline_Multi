# A-03 Recall PWA デプロイチェックリスト

秘密情報、実ノート、共有トークンはGitへ追加しない。

## 1. GAS中継を導入

1. 既存RecallのApps Scriptプロジェクトへ `gas/Code.gs` を反映する。
2. スクリプトプロパティ `KH_RECALL_TOKEN` に十分長いランダム値を設定する。
3. `installCleanupTrigger` を1回実行し、24時間経過行の定期削除を有効にする。
4. ウェブアプリを再デプロイし、URLを控える。

## 2. Macワーカーを起動

```bash
export KH_VAULT_PATH="<Vaultのローカルパス>"
export KH_RECALL_GAS_URL="<GASウェブアプリURL>"
export KH_RECALL_TOKEN="<スクリプトプロパティと同じ値>"
# 任意: Mac/GASが遅い場合だけ上書き（既定: 要約90秒、GAS通信30秒）
# export KH_RECALL_ANSWER_TIMEOUT=120
# export KH_RECALL_HTTP_TIMEOUT=60
python3 -m knowledge_hub.worker
```

常駐化する前に `--once` で接続設定を確認する。標準エラーにトークン、クエリ、
ノート本文を出力しないこと。

## 3. PWAをデプロイ

VercelプロジェクトのRoot Directoryを `recall/` にする。デプロイ後、「接続設定」に
GAS URLと共有トークンを入力し、この端末に保存する。共有端末には保存しない。

## 4. 四関門

1. `python3 -m unittest discover -s tests -v` がgreen。
2. PWAで既知クエリを検索し、5秒台を目標にカード一覧が出る。
3. 正しいObsidianリンクが開き、「まとめる」で出典番号付き回答が返る。
4. 一般知識だけの質問では外部情報源への案内が返る。
5. 実際の会話・会議で1回使い、結果と所要時間だけを `STATUS.md` に記録する。

## 5. 1週間の意図付きObsidian保存実験（任意）

Supabaseの既存カードを正本として残したまま、Edge Functionで生成済みのQ&AをGASへ
`type: capture`としてキューすると、Mac workerが`Vault/Cards/YYYY/MM`へ1枚だけ保存する。
`intent`（なぜ保存したか）は必須で、Mac側ではLLMを再実行しない。同じSupabase card idの
再送は既存カードとユーザー編集を保持する。

Edge Function側の`OBSIDIAN_RELAY_URL`と`OBSIDIAN_RELAY_TOKEN`を外せば並行保存だけを停止でき、
Supabaseの既存取り込み・データには影響しない。実パス、URL、トークンはGitへ記録しない。
Recall検索は既定で`Cards`だけを対象にする。従来どおりVault全体を検索する場合だけ、Mac workerの
環境へ`KH_RECALL_SEARCH_SCOPE=vault`を設定する。
