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
import sys
from pathlib import Path

# Windows 終端 cp950 不支援 emoji，強制 UTF-8
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import fitz  # pymupdf

MODEL = "gemini-3-flash-preview"  # 使用用戶指定的最新預覽模型
MAX_TOKENS = 16384  # 預覽模型通常支援更長的輸出, 這裡設為 16K 以容納極細緻腳本

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

# 第二階段：針對單一題目產生 20 步以上的教學腳本 (加強 SVG 指令)
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
    "display": "黑板文字(≤40 字)",
    "narration": "老師口語(每步 60~180 字, 含「同學們」「注意喔」等親切語氣)",
    "diagram_svg": "可選的 SVG 程式碼"
  }}
]

==== ★★★ SVG 繪圖強制指令 (針對靜力學/物理題) ★★★ ====
1. **必備性**: 只要是力學或涉及幾何結構的題目, 在前 3 步內(通常是『題目解讀』或『觀念切入』) **務必提供一個 diagram_svg** 畫出自由體圖(FBD)。
2. **規格**: viewBox="0 0 400 300", 背景透明。
3. **顏色與標記**: 
   - 使用 #E8E6D8 (粉筆白) 畫結構。
   - 使用 #FFD96B (粉筆黃) 畫力向量與重點。
   - 務必包含 <marker id="arrow"> 並在向量線段使用 marker-end="url(#arrow)"。
4. **範例內容**: 
   ```xml
   <svg viewBox="0 0 400 300" xmlns="http://www.w3.org/2000/svg">
     <defs><marker id="arrow" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#FFD96B" /></marker></defs>
     <line x1="100" y1="200" x2="300" y2="200" stroke="#E8E6D8" stroke-width="2" />
     <line x1="200" y1="200" x2="200" y2="50" stroke="#FFD96B" stroke-width="4" marker-end="url(#arrow)" />
     <text x="210" y="70" fill="#FFD96B" font-size="20">F = 100N</text>
   </svg>
   ```

==== 強制規則 ====
1. **步數 ≥ 20**: 務必拆解得非常細膩, 每一題都要像是在講一堂十分鐘的課。
2. **語音長度**: 題目解讀/觀念切入/公式導入/結果解讀/易錯提醒 每步 narration 需 ≥ 130 字。
3. **公式讀法**: 使用中文讀法, 例如「s 加 3 分之 1」而非算式。
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
    
    try:
        exam_data = json.loads(raw_text1.strip())
    except Exception as e:
        print(f"❌ 第一階段 JSON 解析失敗: {e}")
        # 儲存錯誤內容供調試
        Path("gemini_error_identify.txt").write_text(raw_text1, encoding="utf-8")
        sys.exit(1)

    # Pass 2: Solve Each
    print(f"[solve] 辨識到 {len(exam_data['problems'])} 題, 第二階段：逐題解析...")
    for prob in exam_data["problems"]:
        print(f"   -> 正在處理 {prob['number']}...")
        prompt = SOLVE_PROMPT_TEMPLATE.format(
            number=prob["number"],
            score=prob["score"],
            problem=prob["problem"]
        )
        
        resp2 = client.models.generate_content(
            model=MODEL,
            contents=parts + [prompt],
            config=types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=MAX_TOKENS
            )
        )
        
        raw_text2 = resp2.text.strip()
        if "```" in raw_text2:
            raw_text2 = raw_text2.split("```")[1]
            if raw_text2.startswith("json"): raw_text2 = raw_text2[4:]
        
        try:
            prob["steps"] = json.loads(raw_text2.strip())
        except Exception as e:
            print(f"   ❌ {prob['number']} 解析失敗: {e}")
            prob["steps"] = [{"_section": "易錯提醒", "display": "解析失敗", "narration": "抱歉, 這題解析時發生錯誤, 請手動補正。"}]

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
