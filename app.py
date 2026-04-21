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
import subprocess
import threading
from pathlib import Path
from datetime import datetime

from flask import Flask, request, jsonify, render_template_string, redirect, url_for, send_from_directory, abort

app = Flask(__name__)

# 全域狀態 (啟動時設定)
EXAM_PATH: Path = None
VIDEO_DIR: Path = None
RENDER_LOCK = threading.Lock()
RENDER_STATUS: dict = {}  # {pid: "idle" | "rendering" | "done" | "error"}


def load_exam() -> dict:
    return json.loads(EXAM_PATH.read_text(encoding="utf-8"))


def save_exam(data: dict):
    EXAM_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def problem_status(pid: str) -> dict:
    mp4 = VIDEO_DIR / f"{pid}.mp4"
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

INDEX_HTML = BASE_CSS + """
<div class="container">
  <div class="header-row">
    <div>
      <h1>{{ data.exam_title }}</h1>
      <div class="muted" style="margin-top:4px">{{ data.problems|length }} 題 · {{ exam_path }}</div>
    </div>
    <form method="POST" action="/render_all" style="margin:0">
      <button class="btn btn-success">🎬 批次渲染全部</button>
    </form>
  </div>

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
      <div class="step-num">#{{ loop.index }}</div>
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
    data = load_exam()
    statuses = {p["id"]: problem_status(p["id"]) for p in data["problems"]}
    return render_template_string(
        INDEX_HTML, data=data, statuses=statuses, exam_path=str(EXAM_PATH)
    )


@app.route("/edit/<pid>")
def edit(pid):
    data = load_exam()
    prob = next((p for p in data["problems"] if p["id"] == pid), None)
    if not prob:
        abort(404)
    return render_template_string(
        EDIT_HTML, prob=prob, status=problem_status(pid)
    )


@app.route("/save/<pid>", methods=["POST"])
def save(pid):
    data = load_exam()
    prob = next((p for p in data["problems"] if p["id"] == pid), None)
    if not prob:
        abort(404)
    prob["problem"] = request.form["problem"].strip()
    n = int(request.form["step_count"])
    new_steps = []
    for i in range(n):
        d = request.form.get(f"display_{i}", "").strip()
        nar = request.form.get(f"narration_{i}", "").strip()
        if d or nar:
            new_steps.append({"display": d, "narration": nar})
    prob["steps"] = new_steps
    save_exam(data)

    # 根據按下的按鈕決定是否接著渲染
    if request.form.get("action") == "save_and_render":
        return redirect(url_for("render", pid=pid))
    return redirect(url_for("edit", pid=pid))


@app.route("/render/<pid>", methods=["POST", "GET"])
def render(pid):
    if RENDER_STATUS.get(pid) == "rendering":
        return redirect(url_for("edit", pid=pid))

    RENDER_STATUS[pid] = "rendering"

    def worker():
        try:
            with RENDER_LOCK:  # 避免 pipeline.py 的 /home/claude/work 被多個任務搶用
                subprocess.run(
                    [sys.executable, str(Path(__file__).parent / "batch.py"),
                     str(EXAM_PATH), str(VIDEO_DIR), "--only", pid],
                    check=True,
                )
            RENDER_STATUS[pid] = "done"
        except Exception as e:
            print(f"[render {pid}] 失敗:{e}")
            RENDER_STATUS[pid] = "error"

    threading.Thread(target=worker, daemon=True).start()
    return redirect(url_for("edit", pid=pid))


@app.route("/render_all", methods=["POST"])
def render_all():
    data = load_exam()
    for p in data["problems"]:
        RENDER_STATUS[p["id"]] = "rendering"

    def worker():
        try:
            with RENDER_LOCK:
                subprocess.run(
                    [sys.executable, str(Path(__file__).parent / "batch.py"),
                     str(EXAM_PATH), str(VIDEO_DIR)],
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
    return send_from_directory(VIDEO_DIR, f"{pid}.mp4")


@app.route("/api/status")
def api_status():
    data = load_exam()
    return jsonify({p["id"]: problem_status(p["id"]) for p in data["problems"]})


# ------------------ Main ------------------

def main():
    global EXAM_PATH, VIDEO_DIR
    ap = argparse.ArgumentParser()
    ap.add_argument("exam_json", help="v1 exam.json 檔案路徑")
    ap.add_argument("--video-dir", default="./videos", help="影片輸出資料夾")
    ap.add_argument("--port", type=int, default=5000)
    args = ap.parse_args()

    EXAM_PATH = Path(args.exam_json).resolve()
    VIDEO_DIR = Path(args.video_dir).resolve()
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)

    if not EXAM_PATH.exists():
        import sys
        sys.exit(f"❌ 找不到 {EXAM_PATH}")

    print(f"📖 考卷: {EXAM_PATH}")
    print(f"🎬 影片資料夾: {VIDEO_DIR}")
    print(f"🌐 Web UI: http://localhost:{args.port}")
    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
