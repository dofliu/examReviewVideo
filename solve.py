#!/usr/bin/env python3
"""
solve.py — 考卷 PDF → exam.json

流程:
1. PDF 每頁轉成 PNG 圖像
2. 第一階段：辨識考卷標題與所有題目列表
3. 第二階段：逐一針對各題產生教學腳本 (避免 Token 溢出)
4. 統合結果輸出 JSON

需要環境變數: GEMINI_API_KEY
使用: python3 solve.py <input.pdf> [output.json]
   或 python3 solve.py <input.pdf> --mock  (離線測試用)
"""
import argparse
import base64
import json
import os
import re
import sys
from pathlib import Path


# LaTeX → plain text 映射 (Gemini 2.5 愛用 LaTeX, 但黑板顯示+TTS 都要純文字)
_GREEK_MAP = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
    "zeta": "ζ", "eta": "η", "theta": "θ", "iota": "ι", "kappa": "κ",
    "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π",
    "rho": "ρ", "sigma": "σ", "tau": "τ", "phi": "φ", "chi": "χ",
    "psi": "ψ", "omega": "ω",
}
_SYMBOL_MAP = {
    "times": "×", "div": "÷", "pm": "±", "mp": "∓", "cdot": "·",
    "circ": "°", "degree": "°", "deg": "°", "approx": "≈", "neq": "≠",
    "leq": "≤", "geq": "≥", "infty": "∞", "partial": "∂", "nabla": "∇",
    "rightarrow": "→", "leftarrow": "←", "Rightarrow": "⇒",
}

def strip_latex(text: str) -> str:
    """把 LLM 夾帶的 LaTeX 標記還原成黑板/TTS 可讀的純文字。"""
    if not text: return text
    # \frac{a}{b} → (a)/(b)
    text = re.sub(r'\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}', r'(\1)/(\2)', text)
    # \sqrt{a} → 根號(a)
    text = re.sub(r'\\sqrt\s*\{([^{}]*)\}', r'根號(\1)', text)
    # \text{xxx} → xxx
    text = re.sub(r'\\text\s*\{([^{}]*)\}', r'\1', text)
    # \vec{a} / \hat{a} / \bar{a} → a
    text = re.sub(r'\\(?:vec|hat|bar|tilde|dot|ddot)\s*\{([^{}]*)\}', r'\1', text)
    # 希臘字母 (大寫用首字母大寫判斷)
    def greek_sub(m):
        name = m.group(1)
        if name.lower() in _GREEK_MAP:
            ch = _GREEK_MAP[name.lower()]
            return ch.upper() if name[0].isupper() else ch
        return m.group(0)
    text = re.sub(r'\\([A-Za-z]+)', lambda m: _SYMBOL_MAP.get(m.group(1), m.group(0)), text)
    text = re.sub(r'\\([A-Za-z]+)', greek_sub, text)
    # 數學函數 \sin \cos \tan \log \ln 等 → 拿掉反斜線
    text = re.sub(r'\\(sin|cos|tan|cot|sec|csc|log|ln|exp|lim|sum|int|sinh|cosh|tanh|arcsin|arccos|arctan|min|max)\b', r'\1', text)
    # 上下標 _{xxx} → _xxx, ^{xxx} → ^xxx (保留一層括號的情況)
    text = re.sub(r'_\{([^{}]*)\}', r'_\1', text)
    text = re.sub(r'\^\{([^{}]*)\}', r'^\1', text)
    # 變數下標 F_A → FA, F_R_x → FRx, ΣF_y → ΣFy (用 lookahead 一次處理鏈式底線)
    text = re.sub(r'([A-Za-zα-ωΑ-Ω])_(?=[A-Za-z0-9])', r'\1', text)
    # $...$ / $$...$$ / \(...\) / \[...\] 外殼去掉
    text = re.sub(r'\$\$([^$]*)\$\$', r'\1', text)
    text = re.sub(r'\$([^$\n]*)\$', r'\1', text)
    text = re.sub(r'\\\((.*?)\\\)', r'\1', text)
    text = re.sub(r'\\\[(.*?)\\\]', r'\1', text)
    # 剩餘散落的 \xxx (未知命令) — 保留字母去掉反斜線
    text = re.sub(r'\\([a-zA-Z]+)', r'\1', text)
    # 殘留的 { } 若內容單純就去掉
    text = re.sub(r'\{([^{}]*)\}', r'\1', text)
    return text


