"""B-01 音声レーン統括とCLI（仕様: b01-instruction.md）。

使い方（launchd/cronからの結合点はこの1コマンドのみ）:
    python3 -m knowledge_hub.voice_lane [--source PATH] [--archive PATH] [--vault PATH]
                                        [--cards PATH] [--state PATH] [--no-llm]
1回起動=1パス。ボイスメモ新着を Archive へコピー→ローカル文字起こし→索引カード保存。
exit 0: 処理結果をstdoutへ1行ずつ / exit 2: 設定エラー（stderr参照）

重複防止は二重: ①処理台帳（ファイル名＋サイズで同一性判定。タイトルは後から
変更されうるため鍵にしない） ②Cards/に同じ原本を指すカードが既にあれば
文字起こし自体を実行しない（台帳消失時の保険）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import config
from .llm import call_claude
from .voice_ingest import Recording, discover_recordings

# プロンプト（凍結・b01-instruction.md）
CARD_PROMPT = """あなたは音声メモの索引カード生成器。以下の文字起こしから、JSONオブジェクト1個のみを出力せよ。
形式: {{"title": "...", "summary": "...", "tags": ["...", "..."]}}
- title: 内容を表す15字以内の名詞句。ファイル名に使えない文字（/ \\ : * ? " < > |）は使わない
- summary: 「何を考えた・決めた・調べたか」を優先した2〜3文
- tags: 1〜5個の短い分類語
- 文字起こしに無い内容を創作・補完しない
- JSON以外の説明文・コードブロックは出力しない

