#!/usr/bin/env python3
"""
app.py — 考卷檢討影片系統 Web UI

使用:
    python3 app.py <exam.json>
    # 預設網址 http://localhost:5000

功能:
- 列出考卷中所有題目 + 渲染狀態
- 逐題編輯每個 step 的 display / narration
- 單題觸發 pipeline 渲染
- 內嵌播放完成的影片
"""
import argparse
import json
import re
import subprocess
import threading
import sys
from pathlib import Path
from datetime import datetime

# Windows 終端 cp950 不支援 emoji，強制 UTF-8
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from flask import Flask, request, jsonify, render_template_string, redirect, url_for, send_from_directory, abort

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32 MB

BASE_DIR = Path(__file__).parent
EXAMS_DIR = BASE_DIR / "exams"
PDFS_DIR = BASE_DIR / "pdfs"
SOLVE_SCRIPT = BASE_DIR / "solve.py"

# 全域狀態 (啟動時設定)
EXAM_PATH: Path | None = None     # 目前編輯中的 exam.json;None 代表未選
VIDEO_ROOT: Path | None = None    # 所有考卷影片的根目錄,例如 ./videos
RENDER_LOCK = threading.Lock()
RENDER_STATUS: dict = {}   # {pid: "idle" | "rendering" | "done" | "error"}
SOLVE_STATUS: dict = {}    # {stem: {"state": "solving"|"done"|"error", "msg": str}}


def current_exam_dir() -> Path:
    """當前編輯考卷對應的子目錄,例如 ./videos/real_exam/"""
    return VIDEO_ROOT / EXAM_PATH.stem


# ------------------ 啟動時的 migration / 設定 ------------------
# 把 repo root 散落的 exam JSONs 搬到 exams/ 集中管理。
# 判準:JSON 有 problems 這個 key (list) → 當作 exam
CONFIG_JSON_NAMES = {"tts_config.json", "pipeline_config.json", "pronunciation.json"}


def _looks_like_exam_json(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return isinstance(data, dict) and isinstance(data.get("problems"), list)


def migrate_root_exams() -> list[Path]:
    """啟動時掃 repo root 的 *.json,把 exam 類型的搬進 exams/。回傳搬移後的新路徑"""
    EXAMS_DIR.mkdir(exist_ok=True)
    moved: list[Path] = []
    for p in BASE_DIR.glob("*.json"):
        if p.name in CONFIG_JSON_NAMES:
            continue
        if not _looks_like_exam_json(p):
            continue
        dst = EXAMS_DIR / p.name
        if dst.exists():
            # 同名已存在,避免覆蓋;加時間戳
            dst = EXAMS_DIR / f"{p.stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        p.rename(dst)
        moved.append(dst)
        print(f"[migrate] {p.name} -> {dst.relative_to(BASE_DIR)}")
    return moved


# ------------------ 檔名清理 ------------------
# 允許中文、英數、底線、橫線、空白;禁止路徑字元跟 Windows 保留字
_FNAME_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)],
                 *[f"LPT{i}" for i in range(1, 10)]}


def sanitize_exam_name(name: str) -> str:
    name = name.strip()
    name = _FNAME_BAD.sub("", name)
    name = re.sub(r"\.\.+", "", name)
    name = name.strip(". ")   # 結尾點/空白在 Windows 會出事
    if name.upper() in _WIN_RESERVED:
        name = f"_{name}"
    return name[:80]

# ------------------ 聲音目錄 ------------------
# 渲染時用哪支聲音 = tts_config.json 的 edge.voice。UI 改選項就更新 json。
TTS_CONFIG_PATH = Path(__file__).parent / "tts_config.json"
VOICE_SAMPLE_DIR = Path(__file__).parent / "voices" / "samples"

# (voice id, 顯示名稱, 試聽檔名)
VOICES = [
    ("zh-TW-HsiaoChenNeural", "小陳 (台女,新聞風)",   "voice_tw_hsiaochen_F.mp3"),
    ("zh-TW-HsiaoYuNeural",   "小雨 (台女,較甜)",     "voice_tw_hsiaoyu_F.mp3"),
    ("zh-CN-YunxiNeural",     "雲希 (陸男,年輕)",     "voice_cn_yunxi_M.mp3"),
    ("zh-CN-YunyangNeural",   "雲揚 (陸男,主播穩)",   "voice_cn_yunyang_M.mp3"),
    ("zh-CN-XiaoxiaoNeural",  "曉曉 (陸女,大陸通用)", "voice_cn_xiaoxiao_F.mp3"),
]
VOICE_IDS = {v[0] for v in VOICES}


