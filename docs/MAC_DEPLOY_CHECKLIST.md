# bot稼働中のMacBook Airへの組み込みチェックリスト（四関門2〜4 / A-02）

## 対象マシン（重要）

このリポジトリを編集しているMacBookと、Telegram botが稼働している **別のMacBook Air** は
別マシンである。以下では前者を「開発Mac」、後者を「bot MacBook Air」と呼ぶ。

- 開発Mac: 実装、テスト、commit、ブランチのpushまでを行う。
- bot MacBook Air: ブランチの取得、iCloud実Vault検索、bot結合、四関門2〜4を行う。
  Google Driveミラーリングは、Vaultを実際にGoogle Driveへ移行すると決めた場合だけ別途行う。
- Telegramの実疎通を開発Macで試さない。botトークンや実Vaultを開発Macへコピーしない。

秘密情報や実ノートはGitへ追加せず、検証結果だけを `STATUS.md` に記録する。

## 0. 開発Macからブランチを渡す

> **この節をbot MacBook Airでは実行しない。** `git remote add` と `git push` は、すでに
> `.git` が存在し今回の変更が入っている **開発Macのリポジトリ内** で実行する。
> プロンプトが `~ %` のときはホームディレクトリにいるため、その場所で実行すると
> `fatal: not a git repository` になる。

開発Macで、今回のcommitを共有リモートへpushする。ブランチ名を `work` と決め打ちせず、
`git branch --show-current` の結果を使う。たとえば `codex` と表示されたら受け渡すブランチは
`codex` である。リポジトリURLの前後に
`<` と `>` は入力しない。これらは説明上の「置き換え箇所」を示す記号であり、zshでは
リダイレクトとして解釈されて `parse error` になる。

このリポジトリに含まれるスクリプトなら、URLを入力せずに実行できる。

開発MacのFinderで `Datapipeline_Multi` フォルダを探し、ターミナルへ `cd `（末尾に半角空白）
と入力してから、そのフォルダをFinderからターミナルへドラッグ＆ドロップしてReturnを押す。
プロンプトの場所が移動したことを確認して実行する。

```bash
git status --short --branch
git branch --show-current
./scripts/push_to_github.sh
```

手動で設定する場合は、次の行をそのまま実行する（URLを囲む山括弧は付けない）。

```bash
git remote add origin https://github.com/ShojiJohn1008/Datapipeline_Multi.git
git status --short --branch
BRANCH="$(git branch --show-current)"
git push --set-upstream origin "$BRANCH"
```

すでに `origin` が存在して `error: remote origin already exists.` と表示された場合だけ、
`git remote add` の代わりに以下を使う。

```bash
git remote set-url origin https://github.com/ShojiJohn1008/Datapipeline_Multi.git
BRANCH="$(git branch --show-current)"
git push --set-upstream origin "$BRANCH"
```

## 1. bot MacBook Airでブランチを受け取る

ここから先のコマンドとGUI操作は、明記がない限り **bot MacBook Air上** で行う。
今回のように、デスクトップにもホームにも `Datapipeline_Multi` がまだない場合は、
`git remote add` ではなく、最初に **clone** する。最初のcloneでは、存在が未確認の
`--branch work` を指定しない。今回、開発Macが `## codex...origin/codex` および `codex` と
表示している場合、`codex`はすでにGitHubへpush済みなので、bot MacBook Airではそのブランチを受け取る。

```bash
cd ~/Desktop
git clone https://github.com/ShojiJohn1008/Datapipeline_Multi.git
cd Datapipeline_Multi
if git ls-remote --exit-code --heads origin codex | grep -q 'refs/heads/codex$'; then
  echo "OK: codexブランチは公開済みです"
else
  echo "停止: cloneは成功しましたが、codexブランチはGitHubにありません"
fi
```

`Receiving objects: 100%` と `Resolving deltas: 100%` が出れば、リポジトリ本体のcloneは成功している。
続いて `OK: codexブランチは公開済みです` と表示されたら、次へ進む。

```bash
git fetch origin codex
git switch --track origin/codex
python3 -m unittest discover -s tests -v
```