文字起こし:
{transcript}"""

_UNSAFE = re.compile(r'[/\\:*?"<>|#\n\r\t]')
_SOURCE_LINE = re.compile(r'^source:\s*"?(.+?)"?\s*$')


@dataclass
class Card:
    title: str
    summary: str | None
    tags: list[str]


def sanitize_title(title: str, fallback: str) -> str:
    """ファイル名に使えない文字を除去。空になったら縮退タイトル。"""
    t = _UNSAFE.sub(" ", title)
    t = re.sub(r"\s+", " ", t).strip(" .")
    return t[:30] if t else fallback


# ---- 各ステージ ----------------------------------------------------------

def ingest(rec: Recording, archive_root: Path) -> Path:
    """原本を Archive/YYYY-MM/MMDD_HHmm_<名>.ext へコピー（冪等。ボイスメモ側は不変）。"""
    dt = datetime.fromtimestamp(rec.recorded_at)
    label = sanitize_title(rec.title or "", "音声メモ")
    dest_dir = archive_root / dt.strftime("%Y-%m")
    dest_dir.mkdir(parents=True, exist_ok=True)
    base = f"{dt.strftime('%m%d_%H%M')}_{label}"
    ext = rec.source.suffix.lower()
    dest = dest_dir / f"{base}{ext}"
    n = 2
    while dest.exists() and dest.stat().st_size != rec.source.stat().st_size:
        dest = dest_dir / f"{base}-{n}{ext}"
        n += 1
    if not dest.exists():
        shutil.copy2(rec.source, dest)
    return dest


def transcribe(audio: Path) -> str:
    """`KH_ASR_CMD <音声パス>` を実行しstdoutを返す。失敗・空出力は例外（次パスでリトライ）。"""
    cmd = shlex.split(os.environ.get("KH_ASR_CMD", config.DEFAULT_ASR_CMD)) + [str(audio)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_asr_timeout(audio))
    if proc.returncode != 0:
        raise RuntimeError(f"文字起こし失敗(code={proc.returncode}): {proc.stderr[:200]}")
    text = proc.stdout.strip()
    if not text:
        raise RuntimeError("文字起こしが空")
    return text


def _asr_timeout(audio: Path) -> float:
    env = os.environ.get("KH_ASR_TIMEOUT")
    if env:
        return float(env)
    # m4a約1MB/分の目安で1分あたり90秒、最低5分（速度より完走を優先）
    mb = audio.stat().st_size / (1024 * 1024)
    return max(300.0, mb * 90.0)


def generate_card(transcript: str, fallback_title: str, use_llm: bool) -> Card:
    """LLMでタイトル・要約・タグを生成。失敗時は縮退（カード保存は必ず実行される）。"""
    if use_llm:
        try:
            raw = call_claude(
                CARD_PROMPT.format(transcript=transcript[: config.CARD_INPUT_MAX]),
                timeout=config.CARD_TIMEOUT,
            )
            m = re.search(r"\{.*\}", raw, re.S)
            data = json.loads(m.group(0)) if m else {}
            title = data.get("title")
            if isinstance(title, str) and title.strip():
                summary = data.get("summary")
                tags = data.get("tags") if isinstance(data.get("tags"), list) else []
                return Card(
                    sanitize_title(title, fallback_title),
                    summary.strip() if isinstance(summary, str) and summary.strip() else None,
                    [t.strip() for t in tags if isinstance(t, str) and t.strip()][:5],
                )
        except Exception:
            pass
    return Card(fallback_title, None, [])


def card_exists_for_source(cards_dir: Path, archive_rel: str) -> bool:
    """カード置き場に同じ原本を指すカードが既にあるか（台帳消失時の二重処理保険）。"""
    if not cards_dir.is_dir():
        return False
    for path in cards_dir.glob("*.md"):
        try:
            with path.open(encoding="utf-8") as f:
                for _ in range(15):  # frontmatterの範囲だけ見る
                    line = f.readline()
                    if not line:
                        break
                    m = _SOURCE_LINE.match(line.strip())
                    if m and m.group(1) == archive_rel:
                        return True
        except OSError:
            continue
    return False


def save_card(cards_dir: Path, card: Card, transcript: str, rec: Recording, archive_rel: str) -> Path:
    """索引カードを <カード置き場>/YYYY-MM-DD_<title>.md へ保存（衝突時は連番）。"""
    dt = datetime.fromtimestamp(rec.recorded_at)
    cards_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        "type: voice",
        f"captured_at: {dt.strftime('%Y-%m-%d %H:%M')}",
        f"source: {json.dumps(archive_rel, ensure_ascii=False)}",
        f"summary: {json.dumps(card.summary or '', ensure_ascii=False)}",
        f"tags: {json.dumps(card.tags, ensure_ascii=False)}",
        "---",
        "## 要約",
        card.summary or "（要約なし：縮退保存）",
        "",
        "## 全文文字起こし",
        transcript,
        "",
    ]
    base = f"{dt.strftime('%Y-%m-%d')}_{card.title}"
    dest = cards_dir / f"{base}.md"
    n = 2
    while dest.exists():
        dest = cards_dir / f"{base}-{n}.md"
        n += 1
    dest.write_text("\n".join(lines), encoding="utf-8")
    return dest


# ---- 台帳とパス -----------------------------------------------------------

def load_state(path: Path) -> dict:
    """処理台帳を読む。壊れていたら作り直す（再処理は②のカード実在チェックが防ぐ）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {
                "seen": dict(data.get("seen", {})),
                "processed": dict(data.get("processed", {})),
            }
    except (OSError, ValueError):
        pass
    return {"seen": {}, "processed": {}}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def run_baseline(source: Path, state_path: Path) -> list[str]:
    """既存の録音を処理せず「処理済み」として台帳に記録する（初回導入用の基準線）。

    以後のパスは基準線より後の新着だけを処理する。取り消したい場合は台帳を
    削除すれば全件が再対象になる（カード済みのものは実在チェックが再処理を防ぐ）。
    """
    state = load_state(state_path)
    count = 0
    for rec in discover_recordings(source):
        key = f"{rec.source.name}:{rec.source.stat().st_size}"
        if key not in state["processed"]:
            state["processed"][key] = {"archive": "(baseline)", "card": "(baseline)"}
            count += 1
    save_state(state_path, state)
    return [f"基準線設定: 既存{count}件を処理済み扱いにした（以後の新着のみ処理）"]