def read_current_voice() -> str:
    if not TTS_CONFIG_PATH.exists():
        return VOICES[0][0]
    try:
        cfg = json.loads(TTS_CONFIG_PATH.read_text(encoding="utf-8"))
        return cfg.get("edge", {}).get("voice") or VOICES[0][0]
    except Exception:
        return VOICES[0][0]


def write_current_voice(voice_id: str):
    if voice_id not in VOICE_IDS:
        return False
    cfg = {}
    if TTS_CONFIG_PATH.exists():
        cfg = json.loads(TTS_CONFIG_PATH.read_text(encoding="utf-8"))
    cfg.setdefault("edge", {})["voice"] = voice_id
    TTS_CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return True


def load_exam() -> dict:
    return json.loads(EXAM_PATH.read_text(encoding="utf-8"))


def save_exam(data: dict):
    EXAM_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def problem_status(pid: str) -> dict:
    mp4 = current_exam_dir() / f"{pid}.mp4"
    render_state = RENDER_STATUS.get(pid, "idle")
    return {
        "rendered": mp4.exists(),
        "mp4_size": mp4.stat().st_size if mp4.exists() else 0,
        "mp4_mtime": datetime.fromtimestamp(mp4.stat().st_mtime).strftime("%m/%d %H:%M") if mp4.exists() else "",
        "state": render_state,
    }


# ------------------ 模板 ------------------

BASE_CSS = """
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  * { box-sizing: border-box; }
  body {
    font-family: "Noto Sans TC", "PingFang TC", "Microsoft JhengHei", sans-serif;
    margin: 0; padding: 0; background: #f7f7f5; color: #222;
  }
  .container { max-width: 960px; margin: 0 auto; padding: 24px; }
  .container-wide { max-width: 1100px; margin: 0 auto; padding: 24px; }
  h1 { font-size: 22px; font-weight: 500; margin: 0; }
  h2 { font-size: 18px; font-weight: 500; margin: 0; }
  .muted { color: #888; font-size: 13px; }
  .tiny { font-size: 12px; color: #888; }
  .row { display: flex; align-items: center; gap: 12px; }
  .header-row { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 20px; }
  .card {
    background: white; border: 1px solid #e4e2dc; border-radius: 8px;
    padding: 14px 18px; margin-bottom: 12px;
  }
  .card:hover { border-color: #b5b3a9; }
  .btn {
    display: inline-block; padding: 7px 14px; border-radius: 6px;
    font-size: 13px; text-decoration: none; border: none; cursor: pointer;
    font-family: inherit;
  }
  .btn-primary { background: #185fa5; color: white; }
  .btn-primary:hover { background: #0c447c; }
  .btn-success { background: #0f6e56; color: white; }
  .btn-success:hover { background: #085041; }
  .btn-gray { background: #5f5e5a; color: white; }
  .btn-gray:hover { background: #444441; }
  .btn-link { background: transparent; color: #185fa5; padding: 4px 0; }
  .btn-link:hover { text-decoration: underline; }
  .tiny-btn {
    background: #f1efe8; border: 1px solid #d4d2cc; border-radius: 4px;
    font-size: 11px; cursor: pointer; padding: 2px 4px; margin-top: 4px;
  }
  .tiny-btn:hover { background: #e4e2dc; border-color: #b5b3a9; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; }

  .badge-done { background: #e1f5ee; color: #085041; }
  .badge-rendering { background: #faeeda; color: #633806; }
  .badge-draft { background: #f1efe8; color: #444441; }
  .banner {
    padding: 12px 16px; border-radius: 6px; margin-bottom: 16px;
    display: flex; justify-content: space-between; align-items: center;
  }
  .banner-success { background: #e1f5ee; border: 1px solid #9fe1cb; color: #085041; }
  .banner-warning { background: #faeeda; border: 1px solid #fac775; color: #633806; }
  .step-row {
    display: flex; align-items: flex-start; gap: 10px;
    padding: 12px; border: 1px solid #e4e2dc; border-radius: 6px;
    background: white; margin-bottom: 10px;
  }
  .step-row:hover { border-color: #85b7eb; }
  .step-num { width: 30px; padding-top: 8px; text-align: center; font-size: 12px; color: #888; font-weight: 500; }
  .step-col { flex: 1; }
  textarea {
    width: 100%; padding: 8px; border: 1px solid #d3d1c7;
    border-radius: 4px; font-size: 13px; font-family: inherit;
    resize: vertical;
  }
  textarea:focus { outline: none; border-color: #378add; }
  .mono { font-family: "SF Mono", Menlo, Consolas, monospace; background: #eaf3de; }
  .problem-box { background: #f1efe8; padding: 14px; border-radius: 6px; margin-bottom: 16px; }
  .col-labels { display: flex; gap: 10px; font-size: 12px; color: #888; padding: 0 4px 4px; }
  .col-labels > :first-child { width: 30px; }
  .col-labels > :not(:first-child) { flex: 1; }
  .footer { margin-top: 32px; padding-top: 16px; border-top: 1px solid #e4e2dc; font-size: 12px; color: #888; }
  a { color: #185fa5; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .problem-title { display: flex; align-items: center; gap: 12px; margin-bottom: 4px; }
  .problem-body { font-size: 13px; color: #555; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .actions { display: flex; gap: 8px; margin-left: 12px; }
</style>
"""

