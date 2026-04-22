#!/usr/bin/env python3
"""從 YouTube (或其他 yt-dlp 支援的站) 抓音軌轉成 F5-TTS 可用的 WAV 參考檔。

用法:
    # 下載整段,同時預設截前 15 秒當 ref
    python tools/fetch_ref_voice.py "https://youtu.be/XXXX"

    # 指定截段 (30~45 秒,共 15 秒)
    python tools/fetch_ref_voice.py "https://youtu.be/XXXX" --start 30 --end 45

    # 自訂輸出位置
    python tools/fetch_ref_voice.py "URL" --start 10 --end 25 --output voices/dr_chen.wav

流程:
    URL -> yt-dlp 抓最佳 audio -> 全長 WAV (24 kHz mono) 存 voices/_source_<id>.wav
         -> ffmpeg trim [start, end] -> voices/teacher_ref.wav

F5-TTS ref 建議:
    - 10~20 秒
    - 單一說話者,沒有背景音樂/其他人聲
    - 有抑揚頓挫,不要太平淡
    - 截出來後自己聽一次,確認清楚才拿去用
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

# Windows 終端 cp950 不支援 emoji,強制 UTF-8
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent.parent
VOICES_DIR = BASE / "voices"
DEFAULT_OUTPUT = VOICES_DIR / "teacher_ref.wav"


def run(cmd: list[str], **kw):
    print(f"[run] {' '.join(cmd)}")
    subprocess.run(cmd, check=True, **kw)


def download_full(url: str) -> Path:
    """用 yt-dlp 抓最高品質音軌,轉 24k mono wav 存 voices/_source_<id>.wav"""
    VOICES_DIR.mkdir(exist_ok=True)
    out_template = str(VOICES_DIR / "_source_%(id)s.%(ext)s")
    # --no-playlist 防止一次抓整個 playlist
    run([
        "yt-dlp",
        "--no-playlist",
        "-x",                       # extract audio
        "--audio-format", "wav",
        "--postprocessor-args",
        "-ar 24000 -ac 1",          # 24 kHz mono,符合 F5-TTS 習慣
        "-o", out_template,
        url,
    ])
    # yt-dlp 結束後檔名會是 _source_<id>.wav
    candidates = sorted(VOICES_DIR.glob("_source_*.wav"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        sys.exit("❌ 沒找到下載完成的 wav,檢查 yt-dlp 輸出")
    return candidates[0]


def probe_duration(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True
    )
    return float(r.stdout.strip())


def trim(src: Path, start: float, end: float, dst: Path):
    """Copy segment [start, end) 到 dst (覆寫)"""
    dst.parent.mkdir(exist_ok=True)
    run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{start:.3f}",
        "-to", f"{end:.3f}",
        "-i", str(src),
        "-ar", "24000", "-ac", "1",
        "-c:a", "pcm_s16le",
        str(dst),
    ])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url", help="YouTube 或其他站的影片網址")
    ap.add_argument("--start", type=float, default=0.0, help="截段起點 (秒)")
    ap.add_argument("--end", type=float, default=15.0, help="截段終點 (秒)")
    ap.add_argument("--output", default=str(DEFAULT_OUTPUT),
                    help=f"ref wav 輸出位置 (預設 {DEFAULT_OUTPUT})")
    ap.add_argument("--skip-download", action="store_true",
                    help="已經下載過了,直接用 voices/_source_*.wav 最新一份去 trim")
    args = ap.parse_args()

    out_path = Path(args.output)

    if args.skip_download:
        src_candidates = sorted(VOICES_DIR.glob("_source_*.wav"),
                                key=lambda p: p.stat().st_mtime, reverse=True)
        if not src_candidates:
            sys.exit("❌ 找不到任何 voices/_source_*.wav,先不要加 --skip-download")
        src = src_candidates[0]
    else:
        src = download_full(args.url)

    dur = probe_duration(src)
    print(f"\n[info] 來源 WAV: {src}")
    print(f"[info] 長度: {dur:.1f} 秒")

    if args.end > dur:
        print(f"[warn] --end {args.end} > 檔案長度 {dur:.1f},改成 {dur:.1f}")
        args.end = dur
    if args.start >= args.end:
        sys.exit(f"❌ --start ({args.start}) 必須小於 --end ({args.end})")

    clip_len = args.end - args.start
    if clip_len < 3:
        print(f"[warn] 截段只有 {clip_len:.1f} 秒,F5-TTS 建議 10~20 秒")

    trim(src, args.start, args.end, out_path)
    print(f"\n✅ 完成:{out_path}  ({clip_len:.1f} 秒)")
    print(f"\n下一步:")
    print(f"  1. 聽看看 {out_path} 清楚嗎")
    print(f"  2. 把這段音檔的逐字稿填進 tts_config.json 的 f5.ref_text")
    print(f"  3. 想換段落直接 rerun,加 --skip-download --start X --end Y")


if __name__ == "__main__":
    main()
