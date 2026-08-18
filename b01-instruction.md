# 実装指示書 B-01：音声レーン（ボイスメモ自動取り込み→ローカル文字起こし→索引カード自動生成）

**実装済み**: `knowledge_hub/voice_lane.py`・`knowledge_hub/voice_ingest.py`／
テスト: `tests/test_voice_lane.py`（関門1 green）。残るはMac実機での関門2〜4。

## 背景（実装者へのコンテキスト)

「1つの置き場、複数のレンズ」の入れる側・第1レーン。iPhoneのボイスメモで録音した
音声は**iCloud同期で自動的にMacBook Airへ届いている**ため、iPhone側の操作はゼロ。
Macが数分毎にボイスメモの新着を検知し、原本をDrive `/Archive/` に集約した上で
**ローカルで**文字起こしし、タイトル・要約・タグ付きの索引カードとして
Vault `Cards/` に自動保存する。人が書くフィールドはゼロ（索引カードv0.1準拠）。

- 前提: A-02完了（Driveミラーリング済みローカルパスと `Cards/` が存在すること）
- 完了定義: iPhoneで録音→放置→数分後に `Cards/` に読めるカードが生えており、
  `/find` でその内容が検索にヒットする
- 入口はボイスメモに一本化。Drive手動投入・iCloud共有フォルダは本レーンの監視対象外

## 確定済みの設計判断（変更不可）

1. **取り込み元はボイスメモのAppleコンテナ**（読み取り専用。書き込み・削除はしない）:
   既定 `~/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings`
   （`KH_VOICEMEMO_PATH` で差し替え可）。タイトル・録音日時は同フォルダの
   `CloudRecordings.db` から読む。**半公式ルート**のため、スキーマが読めない場合は
   ファイルスキャン（タイトル=ファイル名・日時=mtime）へ必ず縮退する
2. **文字起こしはローカル実行**。クラウドASRは使わない（医療系機微を外に出さない）
3. **エンジンはコマンド差し替え式**（A-01の `KH_CLAUDE_CMD` と同じ流儀）:
   - 文字起こし: `KH_ASR_CMD`（既定: whisper.cpp。医療用語対応OSSへ差し替え可能）
     契約: 引数に音声ファイルパス、stdoutにプレーンテキスト、失敗はexit非0
   - カード生成LLM: `KH_CLAUDE_CMD`（既定: `claude -p`。Ollama等ローカルLLMへ差し替え可能）
4. **原本の系譜**: ボイスメモ側は不変。処理時に `/Archive/YYYY-MM/MMDD_HHmm_<名>.m4a`
   へコピーしてこれを原本ストアとし、カードのfrontmatter `source:` でつなぐ
5. **タイトルは自動生成**: ファイル名でなく内容からLLMが命名（要約・タグと同一呼び出しで）
6. **全文文字起こしをカード本文に含める**: ripgrepが「喋った単語そのもの」に
   ヒットする状態を作る（要約だけでは検索網羅性が落ちる）
7. **同じ録音を二度文字起こししない**（重複防止は二重）:
   - 処理台帳（`~/.kh_voice_state.json`）: ファイル名＋サイズで同一性判定。
     ボイスメモ上でタイトルを後から変えても再処理されない（タイトルは鍵にしない）
   - カード実在チェック: `Cards/` に同じ `source:` を指すカードが既にあれば
     **文字起こし自体を実行せず**スキップ（台帳消失時の保険）
8. 16GB Airの制約: ASRとLLMは**順次実行**（同時常駐させない）

## パイプライン（実装済み）

```
[launchd/cron 数分毎] → ボイスメモ新着検知(DB読み→縮退スキャン) → Archiveへコピー(原本化)
  → KH_ASR_CMD(ローカル文字起こし) → KH_CLAUDE_CMD(タイトル・要約・タグ) → Cards/保存 → 台帳記録
```

```bash
# 1パス実行（launchdから数分毎に起動する想定）
python3 -m knowledge_hub.voice_lane [--source PATH] [--archive PATH] [--vault PATH] \
                                    [--state PATH] [--no-llm]
# exit 0: 処理結果をstdoutへ1行ずつ / exit 2: 設定エラー
```

- 環境変数: `KH_VOICEMEMO_PATH`（取り込み元）・`KH_ARCHIVE_PATH`（**必須**、A-02の実パス）・
  `KH_CARDS_PATH`（カード保存先。既定 `<Vault>/Cards`）・
  `KH_VAULT_PATH`・`KH_ASR_CMD`・`KH_ASR_TIMEOUT`・`KH_CLAUDE_CMD`・`KH_VOICE_STATE`
- 取り込み元はボイスメモコンテナに限らず任意のフォルダを指定できる（直下のみ・非再帰）。
  Shoji実機は既定のボイスメモコンテナのままでよい。Vault内 `1_Raw/Transcripts/audio` は
  既存ワークフローの「文字起こし済み音声」の置き場であり、本レーンの監視対象ではない
- **初回導入**: `--baseline` で既存の録音を処理済み扱いにできる（以後の新着のみ処理。
  `--keep-days N` を併用すると直近N日分は処理対象に残る。
  過去分も全部カード化したい場合はbaselineせずそのまま回す。台帳削除で取り消し可）
- **同期中を掴まない**: 2パス連続でサイズ不変のファイルだけ処理（初見は「⏳ 同期待ち」）
- ASR失敗: 台帳に載せず次パスでリトライ（「⚠️ 失敗」）。タイムアウトは
  `KH_ASR_TIMEOUT` 指定が無ければサイズから自動算出（1MBあたり90秒・最低5分）
