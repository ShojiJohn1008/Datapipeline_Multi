# 実装指示書 A-03：Recall PWAにVault横断検索（Pull窓）を実装

## 背景（Claude Codeへのコンテキスト）

知識基盤「1つの置き場、複数のレンズ」のPull窓実装。ユーザー調査の結果、
「過去に調べた確証があるときはRecall PWAを開く」という既存習慣が判明したため、
検索窓口をTelegram（MVP-0、稼働中）からRecall PWAへ移す。
MVP-0で作った検索パイプライン（ripgrep→スコアリング→Claude）は裏側として流用する。

- コアシーン：会話・会議中にスマホで「前の自分の知見」を引く
- 完了定義：会話中の実質問1件に、PWAの一覧が数秒で出て役立ったと判定される
- UX原則（確定済み・変更不可）：
  1. **二段返し**：一段目=ヒット一覧を即返し（Claude呼び出し禁止）、
     二段目=「まとめる」ボタンでオンデマンド合成
  2. **スコープの規律**：合成が答えるのは「過去の自分の記録」のみ。
     一般知識の質問には合成せず外部（OpenEvidence等）を案内
  3. 出典は `obsidian://` 直リンクでObsidianへ1タップ（PWAは自前フロントなので橋は不要）

## アーキテクチャ：GAS中継ポーリング方式

Vault（医療系機微を含む）を外部公開しないため、Macは受信しない。
PWA→GASの「質問箱」に書く→Macが数秒毎に取りに行く→「回答箱」に書き戻す→PWAが受け取る。

```
[PWA] --POST ask--> [GAS+Sheets: requests] <--poll(3秒毎)-- [Mac常駐ワーカー]
[PWA] <--poll answer-- [GAS+Sheets: answers] <--POST result-- [Mac: 検索実行]
```

## コンポーネント別仕様

### 1. GAS（Recallの既存GASプロジェクトに追加）
- `doPost(mode=ask)`：{token, query, type: "search"|"summarize", ids?} を受け、
  requestsシートに {id(UUID), timestamp, status:"pending", payload} を追記。idを返す
- `doGet(mode=answer&id=...)`：answersシートから該当idの結果を返す（JSONP、既存方式に合わせる)
- `doGet(mode=pending)`：Macワーカー用。pending行を返し status:"taken" に更新
- `doPost(mode=result)`：Macワーカーが結果を書き戻す
- **共有トークン**：全リクエストで固定トークンを検証。不一致は無応答
- **掃除**：取得済みの回答行は24時間後に削除するトリガー（機微スニペットを残さない）

### 2. Mac常駐ワーカー（Telegram秘書と同居、別プロセスでも可）
- 3秒間隔でpendingをポーリング
- type="search"（一段目）：
  - MVP-0のripgrepパイプラインを実行（類語展開もMVP-0流用）
  - 上位10件の **frontmatterのみ** 読む → {title, captured_at/日付, summary, filepath}
  - Claude APIは呼ばない（速度最優先。目標：質問からPWA表示まで5秒台）
- type="summarize"（二段目、idsで対象カード指定）:
  - 該当カードの本文（該当箇所±20行）をClaudeに渡し合成
  - プロンプト要件：(a)出典番号[1][2]を必ず付け一覧と対応させる
    (b)「あなたの過去の記録によれば」の枠を守る
    (c)渡した記録から答えられない一般知識の問いには
    「これは記録でなく一般知識の質問。外部の医学情報源を推奨」と返す
- 結果をGASへPOST

### 3. PWAフロント（recall/ 配下、既存UIに検索タブ追加）
- 検索ボックス→送信→answerを2秒間隔でポーリング（30秒でタイムアウト表示）
- 一段目表示：カードリスト（タイトル／日付／summary 2行）。各カードに
  - 「Obsidianで開く」→ `obsidian://open?vault=JohnSecondBrain&file=<path>` 直リンク
    （既存Recallのボタンと同方式。パスは1回だけencode。橋ページは使わない）
  - チェックボックス（二段目の対象選択、既定は全件）
- 「この◯件をまとめる」ボタン→ type=summarize 送信→合成結果を出典番号付きで表示
- ローディング中も一段目は表示したまま（二段目待ちで画面を消さない）

## 受け入れ条件（verify-deploy 四関門）
1. ユニット：ワーカーが既知クエリで期待frontmatter一覧を返す
2. 結合：本番PWA（Vercel）→GAS→Mac→回答表示の一周が通る
3. 実データ：実在する過去の調べ物で正しいカードが一覧に出る。
   「まとめる」で出典番号付き合成が返る。一般知識質問で外部案内が返る
4. 本番運用：実際の会話・会議で1回使いShojiが判定→STATUS.mdに記録

## 注意
- Vercelデプロイは recall/ をRoot Directoryに（既知の404対策）
- 遅延が体感でストレスになったらCloudflare Tunnel+Accessへの昇格を検討（今回はやらない）
- requestsシートに検索クエリが残ることは許容（クエリは機微でない前提。気になれば同じ24h掃除に含める）
