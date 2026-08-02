# Voice Memos 取り込み: MacBook Air デプロイチェックリスト

このレーンは、iPhone の Voice Memos が MacBook Air に **同期・ダウンロード済み**になった後だけ使う。ソースの録音は常に読み取り専用で、`Archive/YYYY-MM/` へコピーしてから文字起こしし、Vault の `Cards/Audio/YYYY-MM/` にカードを作る。

## 事前確認

1. Voice Memos を開いて iCloud の初回同期が終わるまで待つ。およそ600件の同期中は **iCloud をオフ/オンしない**。macOS の版によりコンテナの内部パスは変わり得るため、実際に確認する。
2. `Recordings` が読め、ファイルを変更しないことを確認する（書込み、移動、削除は禁止）。

   ```bash
   export KH_VOICE_MEMOS_PATH="$HOME/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings"
   find "$KH_VOICE_MEMOS_PATH" -type f -name '*.m4a' | head
   ```

3. 実行する Python 本体（venv の Python を含む）または launchd の起動元に、macOS の「フルディスクアクセス」を付与する。`Operation not permitted` は TCC の典型なので、端末アプリだけでなく **実際のインタプリタ／ランチャー**を許可してから再試行する。
4. 仮想環境を作り、ffmpeg と mlx-whisper を入れる。mlx-whisper は任意依存であり、通常のリポジトリテストには不要。

   ```bash
   brew install ffmpeg
   python3 -m venv .venv
   .venv/bin/pip install mlx-whisper
   export KH_AUDIO_TRANSCRIBE_CMD="$(pwd)/.venv/bin/python $(pwd)/scripts/transcribe_with_mlx_whisper.py"
   export KH_ARCHIVE_PATH="/実在する/GoogleDrive/Archive"
   export KH_VAULT_PATH="/実在する/ObsidianVault"
   ```

   必要なら `KH_MLX_WHISPER_MODEL` と `KH_MLX_WHISPER_LANGUAGE`（既定: `ja`）で
   mlx-whisper ラッパーのモデルと言語を変更できる。長時間録音の上限は
   `KH_AUDIO_TRANSCRIBE_TIMEOUT` または `--transcribe-timeout`（既定: 7200秒）で変更できる。

## 初回と検証

過去約600件は既定で取り込まない。同期完了後、まず明示的に基準化する。

```bash
python3 -m knowledge_hub.voice_memos --baseline-existing --once
python3 -m knowledge_hub.voice_memos --once
```

`--baseline-existing` は状態だけを作り、コピーも文字起こしも行わない。基準化時刻以前の
mtimeを持つ録音が同期遅延で後から現れた場合も、追加の防御として過去分扱いにする。これは
初回iCloud同期を待つことの代わりではない。過去分をどうしても処理する場合だけ、明確な
運用判断として `--backfill-existing` を指定する。通常は指定しない。

新規の短いテスト録音を1件作り、少なくとも2回の走査（既定では30秒ごとの watch）後に次を確認する。

1. Archive に `YYYY-MM/MMDD_HHmm_voice-memo_<short-id>.m4a` がコピーされ、元の録音が残っている。
2. `Cards/Audio/YYYY-MM/` のカードに scalar frontmatter と transcript がある。
3. `python3 -m knowledge_hub.find "テスト録音の語" --vault "$KH_VAULT_PATH" --no-llm` でカードが見つかる。
4. launchd 再起動後も同じ録音のコピー／カードが増えない。

## launchd

`deploy/com.knowledge-hub.voice-memos.plist.template` を `~/Library/LaunchAgents/` へコピーし、すべての `__...__` プレースホルダーをこのMacの絶対パスに置換する。秘密情報や実ユーザーの絶対パスはテンプレートに書き込まない。置換後に以下を行う。
テンプレートの `KeepAlive` は異常終了した監視プロセスを再起動し、`ThrottleInterval` は再起動を30秒以上あける。

```bash
LOG_DIR="$HOME/Library/Logs/knowledge-hub"
mkdir -p "$LOG_DIR"
# plist の __LOG_DIRECTORY__ を上の LOG_DIR に置換してから続ける
plutil -lint ~/Library/LaunchAgents/com.knowledge-hub.voice-memos.plist
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.knowledge-hub.voice-memos.plist 2>/dev/null || true
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.knowledge-hub.voice-memos.plist
```

失敗時は plist の stderr log と、フルディスクアクセスの対象が launchd から起動する Python になっているかを確認する。実機での同期パス・TCC・文字起こし品質はこのリポジトリでは未検証である。