VOICE_PICKER_HTML = """
<div style="background:white;border:1px solid #e4e2dc;border-radius:8px;padding:10px 14px;margin-bottom:16px;display:flex;align-items:center;gap:12px">
  <span style="font-size:13px;color:#555">🗣 聲音</span>
  <form method="POST" action="/set_voice" style="margin:0;flex:1;display:flex;align-items:center;gap:8px">
    <select name="voice" onchange="this.form.submit()" style="padding:5px 8px;border:1px solid #d3d1c7;border-radius:4px;font-size:13px;font-family:inherit;min-width:240px">
      {% for vid, label, _ in voices %}
        <option value="{{ vid }}" {% if vid == current_voice %}selected{% endif %}>{{ label }}</option>
      {% endfor %}
    </select>
    <noscript><button type="submit" class="btn btn-gray" style="padding:4px 10px">套用</button></noscript>
  </form>
  <audio controls src="/voice_sample/{{ current_voice }}" style="height:32px"></audio>
  <span class="tiny">試聽(下次渲染才生效)</span>
</div>
"""

INDEX_HTML = BASE_CSS + """
<div class="container">
  <div class="header-row">
    <div>
      <h1>{{ data.exam_title }}</h1>
      <div class="muted" style="margin-top:4px">{{ data.problems|length }} 題 · {{ exam_path }}</div>
    </div>
    <div style="display:flex;gap:8px">
      <a href="/exams" class="btn btn-gray">📄 考卷列表</a>
      <a href="/library" class="btn btn-gray">📚 Library</a>
      <form method="POST" action="/render_all" style="margin:0">
        <button class="btn btn-success">🎬 批次渲染全部</button>
      </form>
    </div>
  </div>
""" + VOICE_PICKER_HTML + """

  {% for p in data.problems %}
  {% set st = statuses[p.id] %}
  <div class="card" style="display:flex; align-items:center; justify-content:space-between">
    <div style="flex:1; min-width:0">
      <div class="problem-title">
        <strong>{{ p.number }}</strong>
        <span class="tiny">{{ p.score }} 分 · {{ p.steps|length }} 步驟</span>
        {% if st.state == "rendering" %}
          <span class="badge badge-rendering">渲染中…</span>
        {% elif st.rendered %}
          <span class="badge badge-done">✓ 已產生 ({{ (st.mp4_size / 1024 / 1024) | round(1) }} MB · {{ st.mp4_mtime }})</span>
        {% else %}
          <span class="badge badge-draft">待渲染</span>
        {% endif %}
      </div>
      <div class="problem-body">{{ p.problem }}</div>
    </div>
    <div class="actions">
      <a href="/edit/{{ p.id }}" class="btn btn-primary">編輯</a>
      {% if st.rendered %}
      <a href="/video/{{ p.id }}" target="_blank" class="btn btn-gray">▶ 觀看</a>
      {% endif %}
    </div>
  </div>
  {% endfor %}

  <div class="footer">
    工作流程:編輯 → 存檔 → 單題渲染 / 批次渲染 → 觀看或下載
  </div>
</div>
"""

