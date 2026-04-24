#!/usr/bin/env python3
"""
V0 考卷檢討影片生成器
流程:JSON -> TTS 音檔 -> 逐幀 PNG -> FFmpeg 合成 MP4
"""
import asyncio
import json
import os
import re
import subprocess
import sys
import wave
import struct
import math
import shutil
from pathlib import Path
from functools import lru_cache

# Windows 終端 cp950 不支援 emoji，強制 UTF-8
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from PIL import Image, ImageDraw, ImageFont
from mutagen.mp3 import MP3
from tts_backend import TTSBackend, load_tts_backend

# ---------- 設定 ----------
WIDTH, HEIGHT = 1920, 1080
BG_COLOR = (30, 58, 46)         # 深綠黑板
CHALK_WHITE = (232, 230, 216)   # 粉筆白 (舊步驟)
CHALK_HIGHLIGHT = (255, 217, 107)  # 粉筆黃 (最新步驟)
CHALK_TITLE = (180, 220, 200)   # 粉筆青 (標題)
CHALK_PROBLEM = (255, 200, 140) # 粉筆橙 (題目)
BORDER_COLOR = (60, 90, 75)     # 黑板邊框

FONT_PATH = os.environ.get("CLAUDE_FONT_PATH", "C:/Windows/Fonts/msjh.ttc")
FALLBACK_FONT_PATH = os.environ.get("CLAUDE_FALLBACK_FONT_PATH", "C:/Windows/Fonts/seguisym.ttf")
PAUSE_AFTER_EACH = 0.6

BASE_DIR = Path(__file__).parent
WORK_DIR = BASE_DIR / "work"
OUTPUT_DIR = BASE_DIR / "output"
PRONUNCIATION_MAP_PATH = BASE_DIR / "pronunciation.json"
PIPELINE_CONFIG_PATH = BASE_DIR / "pipeline_config.json"

# ---------- 前處理 ----------
_PRONUNCIATION_MAP = None

def _load_pronunciation_map():
    global _PRONUNCIATION_MAP
    if _PRONUNCIATION_MAP is None:
        if PRONUNCIATION_MAP_PATH.exists():
            raw = json.loads(PRONUNCIATION_MAP_PATH.read_text(encoding="utf-8"))
            _PRONUNCIATION_MAP = sorted([(k, v) for k, v in raw.items() if not k.startswith("_")], key=lambda x: -len(x[0]))
        else: _PRONUNCIATION_MAP = []
    return _PRONUNCIATION_MAP

def normalize_for_tts(text):
    text = re.sub(r"([\w\d]+|\([^()]+\))\s*/\s*\(([^()]+)\)", lambda m: f"{m.group(2)} 分之 {m.group(1).strip('()')}", text)
    text = re.sub(r"\(([^()]+)\)\s*/\s*([A-Za-z_]\w*|\d+)", lambda m: f"{m.group(2)} 分之 {m.group(1)}", text)
    mapping = {"0":"零","1":"一","2":"二","3":"三","4":"四","5":"五","6":"六","7":"七","8":"八","9":"九"}
    text = re.sub(r"([FPxyzuvQT])(\d+)", lambda m: f"{m.group(1)} {''.join(mapping.get(c,c) for c in m.group(2))}", text)
    for src, dst in _load_pronunciation_map(): text = text.replace(src, f" {dst} ")
    return re.sub(r"\s+", " ", text).strip()

# ---------- TTS ----------
_TTS_BACKEND = None
def _get_tts_backend():
    global _TTS_BACKEND
    if _TTS_BACKEND is None: _TTS_BACKEND = load_tts_backend()
    return _TTS_BACKEND

async def gen_tts(text, out_path):
    text = normalize_for_tts(text)
    if not await _get_tts_backend().synthesize(text, out_path):
        raise RuntimeError(f"TTS Failed: {text[:50]}")

def mp3_duration(path): return MP3(str(path)).info.length

# ---------- 繪圖輔助 ----------
@lru_cache(None)
def _get_font(path, size): return ImageFont.truetype(path, size)

@lru_cache(None)
def _font_cps(path):
    try:
        from fontTools.ttLib import TTCollection, TTFont
        if path.lower().endswith(".ttc"):
            return frozenset().union(*(f.getBestCmap().keys() for f in TTCollection(path).fonts))
        return frozenset(TTFont(path).getBestCmap().keys())
    except: return frozenset()

def draw_text_mixed(draw, xy, text, main_font, fill):
    m_cps, f_cps = _font_cps(FONT_PATH), _font_cps(FALLBACK_FONT_PATH)
    x, y = xy
    fb_font = _get_font(FALLBACK_FONT_PATH, main_font.size)
    for ch in text:
        font = fb_font if (ord(ch) in f_cps and ord(ch) not in m_cps) else main_font
        draw.text((x, y), ch, font=font, fill=fill)
        x += int(font.getlength(ch))