def clean_json_escapes(text: str) -> str:
    """修正 LLM 產生的非法 JSON 轉義 (如 \\alpha, \\frac, \\(, \\theta 等 LaTeX)。
    JSON 合法反斜線轉義僅: \\" \\\\ \\/ \\b \\f \\n \\r \\t \\uXXXX
    規則: 只把「非已配對」的單一反斜線加倍 (用 negative lookbehind 避開 \\\\ 後接字元的情況),
    且 \\u 後需 4 位 hex 才算合法, 否則也加倍。"""
    # 第一步: \u 後若不是 4 位 hex, 把 \ 加倍
    text = re.sub(r'(?<!\\)\\u(?![0-9a-fA-F]{4})', r'\\\\u', text)
    # 第二步: \ 後若不是合法轉義字元, 加倍 (排除前面已是 \ 的情況)
    return re.sub(r'(?<!\\)\\(?!["\\/bfnrtu])', r'\\\\', text)

# Windows 終端 cp950 不支援 emoji，強制 UTF-8
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import fitz  # pymupdf

MODEL = "gemini-2.5-flash"  # 使用用戶指定的最新預覽模型
MAX_TOKENS = 32768  # Gemini 2.5 Flash 支援到 65536 output, 這裡設 32K 給複雜 3D 題充足空間
                     # (thinking tokens 也會吃同一份額度, 故拉高避免截斷)

# 第一階段：辨識題目清單 (不變)
IDENTIFY_PROMPT = """你是一位資深的助教, 負責從考卷圖像中提取題目資訊。
請辨識 PDF 圖像中的所有題目, 並回傳包含標題與題目列表的 JSON。

回傳格式:
{
  "exam_title": "考卷名稱",
  "problems": [
    {
      "id": "q1",
      "number": "第 1 題",
      "score": 20,
      "problem": "題目全文(含圖示描述)"
    }
  ]
}
務必辨識所有編號題目與子題。若 PDF 跨頁, 請整合完整。
"""

