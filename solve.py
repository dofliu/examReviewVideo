#!/usr/bin/env python3
"""
solve.py — 考卷 PDF → exam.json

流程:
1. PDF 每頁轉成 PNG 圖像
2. 圖像送給 Claude (Vision) 配合結構化 prompt
3. Claude 回傳完整 exam.json:每題包含 problem、steps[{display, narration}]

需要環境變數: ANTHROPIC_API_KEY
使用: python3 solve.py <input.pdf> [output.json]
   或 python3 solve.py <input.pdf> --mock  (離線測試用)
"""
import argparse
import base64
import json
import os
import sys
from pathlib import Path

import fitz  # pymupdf

MODEL = "claude-opus-4-5"  # 可換成 claude-sonnet-4-5 降低成本
MAX_TOKENS = 8192

SYSTEM_PROMPT = """你是一位資深的工程/數學老師,專長是將考卷題目清楚地拆解成黑板板書形式的教學影片腳本。

請看學生提供的考卷 PDF 圖像,辨識出每一題的題目,然後為每一題產生結構化的解答 JSON。

輸出規範(**必須**是純 JSON,不要有任何前後文字、不要包在 markdown code block 中):

{
  "exam_title": "整份考卷標題(例如:材料力學 — 期中考)",
  "problems": [
    {
      "id": "q1",
      "number": "第 1 題",
      "score": 20,
      "problem": "題目原文(簡潔版,保留關鍵數值)",
      "steps": [
        {
          "display": "顯示在黑板上的內容(公式、等式、關鍵字,<=40 字)",
          "narration": "老師口語講解(自然語氣,含停頓標點,50~120 字)"
        }
      ]
    }
  ]
}

重要原則:
1. **display** 是寫在黑板上讓學生看的,應該是精煉的公式或關鍵字,不是完整句子
2. **narration** 是老師講課的口語旁白,要自然、像真人教學,包含「同學們」「我們來看」這種語氣
3. 每題拆成 4-8 個 step,從「題目確認」到「列出公式」到「代入計算」到「最後答案」逐步推進
4. 數學/工程符號用 Unicode(σ、Δ、²、³、×、÷),不要用 LaTeX 原始碼
5. 計算結果要寫出**正確的數值**,包含單位
6. 如果題目有 (a)(b) 子題,要分開處理成不同的 step 序列
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


def solve_with_claude(pdf_path: Path) -> dict:
    """呼叫 Claude API 進行解題"""
    from anthropic import Anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit("❌ 缺少 ANTHROPIC_API_KEY 環境變數。請執行:export ANTHROPIC_API_KEY=sk-ant-...")

    client = Anthropic(api_key=api_key)
    images_b64 = pdf_to_images_b64(pdf_path)
    print(f"[solve] PDF 有 {len(images_b64)} 頁,送給 Claude Vision...")

    content = []
    for b64 in images_b64:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": b64},
        })
    content.append({
        "type": "text",
        "text": "這是一份考卷。請依照系統指示,為每一題產生結構化解答 JSON。",
    })

    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )

    text = resp.content[0].text.strip()
    # 容錯:如果意外被包在 ```json``` 中,剝掉
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        err_path = Path(__file__).parent / "claude_raw_output.txt"
        err_path.write_text(text, encoding="utf-8")
        sys.exit(f"Claude JSON parse error: {e}\n   Raw output saved to {err_path}")


def mock_output() -> dict:
    """離線測試用:回傳預先寫好的材料力學解答,用於驗證 pipeline 下游"""
    return {
        "exam_title": "材料力學 — 期中考 (Mock)",
        "problems": [
            {
                "id": "q1", "number": "第 1 題", "score": 20,
                "problem": "鋼棒 L=2m, A=500mm², P=50kN, E=200GPa。求 σ 與 ΔL",
                "steps": [
                    {"display": "L = 2 m,  A = 500 mm²",
                     "narration": "同學們,我們來看第一題。已知鋼棒長度 2 公尺,截面積 500 平方毫米。"},
                    {"display": "P = 50 kN,  E = 200 GPa",
                     "narration": "受到 50 千牛頓的軸向拉力,楊氏模量是 200 吉帕。"},
                    {"display": "σ = P / A",
                     "narration": "(a) 小題。正向應力的定義是力除以截面積,所以 σ 等於 P 除以 A。"},
                    {"display": "σ = 50000 / 500 = 100 MPa",
                     "narration": "代入數值:50000 牛頓除以 500 平方毫米,等於 100 百萬帕斯卡。"},
                    {"display": "ΔL = PL / AE",
                     "narration": "(b) 小題。伸長量的公式是 P 乘 L 除以 A 乘 E。"},
                    {"display": "ΔL = (50000 × 2000) / (500 × 200000)\n     = 1.0 mm",
                     "narration": "代入數值,注意單位統一成毫米跟毫帕。最後得到伸長量是 1.0 毫米。"},
                ],
            },
            {
                "id": "q2", "number": "第 2 題", "score": 25,
                "problem": "簡支樑 L=4m, 跨中 P=10kN, b=50mm, h=100mm。求 M_max 與 σ_max",
                "steps": [
                    {"display": "M_max = PL / 4",
                     "narration": "簡支樑跨中受集中力,最大彎矩發生在跨中,公式是 P 乘 L 除以 4。"},
                    {"display": "M_max = 10000 × 4 / 4 = 10000 N·m",
                     "narration": "代入數值,最大彎矩等於 10000 牛頓米,也就是 10 千牛頓米。"},
                    {"display": "σ_max = M·c / I",
                     "narration": "(b) 最大彎曲應力:M 乘以距中性軸最遠距離 c,再除以慣性矩 I。"},
                    {"display": "I = bh³/12 = 50 × 100³ / 12",
                     "narration": "矩形截面的慣性矩公式是 b 乘 h 三次方除以 12。"},
                    {"display": "I = 4.17 × 10⁶ mm⁴,  c = 50 mm",
                     "narration": "算出 I 大約是 4.17 乘 10 的 6 次方立方毫米,最遠距離是半個高度 50 毫米。"},
                    {"display": "σ_max = 120 MPa",
                     "narration": "代進去可以得到最大正向應力 120 百萬帕。這一題要注意單位的換算。"},
                ],
            },
            {
                "id": "q3", "number": "第 3 題", "score": 25,
                "problem": "懸臂樑 L=3m, 自由端 P=5kN, E=200GPa, I=8×10⁶ mm⁴。求自由端撓度 δ",
                "steps": [
                    {"display": "δ = PL³ / (3EI)",
                     "narration": "懸臂樑自由端受集中力,自由端撓度的公式是 P 乘 L 三次方,除以 3EI。"},
                    {"display": "P = 5 kN,  L = 3 m",
                     "narration": "代入已知數值:P 是 5 千牛,L 是 3 公尺。"},
                    {"display": "E = 200 GPa,  I = 8 × 10⁶ mm⁴",
                     "narration": "E 是 200 吉帕,慣性矩 8 乘 10 的 6 次方立方毫米。注意單位要統一。"},
                    {"display": "δ = (5000 × 3000³) / (3 × 200000 × 8×10⁶)",
                     "narration": "全部換成牛頓跟毫米後代入計算。"},
                    {"display": "δ ≈ 28.1 mm",
                     "narration": "最後得到自由端撓度大約 28.1 毫米。這個撓度相對較大,實際設計要考慮剛性要求。"},
                ],
            },
        ],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf", help="輸入考卷 PDF 路徑")
    ap.add_argument("output", nargs="?", default=None, help="輸出 JSON 路徑 (預設:同名 .json)")
    ap.add_argument("--mock", action="store_true", help="用 mock 資料測試下游 (不呼叫 API)")
    args = ap.parse_args()

    pdf_path = Path(args.pdf)
    out_path = Path(args.output) if args.output else pdf_path.with_suffix(".json")

    if args.mock:
        print("[solve] 使用 Mock 模式 (不呼叫 Claude API)")
        data = mock_output()
    else:
        data = solve_with_claude(pdf_path)

    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    n = len(data.get("problems", []))
    print(f"✅ 已產生 {out_path} ({n} 題)")


if __name__ == "__main__":
    main()