def wrap_text(text, font, max_w):
    lines = []
    for raw in text.split("\n"):
        buf = ""
        for ch in raw:
            if font.getlength(buf + ch) > max_w and buf: lines.append(buf); buf = ch
            else: buf += ch
        if buf: lines.append(buf)
    return lines

def draw_text_wrapped(draw, xy, text, font, fill, max_w, line_h):
    wrapped = wrap_text(text, font, max_w)
    x, y = xy
    for ln in wrapped:
        draw_text_mixed(draw, (x, y), ln, font, fill)
        y += line_h
    return y

# ---------- 視覺設定 ----------
_PIPELINE_CONFIG = None
def _get_pipeline_config():
    global _PIPELINE_CONFIG
    if _PIPELINE_CONFIG is None:
        if PIPELINE_CONFIG_PATH.exists():
            _PIPELINE_CONFIG = json.loads(PIPELINE_CONFIG_PATH.read_text(encoding="utf-8"))
        else: _PIPELINE_CONFIG = {}
    return _PIPELINE_CONFIG

# ---------- 動態頭像 ----------
def _prepare_dynamic_avatar(cfg):
    if not cfg.get("enabled"): return
    WORK_DIR.mkdir(exist_ok=True)
    size, bw, shape = int(cfg.get("size", 220)), int(cfg.get("border_width", 3)), cfg.get("shape", "circle")
    tasks = [("path_closed", "avatar_closed.png")] + [(f"talking_{i}", f"avatar_talking_{i}.png", p) for i, p in enumerate(cfg.get("paths_talking", []))]
    for t in tasks:
        in_p = Path(t[2] if len(t)==3 else cfg.get(t[0], ""))
        if not in_p.exists(): continue
        photo = Image.open(in_p).convert("RGBA").resize((size, size), Image.LANCZOS)
        out = Image.new("RGBA", (size+bw*2, size+bw*2), (0,0,0,0))
        mask = Image.new("L", (size, size), 0); md = ImageDraw.Draw(mask)
        if shape == "circle": md.ellipse([0, 0, size, size], fill=255)
        else: md.rectangle([0, 0, size, size], fill=255)
        out.paste(photo, (bw, bw), mask=mask)
        if bw > 0:
            bd = ImageDraw.Draw(out); box = [bw//2, bw//2, size+bw*1.5, size+bw*1.5]
            if shape == "circle": bd.ellipse(box, outline=CHALK_WHITE, width=bw)
            else: bd.rectangle(box, outline=CHALK_WHITE, width=bw)
        out.save(WORK_DIR / t[1], "PNG")

def _build_avatar_concat(audio_p, out_txt, dur, cfg, q_work):
    wav_p = out_txt.with_suffix(".wav")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(audio_p), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav_p)], check=True)
    p_closed = (WORK_DIR / "avatar_closed.png").absolute().as_posix().replace('\\', '/')
    threshold = cfg.get("volume_threshold", 500)
    talking = []
    i = 0
    while (WORK_DIR / f"avatar_talking_{i}.png").exists():
        talking.append((WORK_DIR / f"avatar_talking_{i}.png").absolute().as_posix().replace('\\', '/')); i += 1
    if not talking: talking = [p_closed]
    with wave.open(str(wav_p), 'rb') as w:
        samples = struct.unpack(f"<{w.getnframes()}h", w.readframes(w.getnframes()))
    chunk, lines, idx = 2400, [], 0
    for i in range(0, len(samples), chunk):
        s = samples[i:i+chunk]; rms = math.sqrt(sum(x*x for x in s)/len(s)) if s else 0
        img = talking[idx % len(talking)] if rms > threshold else p_closed
        if rms > threshold: idx += 1
        lines.append(f"file '{img}'\nduration 0.15")
    lines += [f"file '{p_closed}'\nduration {PAUSE_AFTER_EACH}", f"file '{p_closed}'"]
    out_txt.write_text("\n".join(lines), encoding="utf-8")
    wav_p.unlink(missing_ok=True)