# 第二階段: 只產 steps (無 SVG, 避免 JSON escape 失敗), SVG 留給第三階段獨立呼叫
SOLVE_PROMPT_TEMPLATE = """你是一位資深的工程/數學老師, 擅長把考卷題目拆解成「黑板解題影片」的完整教學腳本。
請針對指定的題目, 產生極度詳細(≥ 20 步)的解題步驟 JSON。

==== 題目資訊 ====
題目編號: {number}
配分: {score}
題目內容: {problem}

==== 輸出格式 (必須是純 JSON 列表, 不要 Markdown, 不要前後文字) ====
[
  {{
    "_section": "題目解讀|觀念切入|公式導入|代入計算|單位檢查|結果解讀|易錯提醒",
    "display": "黑板上實際寫出的算式或數值結果 (≤60 字, 公式優先, 不是步驟標題)",
    "narration": "老師口語(每步 60~180 字, 含「同學們」「注意喔」等親切語氣)"
  }}
]

==== 強制規則 ====
1. **步數 ≥ 20**: 務必拆解得非常細膩, 每一題都要像是在講一堂十分鐘的課。
2. **語音長度**: 題目解讀/觀念切入/公式導入/結果解讀/易錯提醒 每步 narration 需 ≥ 130 字。
3. **公式讀法**: 使用中文讀法, 例如「s 加 3 分之 1」而非算式。
4. **數值完結 (★極重要)**: display 欄位若出現分數、除法、三角函數、平方根等數學表達式,
   **必須** 在同一步或下一步給出小數點後 4 位的最終數值, 不可停在未化簡形式。
   - ❌ 反例 (禁止): "cos(A) = 0.4565 / 40"
   - ✅ 正例: "cos(A) = 0.4565 / 40 = 0.0114"
   - ❌ 反例: "FA = 10 × sin(15°) / sin(135°)"
   - ✅ 正例: "FA = 10 × sin(15°) / sin(135°) = 10 × 0.2588 / 0.7071 = 3.660 kN"
   narration 中同樣要把最終數值唸出來, 不要只唸分數。
5. **display 必須是實際算式, 禁止抽象動作描述** (★極重要):
   display 不能只寫「展開平方項」「合併同類項」「代入公式」「畢氏定理」這種「我要做什麼」的標題,
   **必須**寫出該步實際的數學內容, 讓學生能直接抄下來。
   - ❌ 禁止: "展開平方項 (1)"
     ✅ 應寫: "FRx² = (F1 + F2 cos α)² = F1² + 2 F1 F2 cos α + F2² cos²α"
   - ❌ 禁止: "合併同類項"
     ✅ 應寫: "FR² = F1²(1) + F2²(cos²α + sin²α) + 2 F1 F2 cos α"
   - ❌ 禁止: "代入畢氏定理"
     ✅ 應寫: "FR = √(FRx² + FRy²) = √((F1 + F2 cosα)² + (F2 sinα)²)"
   - ❌ 禁止: "畢氏定理"
     ✅ 應寫: "FR² = FRx² + FRy²"
   - ❌ 禁止: "ΣFy = 0 開始計算"
     ✅ 應寫: "FA sin30° − FB sin15° = 0"
   原則:每個 display 看完, 學生要能直接抄下完整算式或結果。
   narration 才是解釋「為什麼這樣做」, display 是「黑板上的字」。

6. **嚴禁 LaTeX 與 Markdown 數學標記** (★極重要): display 與 narration 必須是純文字 + Unicode 符號,
   **絕對禁止**使用以下語法 (這些會被黑板畫成亂碼、TTS 讀出「錢字符」):
   - 禁用: 金錢符號包裹的數學模式 (dollar-math 外殼)
   - 禁用: 反斜線命令如 theta、alpha、frac、sqrt、cos、sin (有反斜線前綴的)
   - 禁用: 大括號上下標 (底線或脫字符後接大括號)
   - ✅ 正確寫法:
     • 變數直接寫 F1 F2 FA FB (不要用金錢符號包裹)
     • 希臘字母直接用 Unicode 字元: θ α β γ δ σ φ π (不要用反斜線前綴)
     • 角度用 30° 不要用脫字符+circ
     • 分數寫 (a+b)/(c) 或中文「a 加 b 除以 c」(不要用反斜線 frac)
     • 三角函數直接寫 sin cos tan (沒有反斜線前綴)
     • 簡單下標用 _1 _u (不用大括號)
   - ✅ 正範例 (display): "F1 與 u 軸夾 30°, F1u = F1 × cos 30° = 250 × 0.8660 = 216.5 N"
   - ❌ 負範例: 出現任何金錢符號 $、反斜線命令、或大括號包裹的上下標
"""