- LLM失敗の縮退: `title=音声メモHHMM`・要約なしで**カード保存は必ず実行**
  （文字起こし結果を失わないことを最優先）

## カード形式

```markdown
---
type: voice
captured_at: 2026-08-01 08:12
source: "Archive/2026-08/0801_0812_通勤メモ.m4a"
summary: "<2〜3文>"
tags: ["tag1", "tag2"]
---
## 要約
<summary再掲>

## 全文文字起こし
<ASR出力そのまま>
```

保存先は `Cards/YYYY-MM-DD_<title>.md`（衝突時は `-2` 連番）。

## 受け入れ条件（四関門）

1. ✅ ユニット: 偽ASR・偽LLMで、検知（DB/縮退）→取り込み→保存→台帳・二重処理防止
   （台帳＋カード実在チェック）・同期中保留・縮退経路が全てテストgreen
2. 結合: Mac実機で実音声1件がカードになる（whisper.cpp実行込み）
3. 実データ: 実際の通勤メモ級の録音でタイトル・要約が妥当、かつ
   `python3 -m knowledge_hub.find` で喋った内容の単語がヒットする
4. 本番: 1週間「録って放置」で運用しShojiが判定→STATUS.mdに記録

## Mac実機セットアップ（関門2の手順）

前提: A-02完了（`$KH_ARCHIVE_PATH` と Vault `Cards/` が実在）。
Macで新しく作るファイルは3つだけ: ①ASRラッパー ②環境変数 ③launchd plist。

### ① ASRラッパー `~/bin/kh-asr.sh`

導入済みWhisperの形態を確認: `which whisper whisper-cli mlx_whisper`

**A. openai-whisper（pipの `whisper`）の場合**（モデル名 `turbo` = large-v3-turbo）:

```sh
#!/bin/sh
# KH_ASR_CMD契約: 引数=音声パス / stdout=テキストのみ / 失敗=exit非0
set -e
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
whisper --model turbo --language ja --output_format txt \
        --output_dir "$tmp" "$1" >/dev/null 2>&1
cat "$tmp"/*.txt
```

**B. whisper.cpp（`whisper-cli`）の場合**（m4a直読み不可の版があるためwav変換を挟む）:

```sh
#!/bin/sh
set -e
tmp=$(mktemp).wav
trap 'rm -f "$tmp"' EXIT
ffmpeg -y -i "$1" -ar 16000 -ac 1 -c:a pcm_s16le "$tmp" >/dev/null 2>&1
whisper-cli -m "$HOME/models/ggml-large-v3-turbo.bin" -l ja -nt -np -f "$tmp"
```

作成後: `chmod +x ~/bin/kh-asr.sh` → `~/bin/kh-asr.sh <適当なm4a>` がテキストを出せばOK。

### ② 環境変数（`~/.zshrc` に追記。値はコミットしない）

```sh
export KH_ARCHIVE_PATH="$HOME/Library/CloudStorage/GoogleDrive-<アカウント>/マイドライブ/Archive"
export KH_ASR_CMD="$HOME/bin/kh-asr.sh"
# Vaultが既定のiCloudパス(JohnSecondBrain)ならKH_VAULT_PATHは不要
```

### ③ 手動で関門2を通す

ボイスメモコンテナを読むため、システム設定→プライバシーとセキュリティ→
**フルディスクアクセス**にターミナルを追加してから:

```sh
cd Datapipeline_Multi
python3 -m knowledge_hub.voice_lane   # 1回目: ⏳ 同期待ち
python3 -m knowledge_hub.voice_lane   # 2回目: ✅ カード生成
```

Obsidianで `Cards/` にカードが見えたら関門2通過。`STATUS.md` に確認日・所要時間を記録。

### ④ launchd常駐化 `~/Library/LaunchAgents/com.kh.voice-lane.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.kh.voice-lane</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string><string>-m</string><string>knowledge_hub.voice_lane</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/＜ユーザー名＞/Datapipeline_Multi</string>
  <key>StartInterval</key><integer>300</integer>
  <key>EnvironmentVariables</key>
  <dict>
    <key>KH_ARCHIVE_PATH</key><string>＜②と同じ実パス＞</string>
    <key>KH_ASR_CMD</key><string>/Users/＜ユーザー名＞/bin/kh-asr.sh</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>StandardOutPath</key><string>/Users/＜ユーザー名＞/Library/Logs/kh-voice-lane.log</string>
  <key>StandardErrorPath</key><string>/Users/＜ユーザー名＞/Library/Logs/kh-voice-lane.err</string>
</dict>
</plist>
```

登録: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.kh.voice-lane.plist`
（launchd実行でボイスメモが読めない場合は python3 にもフルディスクアクセスを付与）

## 注意

- **C-05（バックアップ）をこのレーンの稼働開始前に必ず着手**。実データが貯まり始める
- レッドライン: 患者特定情報は録音時点で入れない。これは運用ルールであり
  本レーンに検知機構は実装しない（誤検知で本文を失うリスクの方が大きい）
- ボイスメモDBのスキーマがOS更新で変わったら: 縮退スキャンで動き続けるが
  タイトルが失われるので、`voice_ingest.py` の `_TITLE_COLUMNS` を実機のDBに合わせ更新
- ローカルLLM移行: まず `claude -p` で品質基準を作り、Ollama（7〜14B・4bit量子化、
  日本語に強いモデル）に `KH_CLAUDE_CMD` を差し替えて比較。十分ならローカル固定
- 録音をボイスメモ側で編集（トリミング等）するとサイズが変わり新規扱いで
  再処理される。別カードが増えるだけで原本・既存カードは壊れない