def _overlay_teacher_photo(img):
    cfg = _get_pipeline_config()
    if cfg.get("dynamic_avatar", {}).get("enabled"): return
    tp = cfg.get("teacher_photo", {})
    if not tp.get("enabled"): return
    path = Path(tp.get("path",""))
    if not path.exists(): return
    try:
        size, margin, shape, bw = int(tp.get("size", 220)), int(tp.get("margin", 40)), tp.get("shape", "circle"), int(tp.get("border_width", 3))
        photo = Image.open(path).convert("RGBA").resize((size, size), Image.LANCZOS)
        mask = Image.new("L", (size, size), 0); md = ImageDraw.Draw(mask)
        if shape == "circle": md.ellipse([0, 0, size, size], fill=255)
        else: md.rectangle([0, 0, size, size], fill=255)
        px, py = WIDTH - size - margin, HEIGHT - size - margin
        img.paste(photo, (px, py), mask=mask)
        if bw > 0:
            bd = ImageDraw.Draw(img); box = [px-bw, py-bw, px+size+bw, py+size+bw]
            if shape == "circle": bd.ellipse(box, outline=CHALK_WHITE, width=bw)
            else: bd.rectangle(box, outline=CHALK_WHITE, width=bw)
    except: pass

# ---------- 渲染與合成 ----------
def render_frame(data, step_idx, out_p, q_work):
    img = Image.new("RGB", (WIDTH, HEIGHT), BG_COLOR); draw = ImageDraw.Draw(img)
    for i in range(8): draw.rectangle([i, i, WIDTH-1-i, HEIGHT-1-i], outline=BORDER_COLOR)
    title_f, prob_f, step_f = _get_font(FONT_PATH, 24), _get_font(FONT_PATH, 60), _get_font(FONT_PATH, 68)
    STEP_Y_MAX = HEIGHT - 220
    draw.rectangle([0, STEP_Y_MAX, WIDTH, HEIGHT], fill=(0, 0, 0, 180))
    y = draw_text_wrapped(draw, (60, 25), data.get("title", ""), title_f, CHALK_TITLE, WIDTH-160, 30)
    y = draw_text_wrapped(draw, (60, y), data.get("subtitle", ""), title_f, CHALK_TITLE, WIDTH-160, 30)
    sep_y = draw_text_wrapped(draw, (100, y+20), data["problem"], prob_f, CHALK_PROBLEM, WIDTH-200, 76) + 20
    draw.line([(80, sep_y), (WIDTH-80, sep_y)], fill=CHALK_TITLE, width=2)
    steps = data["steps"][:step_idx]
    h_list = [max(1, len(wrap_text(s.get("display", ""), step_f, WIDTH-300)))*78+30 for s in steps]
    vis, used, cur_y = [], 0, sep_y+40
    for i in range(len(steps)-1, -1, -1):
        if used + h_list[i] > (STEP_Y_MAX - cur_y) and vis: break
        vis.insert(0, i); used += h_list[i]
    for i in vis:
        c = CHALK_HIGHLIGHT if i==len(steps)-1 else CHALK_WHITE
        draw_text_mixed(draw, (100, cur_y), f"{i+1}.", step_f, c)
        cur_y = draw_text_wrapped(draw, (190, cur_y), steps[i].get("display", ""), step_f, c, WIDTH-300, 78) + 30
    svg_code, img_show = None, None
    for s in reversed(steps):
        if s.get("diagram_svg"): svg_code = s["diagram_svg"]; break
        if s.get("image"): img_show = s["image"]; break
    if not svg_code and not img_show: img_show = data.get("image")
    if svg_code:
        try:
            import cairosvg
            tmp = q_work / f"svg_{step_idx:03d}.png"
            cairosvg.svg2png(bytestring=svg_code.encode("utf-8"), write_to=str(tmp), scale=2.0)
            img_show = str(tmp)
        except Exception as e: print(f"SVG Error: {e}")
    if img_show and Path(img_show).exists():
        p_img = Image.open(img_show); p_img.thumbnail((800, max(200, STEP_Y_MAX-(sep_y+60))))
        px, py = WIDTH-p_img.width-100, sep_y+60
        if not svg_code: draw.rectangle([px-10,py-10,px+p_img.width+10,py+p_img.height+10], fill="white", outline=CHALK_WHITE, width=4)
        img.paste(p_img, (px, py), mask=p_img.convert("RGBA").split()[-1] if p_img.mode in ("RGBA","LA") else None)
    _overlay_teacher_photo(img); img.save(out_p, "PNG")