# 第三階段: 針對單一題目, 專門產生 SVG diagram (直接回 raw SVG, 不包 JSON 避免 escape 地獄)
SVG_PROMPT_TEMPLATE = """你是一位工程繪圖助手。請針對以下題目產生一個清晰、大而不擠的自由體圖(FBD)或幾何示意 SVG。

==== 題目 ====
{problem}

==== 規則 ====
若題目涉及力、力矩、向量、角度、幾何結構、自由體圖 → 產生 SVG
若題目是純代數/數列/機率/統計等無需視覺化 → 僅回傳這三個字元: NO_SVG

==== 🚨 SVG 排版品質要求 (極重要) 🚨 ====
先前繪圖常見問題: 向量太短擠在中心、標註文字疊到向量上、多個力從同一點出發糊在一起。
請務必遵守以下排版守則:

1. **畫布用滿**: viewBox="0 0 800 600" (比原本大一倍, 有足夠空間擺東西)
2. **原點位置明確**: 座標原點 O 用小圓 <circle cx="..." cy="..." r="5" /> 畫出, 並標 "O" 於左下
3. **向量夠長**: 每個力向量長度 ≥ 220px, 不要只畫 80~100px
4. **向量互不重疊**: 兩個向量夾角若 < 45°, 必須從原點往外畫, 但文字標註要錯開 ≥ 40px
5. **標籤位置**:
   - 標籤離向量末端 15~25px (不要黏在箭頭上)
   - 力的名稱 + 數值寫在同一標籤, 例如 "F1 = 250 N"
   - 角度標記用小弧線 (半徑 30~40px) + 文字在弧的外側 20px
6. **座標軸**: 水平軸 (x 或 u) 從左畫到右橫跨 700px; 垂直軸 (y 或 v) 從下畫到上橫跨 500px; 虛線 stroke-dasharray="6,4"
7. **配色**:
   - 結構/座標軸: stroke="#E8E6D8" (粉筆白), stroke-width="2"
   - 力向量: stroke="#FFD96B" (粉筆黃), stroke-width="4", 必須 marker-end="url(#arrow)"
   - 輔助線(虛線、分量投影): stroke="#8FB39B", stroke-width="1", stroke-dasharray="5,5"
   - 文字: fill="#E8E6D8" 或 "#FFD96B", font-size="24", font-family="sans-serif"
8. **必備 defs**:
   <defs><marker id="arrow" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#FFD96B" /></marker></defs>
9. **自檢**: 畫完前自問三次 — 標籤會不會壓到向量? 向量長度有沒有 >= 220? 原點位置清楚嗎?

==== 🚨 輸出格式 (嚴格) 🚨 ====
直接輸出 SVG 原始碼, 從 <svg 開頭到 </svg> 結尾。
不要 JSON, 不要 Markdown 標記, 不要任何說明文字, 不要前言後語。
不需要 SVG 時僅輸出: NO_SVG
"""

def pdf_to_images_b64(pdf_path: Path, dpi: int = 150) -> list[str]:
    """PDF 每頁 → base64 PNG"""
    doc = fitz.open(pdf_path)
    images = []
    for page in doc:
        pix = page.get_pixmap(dpi=dpi)
        png_bytes = pix.tobytes("png")
        images.append(base64.standard_b64encode(png_bytes).decode())
    doc.close()
    return images