def run_pass(source: Path, archive_root: Path, cards_dir: Path, state_path: Path,
             use_llm: bool = True) -> list[str]:
    """1パス実行。処理結果を人間可読の行リストで返す（CLIはこれをそのまま出力）。"""
    state = load_state(state_path)
    prev_seen = state["seen"]
    seen: dict[str, int] = {}
    lines: list[str] = []
    for rec in discover_recordings(source):
        size = rec.source.stat().st_size
        key = f"{rec.source.name}:{size}"
        if key in state["processed"]:
            continue
        if size == 0 or prev_seen.get(rec.source.name) != size:
            # 同期中を掴まない: 2パス連続でサイズ不変のときだけ処理する
            seen[rec.source.name] = size
            lines.append(f"⏳ 同期待ち: {rec.source.name}")
            continue
        try:
            archived = ingest(rec, archive_root)
            archive_rel = f"Archive/{archived.relative_to(archive_root)}"
            if card_exists_for_source(cards_dir, archive_rel):
                # 台帳消失後などの保険。文字起こし自体を実行しない
                state["processed"][key] = {"archive": str(archived), "card": "(既存)"}
                lines.append(f"↩️ 既存カードあり・スキップ: {rec.source.name}")
                continue
            transcript = transcribe(archived)
        except Exception as e:
            seen[rec.source.name] = size  # 台帳に載せず次パスでリトライ
            lines.append(f"⚠️ 失敗（次回リトライ）: {rec.source.name}: {e}")
            continue
        dt = datetime.fromtimestamp(rec.recorded_at)
        card = generate_card(transcript, f"音声メモ{dt.strftime('%H%M')}", use_llm)
        card_path = save_card(cards_dir, card, transcript, rec, archive_rel)
        state["processed"][key] = {"archive": str(archived), "card": card_path.name}
        lines.append(f"✅ {card_path.name} ← {rec.source.name}")
    state["seen"] = seen
    save_state(state_path, state)
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="knowledge_hub.voice_lane", description="B-01 音声レーン（1パス実行）")
    parser.add_argument("--source", help="ボイスメモ録音フォルダ（既定: $KH_VOICEMEMO_PATH → Apple既定パス）")
    parser.add_argument("--archive", help="原本ストアArchiveのローカル実パス（既定: $KH_ARCHIVE_PATH）")
    parser.add_argument("--vault", help="Vaultパス（既定: $KH_VAULT_PATH → iCloud既定パス）")
    parser.add_argument("--cards", help="カード保存先（既定: $KH_CARDS_PATH → <Vault>/Cards）")
    parser.add_argument("--state", help="処理台帳（既定: $KH_VOICE_STATE → ~/.kh_voice_state.json）")
    parser.add_argument("--no-llm", action="store_true", help="LLMを使わない（縮退タイトルで保存）")
    parser.add_argument("--baseline", action="store_true",
                        help="既存の録音を処理済み扱いにして終了（初回導入時: 以後の新着のみ処理）")
    args = parser.parse_args(argv)

    try:
        source = config.resolve_voicememo(args.source)
        if args.baseline:
            state_path = Path(args.state).expanduser() if args.state else config.default_voice_state()
            print("\n".join(run_baseline(source, state_path)))
            return 0
        archive = config.resolve_archive(args.archive)
        cards = config.resolve_cards(args.cards, args.vault)
    except config.ConfigError as e:
        print(str(e), file=sys.stderr)
        return 2

    state_path = Path(args.state).expanduser() if args.state else config.default_voice_state()
    lines = run_pass(source, archive, cards, state_path, use_llm=not args.no_llm)
    print("\n".join(lines) if lines else "新着なし")
    return 0


if __name__ == "__main__":
    sys.exit(main())