def build_clip(f_p, a_p, dur, out_p, q_work):
    cfg = _get_pipeline_config(); dyn, sfx = cfg.get("dynamic_avatar",{}), cfg.get("chalk_sfx",{})
    total = dur + PAUSE_AFTER_EACH
    inputs = ["-loop", "1", "-t", f"{total:.3f}", "-i", str(f_p), "-i", str(a_p)]
    sfx_idx, ava_idx, next_idx = -1, -1, 2
    if sfx.get("enabled") and Path(sfx.get("path","")).exists():
        inputs += ["-stream_loop", "-1", "-i", sfx["path"]]; sfx_idx = next_idx; next_idx += 1
    if dyn.get("enabled") and (WORK_DIR/"avatar_closed.png").exists():
        ava_txt = q_work / f"avatar_{a_p.stem}.txt"
        _build_avatar_concat(a_p, ava_txt, dur, dyn, q_work)
        inputs += ["-f", "concat", "-safe", "0", "-i", str(ava_txt)]; ava_idx = next_idx; next_idx += 1
    
    a_f = "[1:a]aresample=44100,loudnorm=I=-16:TP=-1.5:LRA=11[a_norm]"
    if sfx_idx != -1:
        a_f += f";[{sfx_idx}:a]volume={sfx['volume']},atrim=0:{total:.3f}[bg_sfx]"
        a_f += f";[a_norm][bg_sfx]amix=inputs=2:duration=first[a_mixed]"
        a_final = "[a_mixed]"
    else:
        a_final = "[a_norm]"
    a_f += f";{a_final}apad=pad_dur={PAUSE_AFTER_EACH}[out_a]"

    if ava_idx != -1:
        s, m, bw = int(dyn.get("size",220)), int(dyn.get("margin",40)), int(dyn.get("border_width",3))
        v_f = f"[{ava_idx}:v]format=rgba[ava];[0:v][ava]overlay=x={WIDTH-s-m-bw}:y={HEIGHT-s-m-bw}:eof_action=pass[out_v]"
    else: v_f = "[0:v]copy[out_v]"
    
    cmd = ["ffmpeg", "-y", "-loglevel", "error"] + inputs + [
        "-filter_complex", f"{a_f};{v_f}",
        "-map", "[out_v]", "-map", "[out_a]",
        "-c:v", "libx264", "-tune", "stillimage", "-c:a", "aac", "-pix_fmt", "yuv420p",
        "-t", f"{total:.3f}", "-r", "30", str(out_p)
    ]
    subprocess.run(cmd, check=True)

async def main(json_path, out_name, start_step=None):
    q_work = WORK_DIR / out_name; q_work.mkdir(parents=True, exist_ok=True)
    if start_step is None: # 全量渲染時，清除該目錄下的舊影片，避免快取誤用
        for old_clip in q_work.glob("clip_*.mp4"): old_clip.unlink()

    data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    _prepare_dynamic_avatar(_get_pipeline_config().get("dynamic_avatar",{}))
    audios, frames, clips, durs = [], [], [], []
    for i, s in enumerate(data["steps"]):
        ap, fp, cp = q_work/f"audio_{i:03d}.mp3", q_work/f"frame_{i:03d}.png", q_work/f"clip_{i:03d}.mp4"
        if start_step is None or i+1 == start_step or not ap.exists(): await gen_tts(s["narration"], ap)
        audios.append(ap); d = mp3_duration(ap); durs.append(d)
        if start_step is None or i+1 >= start_step or not fp.exists(): render_frame(data, i+1, fp, q_work)
        frames.append(fp)
        if start_step is None or i+1 >= start_step or not cp.exists(): build_clip(fp, ap, d, cp, q_work)
        clips.append(cp)
        
    list_f = q_work / "concat.txt"; list_f.write_text("\n".join(f"file '{p.absolute().as_posix().replace('\\', '/')}'" for p in clips), encoding="utf-8")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(list_f), "-c", "copy", str(OUTPUT_DIR / f"{out_name}.mp4")], check=True)
    
    srt, cue, t = [], 1, 0.0
    for s, d in zip(data["steps"], durs):
        sent = [p.strip() for p in re.split(r"(?<=[。！？!?])\s*", s.get("narration", "")) if p.strip()]
        if not sent: t += d + PAUSE_AFTER_EACH; continue
        tot, sub_s = sum(len(x) for x in sent), t
        for j, x in enumerate(sent):
            sub_e = t+d if j==len(sent)-1 else sub_s + d*(len(x)/tot)
            srt += [str(cue), f"{int(sub_s//3600):02d}:{int((sub_s%3600)//60):02d}:{int(sub_s%60):02d},{int((sub_s-int(sub_s))*1000):03d} --> {int(sub_e//3600):02d}:{int((sub_e%3600)//60):02d}:{int(sub_e%60):02d},{int((sub_e-int(sub_e))*1000):03d}", x, ""]
            cue += 1; sub_s = sub_e
        t += d + PAUSE_AFTER_EACH
    (OUTPUT_DIR / f"{out_name}.srt").write_text("\n".join(srt), encoding="utf-8")
    print(f"✅ 完成: {out_name}.mp4")

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("json_path"); ap.add_argument("out_name", nargs="?", default="review"); ap.add_argument("--step", type=int); ap.add_argument("--tts")
    args = ap.parse_args()
    if args.tts: os.environ["TTS_PROVIDER"] = args.tts
    asyncio.run(main(args.json_path, args.out_name, start_step=args.step))
