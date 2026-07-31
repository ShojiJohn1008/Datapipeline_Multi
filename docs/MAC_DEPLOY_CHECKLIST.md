# bot稼働中のMacBook Airへの組み込みチェックリスト（四関門2〜4 / A-02）

## 対象マシン（重要）

このリポジトリを編集しているMacBookと、Telegram botが稼働している **別のMacBook Air** は
別マシンである。以下では前者を「開発Mac」、後者を「bot MacBook Air」と呼ぶ。

- 開発Mac: 実装、テスト、commit、ブランチのpushまでを行う。
- bot MacBook Air: ブランチの取得、Driveミラーリング、実Vault検索、bot結合、四関門2〜4を行う。
- Telegramの実疎通を開発Macで試さない。botトークンや実Vaultを開発Macへコピーしない。

秘密情報や実ノートはGitへ追加せず、検証結果だけを `STATUS.md` に記録する。

## 0. 開発Macからブランチを渡す

開発Macで、今回のcommitを共有リモートへpushする。次の変数は実際の名前に置き換える。

```bash
REMOTE=origin
BRANCH=work
git status --short --branch
git push "$REMOTE" "$BRANCH"
```

## 1. bot MacBook Airでブランチを受け取る

ここから先のコマンドとGUI操作は、明記がない限り **bot MacBook Air上** で行う。
既にclone済みなら新しくcloneせず、既存checkoutでfetchして切り替える。

```bash
cd Datapipeline_Multi
REMOTE=origin
BRANCH=work
git status --short --branch        # bot側の未commit変更がないことを確認
git fetch "$REMOTE"
git switch "$BRANCH"
git pull --ff-only "$REMOTE" "$BRANCH"
brew install ripgrep
python3 -m unittest discover -s tests -v
```

未cloneの場合だけ、bot MacBook Air上でリポジトリURLとブランチ名を指定して
`git clone --branch "$BRANCH" "$REPOSITORY_URL"` を実行する。
`docs/HANDOFF_GPT56.md` のガードレールと `docs/DESIGN.md` §9を先に確認する。

## 2. bot MacBook AirのDriveをミラーリングにする（A-02残り）

1. メニューバーのGoogle Driveアイコン → 歯車 → **設定** → **基本設定**。
2. **Google ドライブ**を開き、ファイルの同期方法で **ファイルをミラーリング**を選ぶ。
3. 保存先に十分な空き容量があることを確認して確定し、初回同期の完了を待つ。
4. Finderで実ファイルがオフラインでも開けることを確認する。
5. Vault直下に `Cards/` を作成し、Obsidianでそのフォルダが見えることを確認する。
6. 実パスをbotプロセスの起動環境へ設定する（値はコミットしない）。ターミナルの一時的な
   `export` だけでは、launchdやtmuxで再起動したbotへ引き継がれない場合がある。

```bash
export KH_VAULT_PATH="$HOME/My Drive/<Vault名>"
test -d "$KH_VAULT_PATH/Cards" && test -r "$KH_VAULT_PATH" && rg --version
python3 -m knowledge_hub.find "腫瘍熱" --no-llm
```

この確認が成功したら、既存botの起動スクリプト、tmux起動コマンド、またはlaunchd plistの
`EnvironmentVariables`のうち、実際に使っている1か所へ `KH_VAULT_PATH` を設定してbotを再起動する。

## 3. bot MacBook Air上の既存botへ検索分岐だけを追加する

既存応答経路より前に、`/find <q>`、先頭の「探して」、先頭の「前に調べた」だけを捕捉する。
リポジトリrootを `cwd`、タイムアウトを60秒にして
`python3 -m knowledge_hub.find <q>` を引数配列で起動する。シェル文字列連結はしない。
終了0ならstdoutをそのまま返信し、それ以外なら「検索機能でエラー」とstderrの短い要約を返す。
既存ルートは変更しない。

## 4. 四関門2: 実Telegram疎通

1. botを再起動し、実Telegramから `/find テスト` を1回送る。
2. 60秒以内に「出典」または「見つからず。試した語」の正常応答が返れば通過。
3. botログへ時刻・exit code・所要時間を残す（トークンやノート本文は残さない）。
4. `STATUS.md` の関門2を `[x]` にし、確認日と所要時間を書く。

## 5. 四関門3: 実データ検証

1. `/find 腫瘍熱について前に調べた？` のような、答えを知っている実在クエリを送る。
2. 期待するノート名が `📚 出典` に含まれること、Obsidianリンクが対象ノートを開くことを確認。
3. `STATUS.md` の関門3を `[x]` にし、**ノート名だけ**を記録する。

## 6. 四関門4: 本番判定（Shoji担当）

実際の会話または会議で1回だけ検索を使い、Shojiが「役立った / 要改善」を判定する。
日付、クエリの要旨、判定を `STATUS.md` に記録する。役立ったならMVP-0完了、要改善なら
個人情報を除いた症状と期待結果をTODOとして残す。
