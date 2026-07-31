# HANDOFF — GPT-5.6 作業指示書（実装量産・テストループ担当）

あなた（GPT-5.6）の役割: このリポジトリの **テスト量産・残実装・テストループ・Mac実機組み込み** を担当する。
設計・コアロジック・プロンプトは確定済み（docs/DESIGN.md）。**設計判断はしない**。仕様が曖昧だと感じたら
DESIGN.md を正とし、それでも決められない点は変更せず TODO コメントで報告する。

## 現状（Claude側で完了済み）

- `knowledge_hub/` 全モジュール: 動作するコア実装済み（縮退モード含めCLIがend-to-endで動く）
- `tests/test_vault_search.py`: シードテスト（パターン見本）＋フィクスチャVault `tests/fixtures/vault/`
- ここまで全テスト green を確認済み

## ガードレール（禁止事項）

1. DESIGN.md §3 の関数シグネチャ・終了コード契約を**変更しない**
2. `llm.py` のプロンプト文言を**変更しない**
3. 外部依存（pip パッケージ）を**追加しない**。Python 3.9 互換を維持（`match`文・`X | Y`の実行時使用は不可。
   アノテーションは `from __future__ import annotations` 前提で可）
4. スコア係数（半減期90日・床0.25）を**調整しない**。テストは順序性のみ検証
5. 既存テストを弱めない（アサーション削除・skip化は禁止）

## タスク（順に実施）

### T1. テスト量産（最優先）

`tests/` に unittest を追加。**テストマトリクス**（各行min 1テスト）:

| # | 対象 | ケース |
|---|---|---|
| 1 | run_ripgrep | 複数語OR検索で両ファイルがヒットする |
| 2 | run_ripgrep | 大文字小文字を無視する（英語語で確認） |
| 3 | run_ripgrep | `.obsidian/` 内の .md がヒットしない（フィクスチャに配置済み） |
| 4 | run_ripgrep | rg不在（`KH_RG_BIN=存在しないパス`）で RipgrepError |
| 5 | score_files | ヒット数が多いファイルが上位（mtime同等時） |
| 6 | extract_snippet | 近接する2ヒットの範囲がマージされ重複行がない |
| 7 | extract_snippet | max_chars で切り詰められ末尾に省略記号 |
| 8 | relax_terms | 「腫瘍熱」→「腫瘍」を含む／2文字未満が混入しない |
| 9 | relax_terms | ASCII長語の接頭辞化（anchoring→anchor） |
| 10 | obsidian_link | 日本語・空白入りパスがURLエンコードされ `.md` が落ちる |
| 11 | expand_query | `KH_CLAUDE_CMD` を偽スクリプト（JSON配列をechoする.sh）に向け、原クエリが先頭・最大6語 |
| 12 | expand_query | 偽スクリプトが不正出力/非0終了 → 原クエリのみに縮退 |
| 13 | run_find | ヒット0件 → 「見つからず。試した語：」を含む返信 |
| 14 | run_find | `--no-llm` 相当（use_llm=False）で出典ブロックを含む返信 |
| 15 | run_find | icloudプレースホルダ（`x.md.icloud` をtempに配置）＋0件 → ☁️注記 |
| 16 | CLI (subprocess) | `python3 -m knowledge_hub.find 腫瘍熱 --vault tests/fixtures/vault --no-llm` が exit 0・出典を含む |
| 17 | CLI (subprocess) | 存在しないvault → exit 2・stderrに日本語メッセージ |
| 18 | normalize_query | 「〇〇とは？」「〇〇の話探して」等のバリエーション5種以上が中身の語に正規化される |
| 19 | normalize_query | 定型句のみのクエリ（例:「前に調べた」）で空にならず原文が返る |

注意: mtimeを操作するテストは `os.utime` でtemp配下に作ること（フィクスチャのmtimeはgit cloneで変わるため、コミット済みフィクスチャで時刻依存の検証をしない）。

### T2. `--json` 出力モードの追加

- `find.py` のCLIに `--json` フラグを追加。stdout に以下のJSONを1行で出す:
  `{"query":…, "answer":…|null, "sources":[{"file":…, "date":"YYYY-MM-DD", "link":…, "hits":…}], "total_files":N, "notes":[…]}`
- `run_find` の戻り値契約は変えないこと（内部で整形前データを持つ小さなリファクタは可、シグネチャ変更は不可）
- テストを2本以上追加（正常系JSON parse可能・0件時 sources=[]）

### T3. テストループ

```
python3 -m unittest discover -s tests -v
```
が全green になるまで修正→再実行。コアの挙動とテストが矛盾したら**テストではなくDESIGN.mdを正**として判断。

### T4. Mac実機組み込み（DESIGN.md §9 の手順で）

- 既存botのルーティングに `/find`・「探して」「前に調べた」分岐を追加（既存応答経路は不変更）
- 実Telegramから `/find テスト` で疎通（四関門の2）
- 「腫瘍熱について前に調べた？」等の実在クエリで正しいノートが出典に入ることを確認（四関門の3）

### T5. 記録

- 各関門を通過したら `STATUS.md` の「次の一手」を更新
- 四関門の4（本番の会話で1回使って役立ち判定）は Shoji の作業。依頼して待つ

## 完了報告フォーマット

```
T1: 追加テストN本 / マトリクス網羅 17/17 / 全green
T2: --json 実装済み・テストM本
T3: unittest 合計K本 green
T4: 疎通ログ・実データ検証結果（ヒットしたノート名）
残TODO・判断保留事項: …
```
