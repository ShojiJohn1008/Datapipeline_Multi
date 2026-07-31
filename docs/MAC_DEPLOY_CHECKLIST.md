# Mac実機組み込みチェックリスト（四関門2〜4 / A-02）

この文書はMac上の既存Telegram botと実Vaultを操作する担当者向け。秘密情報や実ノートは
Gitへ追加せず、結果だけを `STATUS.md` に記録する。

## 0. ブランチを受け取る

```bash
git clone <このリポジトリのURL>
cd Datapipeline_Multi
git switch work
brew install ripgrep
python3 -m unittest discover -s tests -v
```

`docs/HANDOFF_GPT56.md` のガードレールと `docs/DESIGN.md` §9を先に確認する。

## 1. Drive for Desktopをミラーリングにする（A-02残り）

1. メニューバーのGoogle Driveアイコン → 歯車 → **設定** → **基本設定**。
2. **Google ドライブ**を開き、ファイルの同期方法で **ファイルをミラーリング**を選ぶ。
3. 保存先に十分な空き容量があることを確認して確定し、初回同期の完了を待つ。
4. Finderで実ファイルがオフラインでも開けることを確認する。
5. Vault直下に `Cards/` を作成し、Obsidianでそのフォルダが見えることを確認する。
6. 実パスをローカル環境だけに設定する（値はコミットしない）。

```bash
export KH_VAULT_PATH="$HOME/My Drive/<Vault名>"
test -d "$KH_VAULT_PATH/Cards" && rg --version
python3 -m knowledge_hub.find "腫瘍熱" --no-llm
```

## 2. botへ検索分岐だけを追加する

既存応答経路より前に、`/find <q>`、先頭の「探して」、先頭の「前に調べた」だけを捕捉する。
リポジトリrootを `cwd`、タイムアウトを60秒にして
`python3 -m knowledge_hub.find <q>` を引数配列で起動する。シェル文字列連結はしない。
終了0ならstdoutをそのまま返信し、それ以外なら「検索機能でエラー」とstderrの短い要約を返す。
既存ルートは変更しない。

## 3. 四関門2: 実Telegram疎通

1. botを再起動し、実Telegramから `/find テスト` を1回送る。
2. 60秒以内に「出典」または「見つからず。試した語」の正常応答が返れば通過。
3. botログへ時刻・exit code・所要時間を残す（トークンやノート本文は残さない）。
4. `STATUS.md` の関門2を `[x]` にし、確認日と所要時間を書く。

## 4. 四関門3: 実データ検証

1. `/find 腫瘍熱について前に調べた？` のような、答えを知っている実在クエリを送る。
2. 期待するノート名が `📚 出典` に含まれること、Obsidianリンクが対象ノートを開くことを確認。
3. `STATUS.md` の関門3を `[x]` にし、**ノート名だけ**を記録する。

## 5. 四関門4: 本番判定（Shoji担当）

実際の会話または会議で1回だけ検索を使い、Shojiが「役立った / 要改善」を判定する。
日付、クエリの要旨、判定を `STATUS.md` に記録する。役立ったならMVP-0完了、要改善なら
個人情報を除いた症状と期待結果をTODOとして残す。