EDIT_HTML = BASE_CSS + """
<div class="container-wide">
  <div class="header-row">
    <div>
      <a href="/" class="btn-link">← 回考卷</a>
      <h1 style="margin-top:6px">{{ prob.number }} <span class="muted" style="font-size:14px">({{ prob.score }} 分)</span></h1>
    </div>
    <div style="display:flex; gap:8px">
      <button form="editForm" type="submit" name="action" value="save" class="btn btn-primary">💾 儲存</button>
      <button form="editForm" type="submit" name="action" value="save_and_render" class="btn btn-success">🎬 儲存並渲染</button>
    </div>
  </div>
""" + VOICE_PICKER_HTML + """

  {% if status.rendered %}
  <div class="banner banner-success">
    <span>✓ 已產生影片 · {{ (status.mp4_size / 1024 / 1024) | round(1) }} MB · {{ status.mp4_mtime }}</span>
    <a href="/video/{{ prob.id }}" target="_blank">開啟影片 →</a>
  </div>
  {% endif %}

  {% if status.state == "rendering" %}
  <div class="banner banner-warning">
    ⏳ 渲染中,請稍後重新整理頁面…
  </div>
  {% endif %}

  <form id="editForm" method="POST" action="/save/{{ prob.id }}">
    <div class="problem-box">
      <div class="tiny">題目原文</div>
      <textarea name="problem" rows="2" style="margin-top:6px">{{ prob.problem }}</textarea>
    </div>

    <div class="col-labels">
      <span></span>
      <span>💬 display (黑板顯示)</span>
      <span>🗣 narration (旁白口語)</span>
    </div>

    {% for step in prob.steps %}
    <div class="step-row">
      <div class="step-num">
        #{{ loop.index }}
        <button type="submit" name="action" value="render_from_{{ loop.index0 }}" class="tiny-btn" title="從此步驟開始重新渲染">🎬</button>
      </div>
      <div class="step-col">
        <textarea name="display_{{ loop.index0 }}" rows="2" class="mono">{{ step.display }}</textarea>
      </div>
      <div class="step-col">
        <textarea name="narration_{{ loop.index0 }}" rows="2">{{ step.narration }}</textarea>
      </div>
    </div>
    {% endfor %}

    <input type="hidden" name="step_count" value="{{ prob.steps|length }}"/>
  </form>

  <div class="footer">
    提示:display 是板書,簡潔即可(公式、關鍵數字)。narration 是口語旁白,自然一點、含停頓標點。
  </div>
</div>
"""

# ------------------ Routes ------------------

@app.route("/")
def index():
    if EXAM_PATH is None:
        return redirect(url_for("exams_list"))
    data = load_exam()
    statuses = {p["id"]: problem_status(p["id"]) for p in data["problems"]}
    return render_template_string(
        INDEX_HTML,
        data=data, statuses=statuses, exam_path=str(EXAM_PATH),
        voices=VOICES, current_voice=read_current_voice(),
    )


@app.route("/edit/<pid>")
def edit(pid):
    if EXAM_PATH is None: return redirect(url_for("exams_list"))
    data = load_exam()
    prob = next((p for p in data["problems"] if p["id"] == pid), None)
    if not prob:
        abort(404)
    return render_template_string(
        EDIT_HTML, prob=prob, status=problem_status(pid),
        voices=VOICES, current_voice=read_current_voice(),
    )


@app.route("/save/<pid>", methods=["POST"])
def save(pid):
    if EXAM_PATH is None: return redirect(url_for("exams_list"))
    data = load_exam()
    prob = next((p for p in data["problems"] if p["id"] == pid), None)
    if not prob:
        abort(404)
    prob["problem"] = request.form["problem"].strip()
    n = int(request.form["step_count"])
    existing_steps = prob.get("steps", [])
    new_steps = []
    for i in range(n):
        d = request.form.get(f"display_{i}", "").strip()
        nar = request.form.get(f"narration_{i}", "").strip()
        if d or nar:
            # 保留原 step 的其他欄位 (如 diagram_svg, _section, image) 避免表單送出時洗掉
            base = dict(existing_steps[i]) if i < len(existing_steps) and isinstance(existing_steps[i], dict) else {}
            base["display"] = d
            base["narration"] = nar
            new_steps.append(base)
    prob["steps"] = new_steps
    save_exam(data)

    # 根據按下的按鈕決定是否接著渲染
    action = request.form.get("action")
    if action == "save_and_render":
        return redirect(url_for("render", pid=pid))
    elif action and action.startswith("render_from_"):
        try:
            step_idx = int(action.replace("render_from_", ""))
            return redirect(url_for("render", pid=pid, step=step_idx))
        except ValueError:
            pass
    return redirect(url_for("edit", pid=pid))


@app.route("/render/<pid>", methods=["POST", "GET"])
def render(pid):
    if EXAM_PATH is None: return redirect(url_for("exams_list"))
    if RENDER_STATUS.get(pid) == "rendering":
        return redirect(url_for("edit", pid=pid))

    start_step = request.args.get("step")
    RENDER_STATUS[pid] = "rendering"

    def worker():
        try:
            with RENDER_LOCK:  # 避免 pipeline.py 的 /home/claude/work 被多個任務搶用
                cmd = [
                    sys.executable, str(Path(__file__).parent / "batch.py"),
                    str(EXAM_PATH), str(VIDEO_ROOT), "--only", pid
                ]
                if start_step is not None:
                    cmd += ["--step", start_step]
                
                subprocess.run(cmd, check=True)
            RENDER_STATUS[pid] = "done"
        except Exception as e:
            print(f"[render {pid}] 失敗:{e}")
            RENDER_STATUS[pid] = "error"

    threading.Thread(target=worker, daemon=True).start()
    return redirect(url_for("edit", pid=pid))