def solve_with_gemini(pdf_path: Path) -> dict:
    """呼叫 Gemini API, 分兩階段處理以避免 token 截斷"""
    from google import genai
    from google.genai import types
    
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("❌ 缺少 GEMINI_API_KEY 環境變數。")

    client = genai.Client(api_key=api_key)
    images_b64 = pdf_to_images_b64(pdf_path)
    print(f"[solve] PDF 有 {len(images_b64)} 頁, 第一階段：辨識題目...")

    parts = [types.Part.from_bytes(data=base64.b64decode(b), mime_type="image/png") for b in images_b64]
    
    # Pass 1: Identity
    resp1 = client.models.generate_content(
        model=MODEL,
        contents=parts + [IDENTIFY_PROMPT],
        config=types.GenerateContentConfig(temperature=0.1)
    )
    
    raw_text1 = resp1.text.strip()
    if "```" in raw_text1:
        raw_text1 = raw_text1.split("```")[1]
        if raw_text1.startswith("json"): raw_text1 = raw_text1[4:]
    raw_text1 = clean_json_escapes(raw_text1.strip())

    try:
        exam_data = json.loads(raw_text1)
    except Exception as e:
        print(f"❌ 第一階段 JSON 解析失敗: {e}")
        # 儲存錯誤內容供調試
        Path("gemini_error_identify.txt").write_text(raw_text1, encoding="utf-8")
        sys.exit(1)

    # 清掉 Pass 1 產生的 problem/exam_title 裡混進來的 LaTeX 標記 ($F_1$ 之類)
    if "exam_title" in exam_data:
        exam_data["exam_title"] = strip_latex(exam_data["exam_title"])
    for prob in exam_data.get("problems", []):
        if "problem" in prob:
            prob["problem"] = strip_latex(prob["problem"])

    # Pass 2: Solve Each (帶 retry + raw response 保存)
    err_dir = pdf_path.parent / "_errors"
    print(f"[solve] 辨識到 {len(exam_data['problems'])} 題, 第二階段：逐題解析...")
    for prob in exam_data["problems"]:
        print(f"   -> 正在處理 {prob['number']}...")
        prompt = SOLVE_PROMPT_TEMPLATE.format(
            number=prob["number"],
            score=prob["score"],
            problem=prob["problem"]
        )

        steps = None
        last_raw = ""
        last_err = None
        last_finish = "?"
        # 兩次嘗試:
        #   第 1 次: temp=0.2, 預設 thinking (讓 Gemini 對複雜 3D 題思考)
        #   第 2 次: temp=0.3, thinking_budget=0 (關掉 thinking, 全部 token 給 output, 救 MAX_TOKENS 截斷)
        for attempt, (temp, no_thinking) in enumerate([(0.2, False), (0.3, True)], start=1):
            try:
                cfg_kwargs = {"temperature": temp, "max_output_tokens": MAX_TOKENS}
                if no_thinking:
                    # Gemini 2.5 Flash: thinking_budget=0 關閉內部思考, 全部額度給輸出文字
                    try:
                        cfg_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
                    except Exception:
                        pass  # 若 SDK 不支援就忽略
                resp2 = client.models.generate_content(
                    model=MODEL,
                    contents=parts + [prompt],
                    config=types.GenerateContentConfig(**cfg_kwargs)
                )
                raw_text2 = (resp2.text or "").strip()
                last_raw = raw_text2
                # 抓 finish_reason 看是否被截斷
                try:
                    last_finish = str(resp2.candidates[0].finish_reason) if resp2.candidates else "?"
                except Exception:
                    last_finish = "?"
                if "```" in raw_text2:
                    raw_text2 = raw_text2.split("```")[1]
                    if raw_text2.startswith("json"): raw_text2 = raw_text2[4:]
                raw_text2 = clean_json_escapes(raw_text2.strip())
                steps = json.loads(raw_text2)
                if attempt > 1:
                    print(f"   ↺ 重試成功 (temperature={temp}, thinking={'off' if no_thinking else 'on'}, finish={last_finish})")
                break
            except Exception as e:
                last_err = e
                hint = ""
                if "MAX_TOKENS" in last_finish:
                    hint = " [被截斷! 第 2 次會關 thinking 重試]" if attempt == 1 else " [二次仍截斷, 考慮拆題或縮短 narration]"
                print(f"   ⚠ 第 {attempt} 次嘗試失敗 (temperature={temp}, finish={last_finish}): {e}{hint}")

        if steps is not None:
            # 後處理: 清除 LaTeX 標記 (無論 prompt 怎麼防, Gemini 還是可能漏出 $ \theta 之類)
            for s in steps:
                if isinstance(s, dict):
                    if "display" in s: s["display"] = strip_latex(s["display"])
                    if "narration" in s: s["narration"] = strip_latex(s["narration"])
            prob["steps"] = steps
        else:
            # 兩次都失敗: 存 raw response 供人工 review
            err_dir.mkdir(parents=True, exist_ok=True)
            err_file = err_dir / f"{prob['id']}_raw.txt"
            err_file.write_text(last_raw or f"(no response) {last_err}", encoding="utf-8")
            print(f"   ❌ {prob['number']} 兩次皆失敗, raw 已存: {err_file}")
            prob["steps"] = [{
                "_section": "易錯提醒",
                "display": "解析失敗 — 請人工補正",
                "narration": f"抱歉, 這題解析時發生錯誤, 請打開編輯頁面手動補正。原始回應已存於 {err_file.name}。"
            }]

    # Pass 3: 針對每題獨立呼叫 Gemini 產 SVG (避開 JSON escape 地獄)
    print(f"\n[solve] 第三階段: 為每題產生 SVG 圖解...")
    for prob in exam_data["problems"]:
        # steps 為解析失敗佔位符就跳過
        _steps = prob.get("steps") or []
        if len(_steps) <= 1 and (not _steps or _steps[0].get("display", "").startswith("解析失敗")):
            print(f"   ⊘ {prob['number']} 因 Pass 2 失敗跳過 SVG 階段")
            continue

        svg_prompt = SVG_PROMPT_TEMPLATE.format(problem=prob["problem"])
        raw, finish_reason = "", None
        # 最多 2 次嘗試: 若首次被截斷, 以更高 temperature 和更明確指示重試
        for attempt in range(2):
            try:
                _prompt = svg_prompt if attempt == 0 else (
                    svg_prompt + "\n\n⚠ 上次你的輸出被截斷了, 請這次務必輸出 **完整** 的 SVG, "
                    "從 <svg 到 </svg> 都要寫完, 不要加註解省略。"
                )
                # SVG 產生是「畫圖任務」, 不需要深度 thinking, 直接關掉讓全部 token 給 SVG 字串輸出
                cfg_kwargs = {
                    "temperature": 0.2 if attempt == 0 else 0.5,
                    "max_output_tokens": 32768,  # 放大上限, 避免複雜 3D 題 SVG 被截斷
                }
                try:
                    cfg_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
                except Exception:
                    pass  # 舊 SDK 不支援就忽略
                resp3 = client.models.generate_content(
                    model=MODEL,
                    contents=parts + [_prompt],
                    config=types.GenerateContentConfig(**cfg_kwargs)
                )
                raw = (resp3.text or "").strip()
                try: finish_reason = str(resp3.candidates[0].finish_reason) if resp3.candidates else "?"
                except: finish_reason = "?"

                # 去 Markdown fence
                if raw.startswith("```"):
                    raw = raw.split("```", 2)[1] if raw.count("```") >= 2 else raw.lstrip("`")
                    for head in ("svg", "xml", "html"):
                        if raw.lower().startswith(head): raw = raw[len(head):]
                    raw = raw.strip()

                if raw.upper().startswith("NO_SVG"):
                    print(f"   ⊙ {prob['number']} 不需要 SVG (finish={finish_reason})")
                    break
                if "<svg" in raw and "</svg>" in raw:
                    svg_str = raw[raw.index("<svg"):raw.rindex("</svg>")+6]
                    # 驗證 prob["steps"][0] 型別 (debug: 若非 dict 則無法儲存欄位)
                    s0 = prob["steps"][0] if prob.get("steps") else None
                    if not isinstance(s0, dict):
                        print(f"   ⚠ {prob['number']} step[0] 不是 dict (is {type(s0).__name__}), 無法注入 SVG")
                        break
                    # SVG 內 <text> 標籤裡常見 F_A / F_R 等下標, 用最窄的 regex 拿掉底線
                    # (不跑 full strip_latex 因為可能誤吃 SVG 內的 CSS {} 或 attribute)
                    svg_str = re.sub(r'([A-Za-zα-ωΑ-Ω])_(?=[A-Za-z0-9])', r'\1', svg_str)
                    s0["diagram_svg"] = svg_str
                    verify_len = len(s0.get("diagram_svg", ""))
                    print(f"   ✅ {prob['number']} SVG 注入 step[0] ({len(svg_str)} 字元, finish={finish_reason}) [verify={verify_len}]")
                    break
                else:
                    print(f"   ⚠ {prob['number']} attempt {attempt+1}: 截斷 (bytes={len(raw)}, finish={finish_reason}, has<svg={('<svg' in raw)}, has</svg>={('</svg>' in raw)})")
            except Exception as e:
                print(f"   ⚠ {prob['number']} attempt {attempt+1} exception: {e}")

        # 兩次都未拿到完整 SVG: 存 raw 供人工 review
        if not prob["steps"][0].get("diagram_svg") and raw:
            err_dir.mkdir(parents=True, exist_ok=True)
            (err_dir / f"{prob['id']}_svg_raw.txt").write_text(raw, encoding="utf-8")

    # 最終驗證: 列出每題 step[0] 的欄位, 確認 diagram_svg 是否保留到寫檔前
    print(f"\n[solve] 寫檔前驗證:")
    for prob in exam_data["problems"]:
        s0 = prob.get("steps", [{}])[0] if prob.get("steps") else {}
        has_svg = "diagram_svg" in s0 and len(s0.get("diagram_svg", "")) > 100
        print(f"   {prob.get('id')} ({prob.get('number')}): step[0] keys={sorted(s0.keys())}, SVG={has_svg}")

    return exam_data

