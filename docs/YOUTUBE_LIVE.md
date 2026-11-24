# YouTubeライブ → 録画 → 文字起こし → Obsidian

`python3 -m knowledge_hub.youtube_live <URL>` 一本で、MacBook Air（Apple Silicon）上で
録画 → 文字起こし → 要約 → Obsidianノート化 まで行う。

## 初回セットアップ（MacBook Airで1回だけ）

```bash
brew install yt-dlp ffmpeg
python3 -m pip install --user mlx-whisper   # Apple Silicon用Whisper（GPU/Neural Engineで高速）
# 要約は既存と同じ `claude -p`（KH_AGENT_CMD で差し替え可）。claude にログイン済みであること
```

`~/.zshrc` に以下があれば流用される（既存パイプラインと共通）。

```bash
export KH_VAULT_PATH="…/JohnSecondBrain"
export KH_ARCHIVE_PATH="…/Archive"   # 未設定なら録画は ~/Movies/YouTubeLive に保存
```

初回の文字起こし時にWhisperモデル（large-v3-turbo、約1.6GB）が自動でダウンロードされる。

## 使い方

```bash
cd ~/Datapipeline_Multi
# ライブ中・配信前・アーカイブ、どのURLでもよい
python3 -m knowledge_hub.youtube_live "https://www.youtube.com/watch?v=XXXX"

# 途中から見始めたけど最初から録りたい
python3 -m knowledge_hub.youtube_live "<URL>" --from-start
```

- ライブ終了で録画が自動停止し、そのまま文字起こし・ノート化へ進む。
- 途中で止めたいときは **Ctrl-C を1回**。そこまでの録画を文字起こしする。
- 配信開始前のURLなら、始まるまで30秒おきに待機する。
- 長時間録画中はMacをスリープさせない: 別ターミナルで `caffeinate -dims` か、
  `caffeinate -dims python3 -m knowledge_hub.youtube_live "<URL>"` として起動する。

## 出力

| 場所 | 中身 |
|---|---|
| `$KH_ARCHIVE_PATH/YouTube/<日付>-<動画ID>/video.*` | 録画（既定720p） |
| 同 `transcript.srt` / `transcript.txt` | 文字起こし（タイムスタンプ付き） |
| `<Vault>/Cards/YYYY/MM/<日付> <タイトル>--<動画ID>.md` | Obsidianノート |

ノートの構成: 概要 / 要点 / トピック別ノート / 印象に残った発言 / 自分用メモ / タグ候補、
末尾に折りたたみの文字起こし全文。`[hh:mm:ss]` はすべてYouTubeの該当時刻へのリンク。
frontmatter は既存カードと同じく `id` / `title` / `source_type` / `captured_at` / `tags` を持つので、
`/find` や Recall からも検索できる。

## 途中からやり直す

```bash
--skip-record                 # 録画済み（保存先の video.*）から文字起こし以降
--media ~/Downloads/x.mp4     # 手元の動画/音声ファイルを文字起こし
--transcript <…/transcript.srt>  # 文字起こし済み。要約とノートだけ作り直す
--no-summary                  # 要約せず文字起こしだけノート化
--overwrite                   # 同名ノートを上書き（既定は手書き追記を守るため別名保存）
```

要約に失敗しても、文字起こし全文入りのノートは必ず書かれ、再実行コマンドがノート内に表示される。

## 調整

| 設定 | 既定 | 用途 |
|---|---|---|
| `--max-height` | 720 | 録画画質。音声だけで十分なら 360 などで容量節約 |
| `--model` | `mlx-community/whisper-large-v3-turbo` | 精度重視なら `mlx-community/whisper-large-v3-mlx` |
| `--backend whisper-cpp` | mlx | `brew install whisper-cpp` + `KH_WHISPER_CPP_MODEL=<ggmlファイル>` |
| `KH_YT_SUMMARY_TIMEOUT` | 900秒 | 長時間配信の要約待ち時間 |