@app.route("/render_all", methods=["POST"])
def render_all():
    if EXAM_PATH is None: return redirect(url_for("exams_list"))
    data = load_exam()
    for p in data["problems"]:
        RENDER_STATUS[p["id"]] = "rendering"

    def worker():
        try:
            with RENDER_LOCK:
                subprocess.run(
                    [sys.executable, str(Path(__file__).parent / "batch.py"),
                     str(EXAM_PATH), str(VIDEO_ROOT)],
                    check=True,
                )
            for p in data["problems"]:
                RENDER_STATUS[p["id"]] = "done"
        except Exception as e:
            print(f"[render_all] 失敗:{e}")
            for p in data["problems"]:
                RENDER_STATUS[p["id"]] = "error"

    threading.Thread(target=worker, daemon=True).start()
    return redirect(url_for("index"))


@app.route("/video/<pid>")
def video(pid):
    return send_from_directory(current_exam_dir(), f"{pid}.mp4")


@app.route("/set_voice", methods=["POST"])
def set_voice():
    voice = request.form.get("voice", "")
    write_current_voice(voice)
    return redirect(request.referrer or url_for("index"))


@app.route("/voice_sample/<voice_id>")
def voice_sample(voice_id):
    """試聽:回傳該 voice 的預先錄好樣本 mp3"""
    entry = next((v for v in VOICES if v[0] == voice_id), None)
    if not entry:
        abort(404)
    fname = entry[2]
    if not (VOICE_SAMPLE_DIR / fname).exists():
        abort(404)
    return send_from_directory(VOICE_SAMPLE_DIR, fname)


# ------------------ Exams 管理 ------------------

