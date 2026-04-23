#!/usr/bin/env python3
"""
V0 考卷檢討影片生成器
流程:JSON → edge-tts 音檔 → 逐幀 PNG → ffmpeg 合成 MP4
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
from pathlib import Path

# Windows 終端 cp950 不支援 emoji，強制 UTF-8
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from functools import lru_cache

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
# 主字型缺字 (如 ≤、≥、∫、⊥) 時退回這支。seguisym 內建於 Windows,覆蓋大量數學/符號
FALLBACK_FONT_PATH = os.environ.get("CLAUDE_FALLBACK_FONT_PATH", "C:/Windows/Fonts/seguisym.ttf")
PAUSE_AFTER_EACH = 0.6  # 每步驟結束後停頓秒數

BASE_DIR = Path(__file__).parent
WORK_DIR = BASE_DIR / "work"
OUTPUT_DIR = BASE_DIR / "output"
PRONUNCIATION_MAP_PATH = BASE_DIR / "pronunciation.json"
PIPELINE_CONFIG_PATH = BASE_DIR / "pipeline_config.json"


# ---------- 發音前處理 ----------
_PRONUNCIATION_MAP: list[tuple[str, str]] | None = None


def _load_pronunciation_map() -> list[tuple[str, str]]:
    """載入 pronunciation.json 並依 key 長度由長到短排序 (longest-match)"""
    global _PRONUNCIATION_MAP
    if _PRONUNCIATION_MAP is None:
        if PRONUNCIATION_MAP_PATH.exists():
            raw = json.loads(PRONUNCIATION_MAP_PATH.read_text(encoding="utf-8"))
            items = [(k, v) for k, v in raw.items() if not k.startswith("_")]
            _PRONUNCIATION_MAP = sorted(items, key=lambda kv: -len(kv[0]))
        else:
            _PRONUNCIATION_MAP = []
    return _PRONUNCIATION_MAP


def _strip_outer_parens(s: str) -> str:
    s = s.strip()
    if s.startswith("(") and s.endswith(")"):
        return s[1:-1]
    return s


def _rewrite_fractions(text: str) -> str:
    """把「1/(s+3)」這類有括號的分式改成「s+3 分之 1」以便 TTS 唸對。
    只處理至少一邊帶括號的情況,避免誤中 kg/m²、URL、日期等非數學用途。"""
    # NUM / (DENOM) 形式
    text = re.sub(
        r"([\w\d]+|\([^()]+\))\s*/\s*\(([^()]+)\)",
        lambda m: f"{m.group(2)} 分之 {_strip_outer_parens(m.group(1))}",
        text,
    )
    # (NUM) / DENOM 形式(單邊括號,DENOM 不能再帶括號避免二次吃到)
    text = re.sub(
        r"\(([^()]+)\)\s*/\s*([A-Za-z_]\w*|\d+)",
        lambda m: f"{m.group(2)} 分之 {m.group(1)}",
        text,
    )
    return text


def _rewrite_alphanum(text: str) -> str:
    """把 F1, F2, x1, y2 等字母加數字的組合轉換成中文唸法,避免 TTS 唸英文。"""
    mapping = {
        "0": "零", "1": "一", "2": "二", "3": "三", "4": "四",
        "5": "五", "6": "六", "7": "七", "8": "八", "9": "九"
    }

    def repl(m):
        letter = m.group(1)
        num = m.group(2)
        # 如果是像是 10, 15 這種多位數,暫不處理或逐字轉
        # 這裡採取逐字轉, F12 -> F 一 二
        num_cn = "".join(mapping.get(c, c) for c in num)
        return f"{letter} {num_cn}"

    # 尋找字母後連著數字的 pattern, 排除像是 100mm 這種單位
    # 只針對 F, P, x, y, z, u, v, Q, T 等常見力學符號
    return re.sub(r"([FPxyzuvQT])(\d+)", repl, text)


def normalize_for_tts(text: str) -> str:
    """把數學/希臘符號替換成 TTS 拼音。替換時前後補 space 避免黏字,最後把多重空白壓成單一。
    SRT 字幕不走這層,仍保留原符號。"""
    text = _rewrite_fractions(text)
    text = _rewrite_alphanum(text)
    for src, dst in _load_pronunciation_map():
        text = text.replace(src, f" {dst} ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ---------- TTS ----------
_TTS_BACKEND: TTSBackend | None = None


def _get_tts_backend() -> TTSBackend:
    """延遲載入 backend (讀 tts_config.json)"""
    global _TTS_BACKEND
    if _TTS_BACKEND is None:
        _TTS_BACKEND = load_tts_backend()
    return _TTS_BACKEND


async def gen_tts(text: str, out_path: Path):
    """透過設定檔選擇的 backend 合成語音;失敗會自動 fallback 到 edge"""
    text = normalize_for_tts(text)
    backend = _get_tts_backend()
    ok = await backend.synthesize(text, out_path)
    if not ok:
        raise RuntimeError(f"TTS 全部 backend 都失敗: {text[:50]}")


def mp3_duration(path: Path) -> float:
    return MP3(str(path)).info.length


# ---------- 字型 fallback ----------
# 為什麼:msjh.ttc 缺 ≤、≥、∫、⊥… 等數學符號,直接畫會變 tofu (□)
# 策略:載入主/副字型 cmap,逐字元決定用哪支字型,副字型同 size 快取


@lru_cache(maxsize=None)
def _font_codepoints(path: str) -> frozenset[int]:
    """回傳字型支援的 Unicode codepoint 集合;ttc 取所有 subfont 聯集"""
    try:
        from fontTools.ttLib import TTCollection, TTFont
    except ImportError:
        return frozenset()
    try:
        if path.lower().endswith(".ttc"):
            coll = TTCollection(path)
            cps: set[int] = set()
            for f in coll.fonts:
                cps.update(f.getBestCmap().keys())
            return frozenset(cps)
        return frozenset(TTFont(path).getBestCmap().keys())
    except Exception:
        return frozenset()


@lru_cache(maxsize=None)
def _get_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def draw_text_mixed(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    main_font: ImageFont.FreeTypeFont,
    fill,
):
    """主字型缺 glyph 的字元改用 fallback 字型畫,逐字元推進 x 座標"""
    main_cps = _font_codepoints(FONT_PATH)
    fb_cps = _font_codepoints(FALLBACK_FONT_PATH)
    # cmap 讀不到 (fontTools 未裝或字型壞) → 退化成原本行為,全部用主字型
    if not main_cps:
        draw.text(xy, text, font=main_font, fill=fill)
        return

    x, y = xy
    size = main_font.size
    fb_font = _get_font(FALLBACK_FONT_PATH, size)
    for ch in text:
        cp = ord(ch)
        if cp in main_cps:
            font = main_font
        elif fb_cps and cp in fb_cps:
            font = fb_font
        else:
            font = main_font  # 兩邊都沒,畫成 tofu 也只能這樣
        draw.text((x, y), ch, font=font, fill=fill)
        x += int(font.getlength(ch))


def wrap_text_for_font(
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    """CJK-aware 貪婪換行:逐字元累積,超寬就切行。保留顯式 \\n"""
    lines: list[str] = []
    for raw_line in text.split("\n"):
        if not raw_line:
            lines.append("")
            continue
        buf = ""
        for ch in raw_line:
            if font.getlength(buf + ch) > max_width and buf:
                lines.append(buf)
                buf = ch
            else:
                buf += ch
        if buf:
            lines.append(buf)
    return lines


def draw_text_mixed_wrapped(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    main_font: ImageFont.FreeTypeFont,
    fill,
    max_width: int,
    line_height: int,
) -> int:
    """畫會換行的混合字型文字,回傳下一個 y 座標(供後續內容定位)"""
    wrapped = wrap_text_for_font(text, main_font, max_width)
    x, y = xy
    for ln in wrapped:
        draw_text_mixed(draw, (x, y), ln, main_font, fill)
        y += line_height
    return y


# ---------- 視覺設定(pipeline_config.json)----------
_PIPELINE_CONFIG: dict | None = None


def _get_pipeline_config() -> dict:
    global _PIPELINE_CONFIG
    if _PIPELINE_CONFIG is None:
        if PIPELINE_CONFIG_PATH.exists():
            _PIPELINE_CONFIG = json.loads(PIPELINE_CONFIG_PATH.read_text(encoding="utf-8"))
        else:
            _PIPELINE_CONFIG = {}
    return _PIPELINE_CONFIG


def _prepare_dynamic_avatar(cfg: dict):
    """將用戶提供的張/合嘴頭像加上圓角和邊框,存為 transparent PNG 供 FFmpeg overlay"""
    if not cfg.get("enabled"): return
    size = int(cfg.get("size", 220))
    border_w = int(cfg.get("border_width", 3))
    shape = cfg.get("shape", "circle")
    
    for key, out_name in [("path_closed", "avatar_closed.png"), ("path_open", "avatar_open.png")]:
        in_path = Path(cfg.get(key, ""))
        out_path = WORK_DIR / out_name
        if not in_path.exists():
            print(f"[warning] dynamic_avatar {key} 找不到: {in_path}")
            continue
            
        try:
            photo = Image.open(in_path).convert("RGBA").resize((size, size), Image.LANCZOS)
            out = Image.new("RGBA", (size + border_w*2, size + border_w*2), (0,0,0,0))
            
            mask = Image.new("L", (size, size), 0)
            md = ImageDraw.Draw(mask)
            if shape == "circle":
                md.ellipse([0, 0, size, size], fill=255)
            else:
                md.rectangle([0, 0, size, size], fill=255)
                
            out.paste(photo, (border_w, border_w), mask=mask)
            
            if border_w > 0:
                bd = ImageDraw.Draw(out)
                box = [border_w//2, border_w//2, size+border_w*1.5, size+border_w*1.5]
                if shape == "circle":
                    bd.ellipse(box, outline=CHALK_WHITE, width=border_w)
                else:
                    bd.rectangle(box, outline=CHALK_WHITE, width=border_w)
            out.save(out_path, "PNG")
        except Exception as e:
            print(f"[warning] 處理 {key} 失敗: {e}")


def _build_avatar_concat(audio_path: Path, out_txt: Path, duration: float, cfg: dict):
    wav_path = out_txt.with_suffix(".wav")
    # Convert MP3 to WAV (16kHz, mono) for fast RMS analysis
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(audio_path), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav_path)], check=True)
    
    path_closed = (WORK_DIR / "avatar_closed.png").absolute().as_posix().replace('\\', '/')
    path_open = (WORK_DIR / "avatar_open.png").absolute().as_posix().replace('\\', '/')
    threshold = cfg.get("volume_threshold", 500)
    
    with wave.open(str(wav_path), 'rb') as w:
        n_frames = w.getnframes()
        data = w.readframes(n_frames)
        
    samples = struct.unpack(f"<{n_frames}h", data)
    chunk_frames = 1600  # 10 fps (0.1s chunks)
    
    lines = []
    for i in range(0, n_frames, chunk_frames):
        chunk = samples[i:i+chunk_frames]
        if not chunk: break
        rms = math.sqrt(sum(s*s for s in chunk) / len(chunk))
        is_open = rms > threshold
        img = path_open if is_open else path_closed
        lines.append(f"file '{img}'\nduration 0.1")
        
    lines.append(f"file '{path_closed}'\nduration {PAUSE_AFTER_EACH}")
    lines.append(f"file '{path_closed}'")
    out_txt.write_text("\n".join(lines), encoding="utf-8")
    wav_path.unlink(missing_ok=True)


def _overlay_teacher_photo(img: Image.Image):
    """右下角貼老師照片(配 pipeline_config.json 的 teacher_photo 區塊)"""
    dyn_cfg = _get_pipeline_config().get("dynamic_avatar", {})
    if dyn_cfg.get("enabled"): return # 如果開啟了動態頭像, 這裡就不繪製靜態圖

    cfg = _get_pipeline_config().get("teacher_photo", {}) or {}
    if not cfg.get("enabled"):
        return
    path = Path(cfg.get("path", ""))
    if not path.exists():
        print(f"[warning] teacher_photo.path 不存在: {path}")
        return
    try:
        size = int(cfg.get("size", 220))
        margin = int(cfg.get("margin", 40))
        shape = cfg.get("shape", "circle")
        border_w = int(cfg.get("border_width", 3))

        photo = Image.open(path).convert("RGBA").resize((size, size), Image.LANCZOS)

        mask = Image.new("L", (size, size), 0)
        md = ImageDraw.Draw(mask)
        if shape == "circle":
            md.ellipse([0, 0, size, size], fill=255)
        else:
            md.rectangle([0, 0, size, size], fill=255)

        px = WIDTH - size - margin
        py = HEIGHT - size - margin
        img.paste(photo, (px, py), mask=mask)

        if border_w > 0:
            bd = ImageDraw.Draw(img)
            box = [px - border_w, py - border_w, px + size + border_w, py + size + border_w]
            if shape == "circle":
                bd.ellipse(box, outline=CHALK_WHITE, width=border_w)
            else:
                bd.rectangle(box, outline=CHALK_WHITE, width=border_w)
    except Exception as e:
        print(f"[warning] 貼老師照片失敗: {e}")


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
    渲染第 N 幀:累積顯示前 steps_to_show 個步驟。
    - 步驟超過可視區會自動「滾動」,永遠保留最新 N 步(前面的滾出去)
    - step 可帶自己的 image 欄位,渲染時顯示目前為止最新出現過的 image
    - 底部 SUBTITLE_RESERVE 高度不繪製內容,留給字幕
    """
    img = Image.new("RGB", (WIDTH, HEIGHT), BG_COLOR)
    draw = ImageDraw.Draw(img)
    draw_board_border(draw)

    # 標題縮小當左上角標籤,題目才是主角
    title_font = ImageFont.truetype(FONT_PATH, 24)
    problem_font = ImageFont.truetype(FONT_PATH, 60)   # 相對放大
    step_font = ImageFont.truetype(FONT_PATH, 68)

    title_line_h = 30
    problem_line_h = 76
    step_line_h = 78
    step_gap = 30

    SUBTITLE_RESERVE = 220   # 底部留 220 px 給字幕
    STEP_Y_MAX = HEIGHT - SUBTITLE_RESERVE

    # 左上角小字標籤(title + 可選 subtitle),字級小、顏色較暗,不搶戲
    title_max_w = WIDTH - 160
    y_cursor = 25
    y_cursor = draw_text_mixed_wrapped(
        draw, (60, y_cursor), data.get("title", ""),
        title_font, CHALK_TITLE, title_max_w, title_line_h,
    )
    y_cursor = draw_text_mixed_wrapped(
        draw, (60, y_cursor), data.get("subtitle", ""),
        title_font, CHALK_TITLE, title_max_w, title_line_h,
    )

    # 題目(主角):下方多留一點空間,再開始
    problem_y = y_cursor + 20
    problem_max_w = WIDTH - 200
    next_y = draw_text_mixed_wrapped(
        draw, (100, problem_y), data["problem"],
        problem_font, CHALK_PROBLEM, problem_max_w, problem_line_h,
    )
    sep_y = next_y + 20
    draw.line([(80, sep_y), (WIDTH - 80, sep_y)], fill=CHALK_TITLE, width=2)

    # ---- 解題步驟:先預算每一步的繪製高度,再決定顯示哪些 ----
    steps = data["steps"][:steps_to_show]
    step_max_w = WIDTH - 300

    step_heights: list[int] = []
    for step in steps:
        wrapped = wrap_text_for_font(step.get("display", ""), step_font, step_max_w)
        h = max(1, len(wrapped)) * step_line_h + step_gap
        step_heights.append(h)

    # 滾動策略:固定優先塞入「最新」步驟,往前能塞多少塞多少,超過 STEP_Y_MAX 就丟掉
    step_y_start = sep_y + 40
    available = STEP_Y_MAX - step_y_start
    visible_indices: list[int] = []
    used = 0
    for idx in range(len(steps) - 1, -1, -1):
        h = step_heights[idx]
        if used + h > available and visible_indices:
            break
        visible_indices.insert(0, idx)
        used += h

    # 實際畫可見步驟(編號仍用原始序號)
    y = step_y_start
    for idx in visible_indices:
        step = steps[idx]
        is_latest = (idx == len(steps) - 1)
        color = CHALK_HIGHLIGHT if is_latest else CHALK_WHITE
        draw_text_mixed(draw, (100, y), f"{idx + 1}.", step_font, color)
        next_y = draw_text_mixed_wrapped(
            draw, (190, y), step.get("display", ""),
            step_font, color, step_max_w, step_line_h,
        )
        y = next_y + step_gap

    # ---- 圖片:最新帶 image 或 diagram_svg 的步驟優先 ----
    img_to_show: str | None = None
    svg_to_render: str | None = None
    
    for step in reversed(steps):
        if step.get("diagram_svg"):
            svg_to_render = step["diagram_svg"]
            break
        if step.get("image"):
            img_to_show = step["image"]
            break
            
    if not svg_to_render and not img_to_show and data.get("image"):
        img_to_show = data["image"]

    # 優先處理 SVG
    if svg_to_render:
        try:
            import cairosvg
            svg_tmp = WORK_DIR / f"temp_diagram_{steps_to_show:03d}.png"
            # 渲染 SVG 為透明 PNG, 放大 2 倍以確保清晰
            cairosvg.svg2png(bytestring=svg_to_render.encode("utf-8"), write_to=str(svg_tmp), scale=2.0)
            img_to_show = str(svg_tmp)
        except ImportError:
            print("[warning] 找不到 cairosvg,無法渲染 SVG 圖解。請執行 pip install cairosvg")
        except Exception as e:
            print(f"[warning] SVG 渲染失敗: {e}")

    if img_to_show:
        img_path = Path(img_to_show)
        if img_path.exists():
            try:
                paste_img = Image.open(img_path)
                # 高度上限也要避開字幕區
                max_img_h = STEP_Y_MAX - (sep_y + 60)
                # 圖解通常較寬, 給予較大寬度限制
                paste_img.thumbnail((800, max(200, max_img_h)))

                paste_x = WIDTH - paste_img.width - 100
                paste_y = sep_y + 60

                # 只有一般圖片才加白框, SVG 繪製的圖解保持透明背景
                if not svg_to_render:
                    pad = 10
                    draw.rectangle(
                        [paste_x - pad, paste_y - pad,
                         paste_x + paste_img.width + pad, paste_y + paste_img.height + pad],
                        fill="white", outline=CHALK_WHITE, width=4
                    )

                if paste_img.mode in ("RGBA", "LA") or (
                    paste_img.mode == "P" and "transparency" in paste_img.info
                ):
                    alpha = paste_img.convert("RGBA").split()[-1]
                    img.paste(paste_img, (paste_x, paste_y), mask=alpha)
                else:
                    img.paste(paste_img, (paste_x, paste_y))
            except Exception as e:
                print(f"[warning] 無法載入圖片 {img_path}: {e}")

    # 右下角老師照片(若 pipeline_config.json 啟用)
    _overlay_teacher_photo(img)

    img.save(out_path, "PNG")