`停止: cloneは成功しましたが、codexブランチはGitHubにありません` と表示された場合、
`codex`はGitHubにまだ存在しない。
**bot MacBook Air側では解決できないため、そこで停止する。** 開発Mac側で節0のpushが成功した後、
bot MacBook Air側のclone済みフォルダで次を実行する（cloneし直す必要はない）。

```bash
cd ~/Desktop/Datapipeline_Multi
git fetch origin codex
git switch --track origin/codex
python3 -m unittest discover -s tests -v
```

`fatal: Remote branch <名前> not found` は、MacBook Airやcloneコマンドの故障ではなく、GitHubに
その名前のブランチが未公開であることを示す。開発Macに表示された実際のブランチ名を使う。

すでにclone済みの場合だけ、新しくcloneせず、既存checkoutでfetchして切り替える。

```bash
cd Datapipeline_Multi
REMOTE=origin
BRANCH=codex
git status --short --branch        # bot側の未commit変更がないことを確認
git fetch "$REMOTE"
git switch "$BRANCH"
git pull --ff-only "$REMOTE" "$BRANCH"
brew install ripgrep
python3 -m unittest discover -s tests -v
```

`docs/HANDOFF_GPT56.md` のガードレールと `docs/DESIGN.md` §9を先に確認する。

## 2. bot MacBook AirでiCloud上の実Vaultを設定する

現在のObsidian VaultがiCloudにあるなら、Google Drive内を探したり、検索のためだけに移動したり
しない。まずbot MacBook AirでiCloud DriveとObsidianの同期完了を確認し、Vault候補を表示する。

```bash
find "$HOME/Library/Mobile Documents/iCloud~md~obsidian/Documents" \
  -type d -name .obsidian -prune -exec dirname {} \; 2>/dev/null
```

何も表示されなければ、Vaultがないと即断せず、iCloudのObsidianコンテナ自体とMarkdownを分けて確認する。

```bash
ICLOUD_OBSIDIAN="$HOME/Library/Mobile Documents/iCloud~md~obsidian/Documents"
if test -d "$ICLOUD_OBSIDIAN"; then
  echo "OK: iCloud Obsidianフォルダはあります"
  find "$ICLOUD_OBSIDIAN" -type f -name '*.md' -print -quit
else
  echo "NG: iCloud ObsidianフォルダがこのMacにありません"
fi
mdfind "kMDItemFSName == '.obsidian'" 2>/dev/null
```

- `NG` の場合: Macの **システム設定 → Apple Account → iCloud → iCloud Drive** をオンにし、
  「iCloud Driveに同期しているアプリ」でObsidianを許可する。Obsidianを起動し、同じApple Accountの
  iCloud Driveにある既存Vaultを「保管庫としてフォルダを開く」で一度開いて同期を待つ。
- `OK` だが `.md` が1件も表示されない場合: iCloud同期が未完了、別のApple Account、または
  このMacで既存Vaultをまだ開いていない可能性を確認する。
- `.md` のパスは表示されるが `.obsidian` が見つからない場合: 表示されたMarkdownの上位フォルダを
  Finderで確認し、Obsidianの「保管庫としてフォルダを開く」で正しいVaultを開いてから再実行する。

同期確認中に新しい空のVaultを同名で作らない。既存Vaultが見えるまで `Cards/` 作成には進まない。

表示された実Vaultのパスを引用符内へ貼り付ける。既定のVault名が `JohnSecondBrain` なら、
リポジトリの既定値も同じiCloudパスを使う。

```bash
export KH_VAULT_PATH="$HOME/Library/Mobile Documents/iCloud~md~obsidian/Documents/JohnSecondBrain"
test -d "$KH_VAULT_PATH/.obsidian" && echo "OK: Vault" || echo "NG: Vaultパスを確認"
mkdir -p "$KH_VAULT_PATH/Cards"
test -d "$KH_VAULT_PATH/Cards" && test -r "$KH_VAULT_PATH" && rg --version
python3 -m knowledge_hub.find "腫瘍熱" --no-llm
```

CLIは開始時に `検索中です（最大60秒）…` をstderrへ表示する。通常は数秒で完了する。
60秒を超えて応答しない旧版では `Control-C` で停止し、iCloudプレースホルダ走査を
時間制限付きにした最新版へ更新してから再実行する。