def _scan_exams() -> list[dict]:
    """列出 exams/ 裡所有 exam JSON"""
    EXAMS_DIR.mkdir(exist_ok=True)
    items = []
    for p in sorted(EXAMS_DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        title = data.get("exam_title") or p.stem
        n_problems = len(data.get("problems", []))
        exam_video_dir = VIDEO_ROOT / p.stem
        n_videos = len(list(exam_video_dir.glob("*.mp4"))) if exam_video_dir.exists() else 0
        items.append({
            "stem": p.stem,
            "title": title,
            "problems": n_problems,
            "videos": n_videos,
            "mtime": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
            "is_current": EXAM_PATH is not None and p.resolve() == EXAM_PATH.resolve(),
        })
    return items


EXAMS_HTML = BASE_CSS + """
<div class="container-wide">
  <div class="header-row">
    <div>
      <h1>📄 考卷列表</h1>
      <div class="muted" style="margin-top:4px">{{ items|length }} 份 · 位置:{{ exams_dir }}</div>
    </div>
    <div style="display:flex;gap:8px">
      <a href="/library" class="btn btn-gray">📚 Library</a>
      <a href="/upload" class="btn btn-success">⬆ 上傳新 PDF</a>
    </div>
  </div>

  {% if not items %}
  <div class="card"><span class="muted">還沒有考卷,按上方「上傳新 PDF」開始,或放一份 JSON 到 <code>exams/</code>。</span></div>
  {% endif %}

  {% for e in items %}
  <div class="card" style="display:flex;align-items:center;justify-content:space-between">
    <div style="flex:1;min-width:0">
      <div class="problem-title">
        <strong>{{ e.title }}</strong>
        <span class="tiny">{{ e.stem }}.json · {{ e.problems }} 題 · {{ e.videos }} 支影片 · {{ e.mtime }}</span>
        {% if e.is_current %}<span class="badge badge-done">編輯中</span>{% endif %}
      </div>
    </div>
    <div class="actions" style="display:flex;gap:8px;align-items:center">
      <a href="/switch/{{ e.stem }}" class="btn btn-primary">進入編輯</a>
      <form method="POST" action="/delete_exam_json/{{ e.stem }}" onsubmit="return confirm('確定要刪除此考卷 JSON 嗎？(注意：不會刪除影片資料夾)')">
        <button type="submit" class="tiny-btn" style="color:#a52a2a;padding:6px 10px">🗑</button>
      </form>
    </div>
  </div>
  {% endfor %}
</div>
"""


UPLOAD_HTML = BASE_CSS + """
<div class="container-wide">
  <div class="header-row">
    <div>
      <a href="/exams" class="btn-link">← 回考卷列表</a>
      <h1 style="margin-top:6px">⬆ 上傳考卷 PDF</h1>
      <div class="muted" style="margin-top:4px">PDF 上傳後會丟給 Gemini Vision 解析,產出 exam.json</div>
    </div>
  </div>

  {% if error %}
  <div class="banner banner-warning">⚠ {{ error }}</div>
  {% endif %}

  <div class="card">
    <form method="POST" action="/upload" enctype="multipart/form-data">
      <div style="margin-bottom:14px">
        <label class="muted" style="display:block;margin-bottom:4px">PDF 檔</label>
        <input type="file" name="pdf" accept="application/pdf" required
               style="padding:6px;border:1px solid #d3d1c7;border-radius:4px;width:100%">
      </div>
      <div style="margin-bottom:14px">
        <label class="muted" style="display:block;margin-bottom:4px">考卷名稱 (存檔名,支援中文;空白=用 PDF 檔名)</label>
        <input type="text" name="exam_name" maxlength="80" placeholder="例:114-02 靜力學期中"
               style="padding:6px 8px;border:1px solid #d3d1c7;border-radius:4px;width:100%;font-family:inherit">
      </div>
      <div style="margin-bottom:14px">
        <label style="display:flex;align-items:center;gap:6px;font-size:13px;color:#555">
          <input type="checkbox" name="mock" value="1">
          Mock 模式 — 不呼叫 Gemini,只產範例 JSON(測試用,省 API 費用)
        </label>
      </div>
      <button type="submit" class="btn btn-success">上傳並解析</button>
    </form>
  </div>

  <div class="footer">
    提示:正式解析約 30~60 秒(Gemini Vision 處理);Mock 模式幾秒就好。
  </div>
</div>
"""


SOLVE_PROGRESS_HTML = BASE_CSS + """
<div class="container-wide">
  <h1>🧠 Gemini 解析中…</h1>
  <div class="card">
    <div style="font-size:14px">考卷:<strong>{{ stem }}</strong></div>
    <div id="state" class="muted" style="margin-top:6px">狀態:<span id="s">solving</span></div>
    <div id="msg" class="tiny" style="margin-top:4px;color:#633806"></div>
  </div>
  <div class="muted" style="margin-top:8px">約 30~60 秒(Mock 模式快很多),完成會自動跳轉。</div>
  <script>
    const stem = {{ stem|tojson }};
    async function poll() {
      try {
        const r = await fetch('/solve_status/' + encodeURIComponent(stem));
        const j = await r.json();
        document.getElementById('s').textContent = j.state;
        if (j.msg) document.getElementById('msg').textContent = j.msg;
        if (j.state === 'done') { window.location = '/switch/' + encodeURIComponent(stem); return; }
        if (j.state === 'error') return;
      } catch (e) {}
      setTimeout(poll, 2000);
    }
    poll();
  </script>
</div>
"""


@app.route("/exams")
def exams_list():
    return render_template_string(
        EXAMS_HTML, items=_scan_exams(), exams_dir=str(EXAMS_DIR)
    )


@app.route("/switch/<stem>")
def switch_exam(stem):
    global EXAM_PATH
    target = EXAMS_DIR / f"{stem}.json"
    if not target.exists():
        abort(404)
    EXAM_PATH = target.resolve()
    current_exam_dir().mkdir(parents=True, exist_ok=True)
    RENDER_STATUS.clear()
    return redirect(url_for("index"))


@app.route("/delete_exam_json/<stem>", methods=["POST"])
def delete_exam_json(stem):
    global EXAM_PATH
    if "/" in stem or ".." in stem:
        abort(400)
    target = EXAMS_DIR / f"{stem}.json"
    if target.exists():
        if EXAM_PATH and target.resolve() == EXAM_PATH.resolve():
            EXAM_PATH = None
        target.unlink()
    return redirect(url_for("exams_list"))


@app.route("/upload", methods=["GET", "POST"])
def upload():
    if request.method == "GET":
        return render_template_string(UPLOAD_HTML, error=None)

    # POST
    f = request.files.get("pdf")
    if not f or not f.filename:
        return render_template_string(UPLOAD_HTML, error="沒有選到 PDF 檔")
    if not f.filename.lower().endswith(".pdf"):
        return render_template_string(UPLOAD_HTML, error="只接受 .pdf")

    raw_name = (request.form.get("exam_name") or "").strip()
    if not raw_name:
        raw_name = Path(f.filename).stem
    stem = sanitize_exam_name(raw_name)
    if not stem:
        return render_template_string(UPLOAD_HTML, error="考卷名稱清理後空白,改一個")

    PDFS_DIR.mkdir(exist_ok=True)
    EXAMS_DIR.mkdir(exist_ok=True)

    pdf_path = PDFS_DIR / f"{stem}.pdf"
    json_path = EXAMS_DIR / f"{stem}.json"
    if json_path.exists():
        return render_template_string(
            UPLOAD_HTML,
            error=f"'{stem}.json' 已存在,請改名或先從考卷列表刪掉舊的",
        )

    f.save(str(pdf_path))
    use_mock = request.form.get("mock") == "1"
    SOLVE_STATUS[stem] = {"state": "solving", "msg": "啟動 solve.py…"}

    def worker():
        cmd = [sys.executable, str(SOLVE_SCRIPT), str(pdf_path), str(json_path)]
        if use_mock:
            cmd.append("--mock")
        try:
            r = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=900)
            if r.returncode == 0 and json_path.exists():
                SOLVE_STATUS[stem] = {"state": "done", "msg": "完成"}
            else:
                tail = (r.stderr or r.stdout or "")[-300:]
                SOLVE_STATUS[stem] = {"state": "error", "msg": tail.strip() or "solve.py 失敗"}
        except Exception as e:
            SOLVE_STATUS[stem] = {"state": "error", "msg": str(e)}

    threading.Thread(target=worker, daemon=True).start()
    return render_template_string(SOLVE_PROGRESS_HTML, stem=stem)