# ---------- FFmpeg 合成 ----------
def build_clip(frame_path: Path, audio_path: Path, duration: float, out_path: Path):
    """單一步驟 → 一段 mp4 clip (圖片 + 音檔,含結尾停頓)"""
    cfg = _get_pipeline_config()
    dyn_cfg = cfg.get("dynamic_avatar", {})
    sfx_cfg = cfg.get("chalk_sfx", {})
    
    dyn_enabled = dyn_cfg.get("enabled", False)
    if dyn_enabled and not ((WORK_DIR / "avatar_closed.png").exists() and (WORK_DIR / "avatar_open.png").exists()):
        dyn_enabled = False

    sfx_enabled = sfx_cfg.get("enabled", False)
    sfx_path = Path(sfx_cfg.get("path", ""))
    sfx_volume = sfx_cfg.get("volume", 0.15)

    total_duration = duration + PAUSE_AFTER_EACH
    
    # 基本音訊處理鏈 (語音)
    af_narration = f"aresample=44100,loudnorm=I=-16:TP=-1.5:LRA=11"
    
    inputs = [
        "-loop", "1", "-t", f"{total_duration:.3f}", "-i", str(frame_path),
        "-i", str(audio_path)
    ]
    
    next_in_idx = 2
    sfx_in_idx = -1
    avatar_in_idx = -1

    if sfx_enabled and sfx_path.exists():
        inputs += ["-stream_loop", "-1", "-i", str(sfx_path)]
        sfx_in_idx = next_in_idx
        next_in_idx += 1

    if dyn_enabled:
        avatar_txt = WORK_DIR / f"avatar_{audio_path.stem}.txt"
        _build_avatar_concat(audio_path, avatar_txt, duration, dyn_cfg)
        inputs += ["-f", "concat", "-safe", "0", "-i", str(avatar_txt)]
        avatar_in_idx = next_in_idx
        next_in_idx += 1

    # Audio filtering
    if sfx_in_idx != -1:
        a_filter = (
            f"[1:a]{af_narration}[voice];"
            f"[{sfx_in_idx}:a]volume={sfx_volume},atrim=0:{total_duration:.3f}[bg];"
            f"[voice][bg]amix=inputs=2:duration=first:dropout_transition=0.5,apad=pad_dur={PAUSE_AFTER_EACH}[out_a]"
        )
    else:
        a_filter = f"[1:a]{af_narration},apad=pad_dur={PAUSE_AFTER_EACH}[out_a]"

    # Video filtering
    if dyn_enabled:
        size = int(dyn_cfg.get("size", 220))
        margin = int(dyn_cfg.get("margin", 40))
        border_w = int(dyn_cfg.get("border_width", 3))
        px = WIDTH - size - margin - border_w
        py = HEIGHT - size - margin - border_w
        # 將 avatar 蓋在黑板(0:v)上
        v_filter = f"[{avatar_in_idx}:v]format=rgba[ava];[0:v][ava]overlay=x={px}:y={py}:eof_action=pass[out_v]"
    else:
        # 如果不需要 overlay
        v_filter = f"[0:v]copy[out_v]"
        # 原本不用 filter_complex 直接複製, 但為了統一邏輯這裡用 v_filter

    filter_complex = f"{a_filter};{v_filter}"
    
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error"
    ] + inputs + [
        "-filter_complex", filter_complex,
        "-map", "[out_v]", "-map", "[out_a]",
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
def _split_sentences(text: str) -> list[str]:
    """按中英文句末標點 (。！？!?) 切句,保留標點於句尾"""
    parts = re.split(r"(?<=[。！？!?])\s*", text)
    return [p.strip() for p in parts if p.strip()]


def build_srt(data: dict, durations: list[float], out_path: Path):
    """產生 SRT:每個步驟按 。！？ 拆成多個 cue,時長依字數比例分配"""
    def fmt(t: float) -> str:
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s = int(t % 60)
        ms = int((t - int(t)) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    lines: list[str] = []
    cue_num = 1
    t = 0.0
    for step, dur in zip(data["steps"], durations):
        sentences = _split_sentences(step.get("narration", ""))
        if not sentences:
            t += dur + PAUSE_AFTER_EACH
            continue
        total_chars = sum(len(s) for s in sentences) or 1
        sub_start = t
        for j, sent in enumerate(sentences):
            # 最後一句用剩餘時間,避免浮點累積誤差
            if j == len(sentences) - 1:
                sub_end = t + dur
            else:
                sub_end = sub_start + dur * (len(sent) / total_chars)
            lines.append(str(cue_num))
            lines.append(f"{fmt(sub_start)} --> {fmt(sub_end)}")
            lines.append(sent)
            lines.append("")
            cue_num += 1
            sub_start = sub_end
        t += dur + PAUSE_AFTER_EACH  # 下一步驟從停頓後開始
    out_path.write_text("\n".join(lines), encoding="utf-8")


# ---------- 主流程 ----------
async def main(json_path: str, out_name: str = "review"):
    WORK_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)
    data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    n = len(data["steps"])

    # 準備動態頭像資源
    _prepare_dynamic_avatar(_get_pipeline_config().get("dynamic_avatar", {}))

    print(f"[1/4] 生成 {n} 段旁白音檔...")
    audio_files = []
    for i, step in enumerate(data["steps"]):
        ap = WORK_DIR / f"audio_{i:03d}.mp3"
        await gen_tts(step["narration"], ap)
        audio_files.append(ap)
        print(f"   ✓ step {i+1}: {mp3_duration(ap):.2f}s  (backend={_get_tts_backend().name})")

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