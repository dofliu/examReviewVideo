#!/usr/bin/env python3
"""
V0 考卷檢討影片生成器
流程:JSON → edge-tts 音檔 → 逐幀 PNG → ffmpeg 合成 MP4
"""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

# Windows 終端 cp950 不支援 emoji，強制 UTF-8
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import edge_tts
from PIL import Image, ImageDraw, ImageFont
from mutagen.mp3 import MP3

# ---------- 設定 ----------
WIDTH, HEIGHT = 1920, 1080
BG_COLOR = (30, 58, 46)         # 深綠黑板
CHALK_WHITE = (232, 230, 216)   # 粉筆白 (舊步驟)
CHALK_HIGHLIGHT = (255, 217, 107)  # 粉筆黃 (最新步驟)
CHALK_TITLE = (180, 220, 200)   # 粉筆青 (標題)
CHALK_PROBLEM = (255, 200, 140) # 粉筆橙 (題目)
BORDER_COLOR = (60, 90, 75)     # 黑板邊框

FONT_PATH = os.environ.get("CLAUDE_FONT_PATH", "C:/Windows/Fonts/msjh.ttc")
VOICE = "zh-TW-HsiaoChenNeural"
RATE = "-5%"   # 講稍慢一點,老師口吻
PAUSE_AFTER_EACH = 0.6  # 每步驟結束後停頓秒數

WORK_DIR = Path(__file__).parent / "work"
OUTPUT_DIR = Path(__file__).parent / "output"


# ---------- TTS ----------
TTS_ENGINE = None  # 第一次呼叫時決定 (edge 或 espeak)


async def _try_edge_tts(text: str, out_path: Path) -> bool:
    try:
        communicate = edge_tts.Communicate(text, VOICE, rate=RATE)
        await communicate.save(str(out_path))
        return True
    except Exception:
        return False


def _espeak_tts(text: str, out_path: Path):
    """fallback: espeak-ng 離線中文 TTS (品質較機械化,僅用於沙箱 demo)"""
    wav_path = out_path.with_suffix(".wav")
    subprocess.run(
        ["espeak-ng", "-v", "cmn", "-s", "140", "-p", "45",
         text, "-w", str(wav_path)],
        check=True, capture_output=True
    )
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-i", str(wav_path), "-b:a", "128k", str(out_path)],
        check=True
    )
    wav_path.unlink()


async def gen_tts(text: str, out_path: Path):
    """優先用 edge-tts (自然中文),失敗則 fallback 到 espeak-ng"""
    global TTS_ENGINE
    if TTS_ENGINE is None:
        ok = await _try_edge_tts(text, out_path)
        TTS_ENGINE = "edge" if ok else "espeak"
        if ok:
            return
    if TTS_ENGINE == "edge":
        await _try_edge_tts(text, out_path)
    else:
        _espeak_tts(text, out_path)


def mp3_duration(path: Path) -> float:
    return MP3(str(path)).info.length


# ---------- 黑板渲染 ----------
def draw_board_border(draw: ImageDraw.ImageDraw):
    """畫黑板木框"""
    for i in range(8):
        draw.rectangle(
            [i, i, WIDTH - 1 - i, HEIGHT - 1 - i],
            outline=BORDER_COLOR, width=1
        )


def render_frame(
    data: dict,
    steps_to_show: int,
    out_path: Path
):
    """
    渲染第 N 幀:累積顯示前 steps_to_show 個步驟
    最新一步用黃色粉筆突顯
    """
    img = Image.new("RGB", (WIDTH, HEIGHT), BG_COLOR)
    draw = ImageDraw.Draw(img)
    draw_board_border(draw)

    title_font = ImageFont.truetype(FONT_PATH, 44)
    problem_font = ImageFont.truetype(FONT_PATH, 72)
    step_font = ImageFont.truetype(FONT_PATH, 68)

    # 左上角標題
    draw.text((80, 60), data.get("title", ""), font=title_font, fill=CHALK_TITLE)
    draw.text((80, 115), data.get("subtitle", ""), font=title_font, fill=CHALK_TITLE)

    # 題目 (畫一條分隔線在下方)
    problem_y = 210
    draw.text((100, problem_y), data["problem"], font=problem_font, fill=CHALK_PROBLEM)
    sep_y = problem_y + 100
    draw.line([(80, sep_y), (WIDTH - 80, sep_y)], fill=CHALK_TITLE, width=2)

    # 解題步驟 (累積)
    y = sep_y + 60
    steps = data["steps"][:steps_to_show]
    for i, step in enumerate(steps):
        is_latest = (i == len(steps) - 1)
        color = CHALK_HIGHLIGHT if is_latest else CHALK_WHITE
        # 步驟編號
        label = f"{i + 1}."
        draw.text((100, y), label, font=step_font, fill=color)
        # 內容
        draw.text((190, y), step["display"], font=step_font, fill=color)
        y += 130

    # 如果有附加圖片，顯示在右半邊
    if "image" in data and data["image"]:
        img_path = Path(data["image"])
        if img_path.exists():
            try:
                # 載入圖片並縮放到適合大小
                paste_img = Image.open(img_path)
                paste_img.thumbnail((750, 600))  # 保持比例縮放
                
                # 放置位置：畫面右側，分隔線下方
                paste_x = WIDTH - paste_img.width - 100
                paste_y = sep_y + 60
                
                # 畫一個白底的框框當作紙張，比較有融合感
                pad = 10
                draw.rectangle(
                    [paste_x - pad, paste_y - pad, 
                     paste_x + paste_img.width + pad, paste_y + paste_img.height + pad],
                    fill="white", outline=CHALK_WHITE, width=4
                )
                
                # 確保圖片可被貼上 (處理 RGBA 等)
                if paste_img.mode in ('RGBA', 'LA') or (paste_img.mode == 'P' and 'transparency' in paste_img.info):
                    alpha = paste_img.convert('RGBA').split()[-1]
                    img.paste(paste_img, (paste_x, paste_y), mask=alpha)
                else:
                    img.paste(paste_img, (paste_x, paste_y))
            except Exception as e:
                print(f"[warning] 無法載入圖片 {img_path}: {e}")

    img.save(out_path, "PNG")