@app.route("/solve_status/<stem>")
def solve_status(stem):
    return jsonify(SOLVE_STATUS.get(stem, {"state": "unknown", "msg": ""}))


# ------------------ Library (跨考卷影片瀏覽) ------------------

def _scan_library() -> list[dict]:
    """掃 VIDEO_ROOT 底下所有子資料夾,回傳每個考卷的影片清單"""
    exams = []
    if not VIDEO_ROOT.exists():
        return exams
    for sub in sorted(VIDEO_ROOT.iterdir()):
        if not sub.is_dir():
            continue
        mp4s = sorted(sub.glob("*.mp4"))
        if not mp4s:
            continue
        items = []
        total = 0
        for m in mp4s:
            size = m.stat().st_size
            total += size
            items.append({
                "name": m.name,
                "stem": m.stem,
                "size_mb": round(size / 1024 / 1024, 1),
                "mtime": datetime.fromtimestamp(m.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                "has_srt": (sub / f"{m.stem}.srt").exists(),
            })
        exams.append({
            "exam_stem": sub.name,
            "video_count": len(items),
            "total_mb": round(total / 1024 / 1024, 1),
            "is_current": EXAM_PATH is not None and sub.name == EXAM_PATH.stem,
            "items": items,
        })
    return exams


LIBRARY_HTML = BASE_CSS + """
<div class="container-wide">
  <div class="header-row">
    <div>
      <a href="/" class="btn-link">← 回考卷</a>
      <h1 style="margin-top:6px">📚 影片 Library</h1>
      <div class="muted" style="margin-top:4px">根目錄:{{ root }}</div>
    </div>
  </div>

  {% if not exams %}
  <div class="card"><span class="muted">還沒有任何已渲染的影片。先回去跑批次渲染。</span></div>
  {% endif %}

  {% for e in exams %}
  <div class="card">
    <div class="problem-title" style="margin-bottom:10px; display:flex; justify-content:space-between; align-items:center">
      <div>
        <strong>{{ e.exam_stem }}</strong>
        <span class="tiny">{{ e.video_count }} 支 · {{ e.total_mb }} MB</span>
        {% if e.is_current %}<span class="badge badge-done">目前編輯中</span>{% endif %}
      </div>
      <form method="POST" action="/library/delete_exam/{{ e.exam_stem }}" onsubmit="return confirm('確定要刪除「{{ e.exam_stem }}」資料夾下的所有影片嗎？')">
        <button type="submit" class="tiny-btn" style="color:#a52a2a">🗑 刪除全部</button>
      </form>
    </div>
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead>
        <tr style="background:#f1efe8;text-align:left">
          <th style="padding:6px 8px">檔名</th>
          <th style="padding:6px 8px;width:90px">大小</th>
          <th style="padding:6px 8px;width:140px">修改時間</th>
          <th style="padding:6px 8px;width:60px">SRT</th>
          <th style="padding:6px 8px;width:160px">動作</th>
        </tr>
      </thead>
      <tbody>
        {% for it in e['items'] %}
        <tr style="border-top:1px solid #eeece3">
          <td class="mono" style="padding:6px 8px">{{ it.name }}</td>
          <td style="padding:6px 8px">{{ it.size_mb }} MB</td>
          <td style="padding:6px 8px">{{ it.mtime }}</td>
          <td style="padding:6px 8px">{% if it.has_srt %}✓{% else %}—{% endif %}</td>
          <td style="padding:6px 8px">
            <a class="btn btn-gray" href="/library/file/{{ e.exam_stem }}/{{ it.name }}" target="_blank">▶ 觀看</a>
            <a class="btn btn-link" href="/library/file/{{ e.exam_stem }}/{{ it.name }}" download title="下載">⬇</a>
            <form method="POST" action="/library/delete_file/{{ e.exam_stem }}/{{ it.name }}" style="display:inline" onsubmit="return confirm('刪除 {{ it.name }}?')">
              <button type="submit" class="tiny-btn" style="color:#a52a2a;border:none;background:none;padding:0;margin:0;margin-left:8px" title="刪除">🗑</button>
            </form>
          </td>

        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% endfor %}

  <div class="footer">
    想編輯非「目前」考卷?重啟 Flask:<code>python app.py &lt;那份.json&gt;</code>
  </div>
</div>
"""


@app.route("/library")
def library():
    return render_template_string(
        LIBRARY_HTML, exams=_scan_library(), root=str(VIDEO_ROOT)
    )


@app.route("/library/file/<exam_stem>/<filename>")
def library_file(exam_stem, filename):
    """供 library 頁面播放/下載影片跟字幕。嚴格檢查路徑避免目錄穿越"""
    # 禁止 path traversal
    if "/" in exam_stem or ".." in exam_stem or "/" in filename or ".." in filename:
        abort(400)
    folder = VIDEO_ROOT / exam_stem
    if not folder.is_dir():
        abort(404)
    target = folder / filename
    if not target.exists() or target.suffix.lower() not in {".mp4", ".srt"}:
        abort(404)
    return send_from_directory(folder, filename)


@app.route("/library/delete_exam/<exam_stem>", methods=["POST"])
def library_delete_exam(exam_stem):
    if "/" in exam_stem or ".." in exam_stem:
        abort(400)
    folder = VIDEO_ROOT / exam_stem
    if folder.is_dir():
        import shutil
        shutil.rmtree(folder)
    return redirect(url_for("library"))


@app.route("/library/delete_file/<exam_stem>/<filename>", methods=["POST"])
def library_delete_file(exam_stem, filename):
    if "/" in exam_stem or ".." in exam_stem or "/" in filename or ".." in filename:
        abort(400)
    folder = VIDEO_ROOT / exam_stem
    target = folder / filename
    if target.exists() and target.suffix.lower() == ".mp4":
        target.unlink()
        # 同時刪除配套的 srt
        srt = target.with_suffix(".srt")
        if srt.exists():
            srt.unlink()
    return redirect(url_for("library"))


@app.route("/api/status")
def api_status():
    if EXAM_PATH is None:
        return jsonify({"error": "No exam selected"}), 400
    data = load_exam()
    return jsonify({p["id"]: problem_status(p["id"]) for p in data["problems"]})


# ------------------ Main ------------------

def main():
    global EXAM_PATH, VIDEO_ROOT
    ap = argparse.ArgumentParser()
    ap.add_argument("exam_json", nargs="?", default=None,
                    help="選填:指定啟動時預開的 exam.json;省略則停在考卷列表頁")
    ap.add_argument("--video-dir", default="./videos",
                    help="影片輸出根目錄 (實際輸出至 <video-dir>/<exam_stem>/)")
    ap.add_argument("--port", type=int, default=5000)
    args = ap.parse_args()

    VIDEO_ROOT = Path(args.video_dir).resolve()
    VIDEO_ROOT.mkdir(parents=True, exist_ok=True)
    EXAMS_DIR.mkdir(exist_ok=True)
    PDFS_DIR.mkdir(exist_ok=True)

    moved = migrate_root_exams()
    if moved:
        print(f"📦 遷移 {len(moved)} 份 exam JSON 到 exams/")

    # 解析啟動參數;若給的路徑不存在,嘗試當成 exams/<stem>.json
    if args.exam_json:
        cand = Path(args.exam_json)
        if not cand.exists():
            cand = EXAMS_DIR / cand.name
        if not cand.exists():
            sys.exit(f"❌ 找不到 {args.exam_json}(也不在 exams/ 裡)")
        EXAM_PATH = cand.resolve()
        current_exam_dir().mkdir(parents=True, exist_ok=True)
        print(f"📖 預開考卷: {EXAM_PATH}")
    else:
        print(f"📖 未指定考卷,將從考卷列表開始(/exams)")

    print(f"🎬 影片根目錄: {VIDEO_ROOT}")
    print(f"🌐 Web UI: http://localhost:{args.port}")
    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
