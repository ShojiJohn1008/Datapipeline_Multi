# DESIGN — MVP-0: Telegram秘書 Vault横断検索 `/find`

対象バックログ: A-01（出す側MVP）／指示書: mvp0instruction.md 準拠
作業ルーティング: **設計・難所 = Claude ／ 実装量産・テストループ = GPT-5.6**（docs/HANDOFF_GPT56.md 参照）

---

## 1. 全体アーキテクチャ

```
Telegram (スマホ)
   │  /find <クエリ>  または 先頭「探して」「前に調べた」
   ▼
Telegram秘書bot（MacBook Air / tmux / Claude Codeベース・既存）
   │  サブプロセス起動（このリポジトリのCLI）
   ▼
python3 -m knowledge_hub.find "<クエリ>" [--vault PATH] [--no-llm]
   ①受信 → ①'クエリ正規化（口語定型句除去）→ ②語展開(llm) → ③rg実行
   → ④集約・スコア → ⑤抜粋±10行 → ⑥回答生成(llm) → ⑦stdoutにTelegram返信文
   ▼
bot は stdout をそのまま Telegram に返信する
```

- **bot本体のコードはこのリポジトリに含まれない**（Mac上の既存bot）。結合は「CLIを叩いてstdoutを返す」1点のみ。
  既存botのルーティングを壊さないための最小接点。
- LLM呼び出しは `claude -p`（CLI）をサブプロセス実行。環境変数 `KH_CLAUDE_CMD` で差し替え可能
  （既存botのAPI経路に乗せ替える場合もこの1点を変えるだけ）。

## 2. モジュール構成と責務

| ファイル | 責務 | 変更難度 |
|---|---|---|
| `knowledge_hub/config.py` | Vaultパス解決・存在チェック・定数（タイムアウト等） | 低 |
| `knowledge_hub/vault_search.py` | **コア**: rg実行(JSON parse)・集約・スコア・抜粋・iCloud検知・obsidianリンク | **高（凍結）** |
| `knowledge_hub/llm.py` | claude CLI呼び出し・語展開・回答生成・プロンプト定義・フォールバック | 中（プロンプトは凍結） |
| `knowledge_hub/find.py` | パイプライン統括・0件リトライ・時間予算・返信整形・CLI | 中 |
| `tests/` | unittest（`python3 -m unittest discover -s tests -v`） | GPT-5.6が量産 |

「凍結」= インターフェース・プロンプト文言を変える場合は本書と HANDOFF を同時更新すること。

## 3. インターフェース契約（凍結）

```python
# vault_search.py
run_ripgrep(terms: list[str], vault: Path, timeout: float = 5.0)
    -> tuple[dict[str, FileHits], bool]        # (相対パス→ヒット, タイムアウトしたか)
    # タイムアウト時も部分stdoutをparseして返す（フェイルセーフ）
score_files(hits: dict[str, FileHits], vault: Path, now: float | None = None)
    -> list[FileResult]                        # score降順・同点はmtime新しい順
extract_snippet(abs_path: Path, line_numbers: list[int],
                margin: int = 10, max_chars: int = 2000) -> str
icloud_placeholders_exist(vault: Path) -> bool
try_brctl_download(vault: Path) -> bool
obsidian_link(vault: Path, rel_path: Path) -> str

# llm.py
expand_query(query: str, timeout: float = 10.0) -> list[str]   # 先頭は必ず原クエリ・最大6語
generate_answer(query: str, results: list[FileResult], timeout: float = 20.0) -> str | None
    # None = 生成失敗（縮退モード: 抜粋のみ返信）

# find.py
normalize_query(query: str) -> str   # 「腫瘍熱について前に調べた？」→「腫瘍熱」。剥がしすぎたら原文
relax_terms(terms: list[str]) -> list[str]   # 0件時の緩和語（元の語を含む上位集合）
run_find(query: str, vault: Path, use_llm: bool = True) -> str  # Telegram返信文そのもの
main() -> int   # 0=正常（0件回答も正常）, 2=設定エラー（vault無し等）
```

- CLI終了コード契約: **0** なら stdout をそのまま返信してよい。**2** は設定エラーで stderr に日本語メッセージ。
- Python 3.9+ 互換（Mac標準CLT想定）。外部依存ゼロ（標準ライブラリのみ）。必須外部コマンド: `rg`。

## 4. スコアリング（設計決定）

「ヒット数 × 更新日の新しさ」を次で定義:

```
score = hits × recency_weight(age_days)
recency_weight = 0.25 + 0.75 × exp(−age_days × ln2 / 90)
```

- 半減期90日。**床0.25** を置く理由: 古くても濃いノート（臨床知識など）がゼロに沈まないようにする。
- テストは**順序性のみ**を検証する（係数の微調整を妨げないため）:
  同ヒット数なら新しい方が上位／recency同等ならヒット数が支配。

## 5. エッジケース方針（指示書対応表）

| ケース | 挙動 |
|---|---|
| 0件 | `relax_terms()` で語を緩めて**1回だけ**再検索。それでも0件なら「見つからず。試した語：…」と正直に返す |
| 100ファイル超 | 上位5件のみ返し「（他にも◯件ヒット）」を添える |
| iCloud実体退避 | rg 0件かつ `**/*.icloud` が存在 → `brctl download <VAULT>` を試み、返信に「☁️ iCloud同期待ちの可能性」を含める |
| 日本語 | UTF-8そのまま。`-w` は使わない |
| rgタイムアウト(5s) | 部分stdoutをparseし「⚠️ 部分結果」注記付きで返す |
| LLM失敗/タイムアウト | 語展開→**正規化済みクエリのみ**で続行（normalize_queryが「〜について前に調べた？」等の定型句を剥がすので自然文でも検索が成立する）。回答生成→縮退モード（出典＋抜粋見出しのみ） |
| 全体予算60s | 回答生成前に残り予算を確認。不足なら回答生成をスキップして縮退返信 |

`relax_terms()` の緩め方（設計決定）:
1. 空白・「・」「/」「－」等で分割した部分語を追加
2. CJK語で3文字以上 → 末尾1文字を落とした接頭辞を追加（例: 腫瘍熱→腫瘍）
3. ASCII語で6文字以上 → 2/3長の接頭辞を追加（例: anchoring→anchor）
4. 2文字未満は捨てる・重複排除・元の語は維持（OR検索の上位集合になる）

## 6. LLMプロンプト（凍結・llm.py内に定義)

- **語展開**: JSON配列のみを出力させ、正規表現で最初の `[...]` を抽出してparse。失敗したら原クエリのみ。
- **回答生成**: 「抜粋のみを根拠に2〜4文」「抜粋に無いことは書かない」「出典リストは書かない（こちらで付す）」。

## 7. 返信フォーマット

```
🔎 「<クエリ>」

<回答2〜4文。縮退時は「（要約なし：ヒット箇所のみ提示）」>

📚 出典:
1. <ファイル名（拡張子なし）>（YYYY-MM-DD）
   obsidian://open?vault=<URLエンコード済Vault名>&file=<URLエンコード済相対パス(拡張子なし)>
...最大5件
（他にも N 件ヒット）
<注記行: ⚠️/☁️ があれば>
```

0件時: `見つからず。試した語：語1 / 語2 / …`（＋注記行）

## 8. 時間予算（目標: 受信から30秒）

| 工程 | 上限 |
|---|---|
| 語展開(claude) | 10s（超過→原クエリで続行） |
| rg（最大2回: 本検索＋緩和リトライ） | 各5s |
| 抜粋読み込み | 実質0s（該当範囲のみ読む） |
| 回答生成(claude) | 20s（超過→縮退） |
| 全体フェイルセーフ | 60s（残予算<5sで回答生成スキップ） |

## 9. bot結合手順（Mac側・GPT-5.6のT4）

1. このリポジトリを Mac に clone（bot と同居でよい）
2. 既存botのメッセージルーティングに追加:
   - `/find <q>` または本文先頭が「探して」「前に調べた」→ 検索モード
   - `python3 -m knowledge_hub.find "<q>"` を cwd=リポジトリ root、タイムアウト60sで実行
   - exit 0 → stdout をそのまま返信／それ以外 → 「検索機能でエラー」＋stderr要約を返信
3. 既存の応答経路には一切手を入れない（新規分岐の追加のみ）

## 10. 受け入れ（verify-deploy 四関門）

1. ユニット: `python3 -m unittest discover -s tests -v` 全green（rgラッパーが既知クエリで期待ファイルを返すテストを含む）
2. 結合: 実Telegramから `/find` → 応答が返る
3. 実データ: 「腫瘍熱について前に調べた？」等、実在の調べ物で正しいノートが出典に含まれる
4. 本番: 実際の会話・会議で1回使い、役立ったかを Shoji が判定 → STATUS.md に記録して完了