# ---------- FFmpeg 合成 ----------
def build_clip(frame_path: Path, audio_path: Path, duration: float, out_path: Path):
    """單一步驟 → 一段 mp4 clip (圖片 + 音檔,含結尾停頓)"""
    # 在音檔後加 silence pad
    total_duration = duration + PAUSE_AFTER_EACH
    # 音訊處理鏈：resample 44.1kHz → 響度正規化 → 結尾靜音填充
    af_chain = (
        f"aresample=44100,"
        f"loudnorm=I=-16:TP=-1.5:LRA=11,"
        f"apad=pad_dur={PAUSE_AFTER_EACH}"
    )
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-loop", "1", "-i", str(frame_path),
        "-i", str(audio_path),
        "-af", af_chain,
        "-c:v", "libx264", "-tune", "stillimage",
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
        "-pix_fmt", "yuv420p",
        "-t", f"{total_duration:.3f}",
        "-r", "30",
        str(out_path),
    ]
    subprocess.run(cmd, check=True)


def concat_clips(clip_paths: list[Path], out_path: Path):
    """串接所有 clips"""
    list_file = WORK_DIR / "concat_list.txt"
    list_file.write_text(
        "\n".join(f"file '{p.absolute()}'" for p in clip_paths)
    )
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0",
        "-i", str(list_file),
        "-c", "copy",
        str(out_path),
    ]
    subprocess.run(cmd, check=True)


# ---------- 字幕 (SRT) ----------
def build_srt(data: dict, durations: list[float], out_path: Path):
    """產生 SRT 字幕檔,每段對應一個步驟的 narration"""
    def fmt(t: float) -> str:
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s = int(t % 60)
        ms = int((t - int(t)) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    lines = []
    t = 0.0
    for i, (step, dur) in enumerate(zip(data["steps"], durations)):
        start = t
        end = t + dur
        lines.append(f"{i + 1}")
        lines.append(f"{fmt(start)} --> {fmt(end)}")
        lines.append(step["narration"])
        lines.append("")
        t = end + PAUSE_AFTER_EACH  # 下一段從停頓後開始
    out_path.write_text("\n".join(lines), encoding="utf-8")


# ---------- 主流程 ----------
async def main(json_path: str, out_name: str = "review"):
    WORK_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)
    data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    n = len(data["steps"])

    print(f"[1/4] 生成 {n} 段旁白音檔...")
    audio_files = []
    for i, step in enumerate(data["steps"]):
        ap = WORK_DIR / f"audio_{i:03d}.mp3"
        await gen_tts(step["narration"], ap)
        audio_files.append(ap)
        print(f"   ✓ step {i+1}: {mp3_duration(ap):.2f}s  (engine={TTS_ENGINE})")

    print(f"[2/4] 渲染 {n} 幀黑板畫面...")
    frame_files = []
    for i in range(n):
        fp = WORK_DIR / f"frame_{i:03d}.png"
        render_frame(data, i + 1, fp)
        frame_files.append(fp)

    print(f"[3/4] 合成 {n} 段 clips...")
    clip_files = []
    durations = []
    for i in range(n):
        cp = WORK_DIR / f"clip_{i:03d}.mp4"
        dur = mp3_duration(audio_files[i])
        durations.append(dur)
        build_clip(frame_files[i], audio_files[i], dur, cp)
        clip_files.append(cp)

    print(f"[4/4] 串接輸出 MP4 + SRT 字幕...")
    final_mp4 = OUTPUT_DIR / f"{out_name}.mp4"
    final_srt = OUTPUT_DIR / f"{out_name}.srt"
    concat_clips(clip_files, final_mp4)
    build_srt(data, durations, final_srt)

    # 取得總長度
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(final_mp4)],
        capture_output=True, text=True, check=True
    )
    total = float(probe.stdout.strip())
    print(f"\n✅ 完成: {final_mp4} ({total:.1f}s)")
    print(f"   字幕: {final_srt}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("Usage: python pipeline.py <problem.json> [output_name]")
    json_path = sys.argv[1]
    out_name = sys.argv[2] if len(sys.argv) > 2 else "review_v0"
    asyncio.run(main(json_path, out_name))