FinderとObsidianの両方で `Cards/` が見えることも確認する。iCloudがファイル本体を退避して
`.icloud`プレースホルダだけにしている場合、CLIはダウンロードを要求して同期待ちを通知する。

この確認が成功したら、実パスをbotプロセスの起動環境へ設定する（値はコミットしない）。
一時的な `export` だけでは、launchdやtmuxで再起動したbotへ引き継がれない場合がある。
既存botの起動スクリプト、tmux起動コマンド、またはlaunchd plistの `EnvironmentVariables` のうち、
実際に使っている1か所へ `KH_VAULT_PATH` を設定してbotを再起動する。

Claude Channels版の秘書は、通常の `claude` ではなく次の引数を維持して起動する。

```bash
claude -c --channels plugin:telegram@claude-plugins-official
```

起動スクリプトを作る場合も、最後を
`exec claude -c --channels plugin:telegram@claude-plugins-official` とする。`--channels` を落とすと
Claudeプロセスは動いていてもTelegramメッセージを受信しない。

### Google Driveミラーリング（A-02を別途進める場合）

Google Drive for Desktopのミラーリングは、iCloud Vaultを自動的にGoogle Driveへ移す設定ではない。
保存先をGoogle Driveへ変更・移行する方針を決めた場合だけ、Driveの設定で
**ファイルをミラーリング**を選び、移行後のVaultをObsidianで開いてから `KH_VAULT_PATH` を更新する。
同じVaultをiCloudとGoogle Driveで同時に同期させない。

## 3. bot MacBook Air上の既存botへ検索分岐だけを追加する

既存応答経路より前に、`/find <q>`、先頭の「探して」、先頭の「前に調べた」だけを捕捉する。
リポジトリrootを `cwd`、タイムアウトを60秒にして
`python3 -m knowledge_hub.find <q>` を引数配列で起動する。シェル文字列連結はしない。
終了0ならstdoutをそのまま返信し、それ以外なら「検索機能でエラー」とstderrの短い要約を返す。
既存ルートは変更しない。

### 検索対象の境界

`/find` は過去ノートの検索・引用機能であり、診断や意思決定の自動化ではない。Shojiの明示的な
運用判断により、一般的な臨床知識や過去に調べた医学トピック（例: `腫瘍熱`）も検索対象にできる。
ただし、患者を識別できる情報、患者別の臨床記録、認証情報はTelegramへ出さない。回答はVault内の
既存記載と出典に限定し、最終判断はShojiが行う。既存botの「非臨床」境界を変更する場合も、
この「臨床知識の検索は可／患者固有情報は不可」の区別を `CLAUDE.md` に明記してから再起動する。

## 4. 四関門2: 実Telegram疎通

1. botを再起動し、実Telegramから `/find テスト` を1回送る。
2. 60秒以内に「出典」または「見つからず。試した語」の正常応答が返れば通過。
3. botログへ時刻・exit code・所要時間を残す（トークンやノート本文は残さない）。
4. `STATUS.md` の関門2を `[x]` にし、確認日と所要時間を書く。

## 5. 四関門3: 実データ検証

1. `/find 腫瘍熱について前に調べた？` のような、答えを知っている実在クエリを送る。
2. 期待するノート名が `📚 出典` に含まれること、Obsidianリンクが対象ノートを開くことを確認。
3. `STATUS.md` の関門3を `[x]` にし、**ノート名だけ**を記録する。

実機確認では、生の `obsidian://` とMarkdown形式の
`[Obsidianで開く](obsidian://open?...)` はどちらもタップできなかった。そのためMVP-0ではURIを
インラインコードで表示し、Telegramで長押しコピーして開く運用とする。HTTPSリダイレクトは公開基盤と
アクセス制御が必要になるため、必要性を本番利用後に判断する別タスクとする。

## 6. 四関門4: 本番判定（Shoji担当）

実際の会話または会議で1回だけ検索を使い、Shojiが「役立った / 要改善」を判定する。
日付、クエリの要旨、判定を `STATUS.md` に記録する。役立ったならMVP-0完了、要改善なら
個人情報を除いた症状と期待結果をTODOとして残す。