def mock_output() -> dict:
    """離線測試用"""
    return {
        "exam_title": "材料力學 — 期中考 (Mock)",
        "problems": [
            {
                "id": "q1", "number": "第 1 題", "score": 20,
                "problem": "鋼棒 L=2m, A=500mm², P=50kN, E=200GPa。求 σ 與 ΔL",
                "steps": [
                    {"_section": "題目解讀", "display": "L = 2 m,  A = 500 mm²",
                     "narration": "同學們,我們來看第一題。已知鋼棒長度 2 公尺,截面積 500 平方毫米。這是非常基礎的軸力構件問題, 主要是要考大家對應力與應變的基本定義。"},
                    {"_section": "題目解讀", "display": "P = 50 kN,  E = 200 GPa",
                     "narration": "受到 50 千牛頓的軸向拉力, 楊氏模量是 200 吉帕。注意單位, 千牛是十的三次方, 吉帕是十的九次方。"},
                    {"_section": "觀念切入", "display": "σ = P / A", "diagram_svg": "<svg viewBox='0 0 400 300' xmlns='http://www.w3.org/2000/svg'><defs><marker id='arrow' viewBox='0 0 10 10' refX='10' refY='5' markerWidth='6' markerHeight='6' orient='auto-start-reverse'><path d='M 0 0 L 10 5 L 0 10 z' fill='#FFD96B' /></marker></defs><rect x='100' y='140' width='200' height='20' fill='#E8E6D8' /><line x1='300' y1='150' x2='360' y2='150' stroke='#FFD96B' stroke-width='4' marker-end='url(#arrow)' /><text x='310' y='130' fill='#FFD96B' font-size='20'>P=50kN</text></svg>",
                     "narration": "正向應力的定義是單位面積所承受的內力。我們想像把棒子切開, 裡面的內力就是 P。"},
                    {"_section": "代入計算", "display": "σ = 50000 / 500 = 100 MPa",
                     "narration": "代入數值:50000 牛頓除以 500 平方毫米,等於 100 百萬帕斯卡。"},
                ],
            }
        ]
    }

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf", help="輸入考卷 PDF 路徑")
    ap.add_argument("output", nargs="?", default=None, help="輸出 JSON 路徑")
    ap.add_argument("--mock", action="store_true", help="用 mock 資料測試")
    args = ap.parse_args()

    pdf_path = Path(args.pdf)
    out_path = Path(args.output) if args.output else pdf_path.with_suffix(".json")

    if args.mock:
        data = mock_output()
    else:
        data = solve_with_gemini(pdf_path)

    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ 已產生 {out_path} ({len(data.get('problems', []))} 題)")

if __name__ == "__main__":
    main()
