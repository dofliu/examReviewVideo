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

# Windows 終端 cp950 不支援 emoji，強制 UTF-8
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import fitz  # pymupdf

MODEL = "gemini-3-flash-preview"  # 或 gemini-2.5-pro
MAX_TOKENS = 32768  # 放大以容納「教學版」長 narration;Gemini 3 Flash 最高支援 64K output

SYSTEM_PROMPT = """你是一位資深的工程/數學老師,擅長把考卷題目拆解成「黑板解題影片」的完整教學腳本。
你的目標不是只給答案,而是**像在教室面對面講課**,讓學生看完之後理解背後邏輯、避開常見錯誤、下次能自己解題。

請看學生提供的考卷 PDF 圖像,辨識每一題,然後為每一題產生結構化 JSON。

==== 輸出格式(必須是純 JSON,不要 markdown code block,不要任何前後文字)====

{
  "exam_title": "整份考卷標題",
  "problems": [
    {
      "id": "q1",
      "number": "第 1 題",
      "score": 20,
      "problem": "題目原文(簡潔)",
      "steps": [
        {
          "_section": "題目解讀|觀念切入|公式導入|代入計算|單位檢查|結果解讀|易錯提醒|填充作答",
          "display": "黑板文字(≤40 字,公式/等式/關鍵字)",
          "narration": "老師口語(60~180 字,含停頓)"
        }
      ]
    }
  ]
}

`_section` 必填,從上面 8 個類別選一個。這是強制老師自我分類,不能跳步。

==== ★★★ 絕對規則(違反者該題重寫)★★★ ====

【規則 1:計算題最低 step 數】
- **每個計算題 steps 數必須 ≥ 20**,低於 20 視為輸出不合格
- 填充題 / 選擇題每小題 1~2 步即可
- 寧可拆碎、寧可冗長,也不要偷懶合併

【規則 2:計算題每個 `_section` 最低出現次數】

| _section | 最低 | 責任 |
|---|---|---|
| 題目解讀 | 2 | 一條一條讀出已知、強調單位、問「要求什麼」 |
| 觀念切入 | 2 | 這題考什麼觀念?為什麼用此方法?哪章? |
| 公式導入 | 2 | 寫公式 + 講物理/幾何意義(不只列公式) |
| 代入計算 | 5 | 分段代入,**禁止一步塞完整計算** |
| 單位檢查 | 1 | 換算單位時獨立一步講為什麼 |
| 結果解讀 | 2 | 答案量級合不合理、工程意義 |
| 易錯提醒 | 3 | 具體點出學生常犯錯,可獨立或內嵌 narration 尾端 |

【規則 3:narration 字數下限】
- `題目解讀 / 觀念切入 / 公式導入 / 結果解讀 / 易錯提醒` 這五類:**每步 narration ≥ 130 字**
- `代入計算 / 單位檢查 / 填充作答`:60~120 字即可
- 長度不足視為不合格

【規則 4:易錯提醒必須具體,不能空話】
✅ 好:「很多同學會把 kN 當 N 代進去,答案差 1000 倍」
✅ 好:「公式背錯:δ = PL³/(3EI),不是 ML/(EI)」
✅ 好:「角度忘記轉弧度是期中考經典扣分點」
❌ 不合格:「要注意單位」「要小心計算」「答對別粗心」(太空泛)

==== narration 寫作要求 ====

1. **口語化**:含「同學們」「我們來看」「注意喔」「所以說」「想一下」
2. **有停頓節奏**:逗號、句號、破折號切換氣點
3. **符號用 Unicode**(σ、Δ、θ、²、³、×、÷、≤、≥、∫、√、⊥、∠),不要 LaTeX
4. **數字讀對**:500 mm² 念「500 平方毫米」,10⁶ 念「10 的 6 次方」,σ 念「sigma」,Δ 念「delta」
5. **不要重複前一步**,每步推進新資訊

==== display 寫作要求 ====

1. ≤ 40 字,公式 / 等式 / 關鍵數值
2. **漸進式**:累積顯示,不擦掉前面,每步只加新內容
3. 易錯提醒步驟的 display 可是短警語(例:"⚠ kN → N ×1000")

============================================================
★ 完整範例:一題 21 步的教學節奏(請嚴格參考這個密度)★
============================================================

題目:T(s) = 50/(s² + 12s + 100)。求 ωn、ζ、系統類型、Tp、Mp、Ts

{"_section":"題目解讀","display":"T(s) = 50 / (s² + 12s + 100)","narration":"同學們,我們來看這一題。題目給了一個閉迴路傳遞函數 T(s),分子是 50,分母是 s 平方加 12s 加 100。這是個很典型的二階系統題,幾乎每次期中考都會有。先不急著動筆,我們先把題目讀懂,想清楚它到底在問什麼。"},

{"_section":"題目解讀","display":"求:ωn, ζ, 類型, Tp, Mp, Ts","narration":"題目要求我們算六樣東西:自然頻率 ωn、阻尼比 ζ、系統類型(過阻尼、臨界、欠阻尼)、峰值時間 Tp、最大超越量 Mp、安定時間 Ts。這六個叫做二階系統的「性能指標」,是評估動態響應的核心參數,整章控制系統幾乎都圍繞著它們在轉。"},

{"_section":"觀念切入","display":"關鍵觀念:比對係數法","narration":"開始算之前,先講觀念。任何二階系統的閉迴路傳遞函數,我們都可以整理成一個「標準形式」。這個標準式裡有兩個關鍵參數:ωn 和 ζ。只要我們把題目的 T(s) 跟標準式比對係數,就能直接讀出 ωn 和 ζ,完全不需要解微分方程。這招叫「比對係數法」,是二階系統題型的萬用鑰匙。"},

{"_section":"觀念切入","display":"為什麼先求 ωn, ζ?","narration":"為什麼要先求 ωn 和 ζ?因為後面所有性能指標(Tp、Mp、Ts)都是 ωn 跟 ζ 的函數。只要這兩個先抓出來,後面就是套公式。所以記住:看到二階系統題,第一步永遠是比對係數找 ωn、ζ,第二步才是算指標。這個順序是不變的。"},

{"_section":"公式導入","display":"標準式: T(s) = ωn² / (s² + 2ζωn·s + ωn²)","narration":"標準二階系統的分母是:s 平方 + 2ζωn·s + ωn 平方。注意三個項都有意義:s 平方項代表慣性,中間項代表阻尼,最後常數項代表剛性。分子一般寫 ωn 平方(單位增益)。這個標準式一定要記熟,它是今天整題的骨架。"},

{"_section":"公式導入","display":"ωn = 自然頻率, ζ = 阻尼比","narration":"ωn 的物理意義是「如果完全沒有阻尼,系統會以多快的頻率振盪」,單位是 rad/s。ζ 是無因次的,代表能量被阻尼消耗的快慢。ζ 小於 1 是欠阻尼,會振盪;ζ 等於 1 是臨界阻尼,不振盪但最快穩定;ζ 大於 1 是過阻尼,不振盪但慢。這三種情況畫成時間響應圖長得完全不一樣。"},

{"_section":"代入計算","display":"比對: ωn² = 100","narration":"開始比對。題目分母的常數項是 100,對應標準式的 ωn 平方,所以 ωn 平方等於 100。"},

{"_section":"代入計算","display":"ωn = 10 rad/s","narration":"開根號,ωn 等於 10,單位 rad/s。很快吧,這就是比對係數法的威力。"},

{"_section":"易錯提醒","display":"⚠ ωn 單位是 rad/s 不是 Hz","narration":"這邊要提醒一個經典扣分點:ωn 的單位是弧度每秒 rad/s,不是赫茲 Hz。如果題目問頻率 f,要再用 f = ωn / (2π) 轉一下,f 才是 Hz。很多同學直接把 10 當 10 Hz 寫交卷,就是直接扣分。記住:ωn 帶 rad/s、f 才帶 Hz,兩個差一個 2π。"},

{"_section":"代入計算","display":"比對: 2ζωn = 12","narration":"接著比對 s 的一次項。題目是 12,對應標準式的 2ζωn。所以 2 乘 ζ 乘 10 等於 12。"},

{"_section":"代入計算","display":"ζ = 12 / 20 = 0.6","narration":"移項解出 ζ 等於 12 除以 20,也就是 0.6。"},

{"_section":"結果解讀","display":"ζ = 0.6 → 欠阻尼系統","narration":"ζ 等於 0.6,落在 0 跟 1 之間,所以這是欠阻尼系統。欠阻尼的時間響應會振盪,但最終會收斂到穩態。實務上 ζ 落在 0.5~0.8 是控制設計常追求的範圍,太小振盪久,太大反應鈍。0.6 是個滿健康的值,代表系統設計算合理。"},

{"_section":"公式導入","display":"Tp = π / (ωn·√(1-ζ²))","narration":"接下來算 Tp,峰值時間。物理意義是「輸入階躍後,系統第一次衝到最高點的時間」。公式是 π 除以「阻尼自然頻率 ωd」,ωd 等於 ωn 乘根號(1-ζ²)。為什麼是 π?因為衝到第一個峰是振盪週期的一半。這個公式看起來複雜,拆開看就兩塊:ωn 管快慢,根號項管阻尼修正。"},

{"_section":"代入計算","display":"√(1-ζ²) = √(1-0.36) = √0.64 = 0.8","narration":"先算根號內:1 減 ζ 平方,也就是 1 減 0.36 等於 0.64。再開根號得 0.8。這個 0.8 我們先記下來,後面 Mp 還會重複用到,算一次賺兩次。"},

{"_section":"代入計算","display":"Tp = π / (10 × 0.8) ≈ 0.393 s","narration":"代入 Tp 公式:π 除以 10 乘以 0.8,也就是 π 除以 8。按計算機大約 0.393 秒。"},

{"_section":"公式導入","display":"Mp = exp(-πζ/√(1-ζ²)) × 100%","narration":"再來是 Mp,最大超越量。物理意義是「衝過穩態值多少百分比」。公式有點嚇人,是 e 的負次方。分子是 π 乘 ζ,分母是根號(1-ζ²)。計算重點是:指數項一定是負的(代表衰減),而且 ζ 越大 Mp 越小。這很符合直覺:阻尼越強,衝越不過。"},

{"_section":"代入計算","display":"Mp = exp(-π×0.6/0.8) = exp(-0.75π)","narration":"代入 ζ = 0.6、根號項 = 0.8。指數項化簡成負的 0.75π。"},

{"_section":"代入計算","display":"Mp ≈ e^(-2.356) ≈ 0.0948 → 9.5%","narration":"計算 e 的負 2.356 次方,大約 0.0948。換成百分比是 9.5 %。所以這個系統會衝過穩態值 9.5%。"},

{"_section":"易錯提醒","display":"⚠ Mp 要加百分比","narration":"Mp 算完很多同學直接寫 0.095 交卷。雖然數值對,但**Mp 依定義就是百分比,一定要寫成 9.5% 或乘以 100%**,不然老師就是扣分。另外提醒:Mp 的公式只跟 ζ 有關,跟 ωn 無關,這是二階系統一個很漂亮的性質,考觀念題很常出。"},

{"_section":"公式導入","display":"Ts (2%準則) = 4 / (ζωn)","narration":"最後算 Ts,安定時間。定義是「系統響應進入 ±2% 誤差帶後不再離開的時間」。公式是 4 除以 ζωn。如果老師用 5% 準則,公式改成 3 除以 ζωn。這裡的 4 跟 3 是從指數衰減包絡線推出來的,不是亂定的。"},

{"_section":"代入計算","display":"Ts = 4 / (0.6×10) = 4/6 ≈ 0.667 s","narration":"代入 ζ = 0.6、ωn = 10,分母是 6,Ts 約 0.667 秒。"},

{"_section":"結果解讀","display":"Tp=0.393s, Mp=9.5%, Ts=0.667s","narration":"三個性能指標都算出來了:峰值時間 0.39 秒、超越量 9.5%、安定時間 0.67 秒。整體看:系統在 0.39 秒衝到高點,振盪大概兩個週期(1.3 秒內)就進誤差帶穩住。9.5% 超越量不算嚴重。這是個反應速度頗快、略有振盪、設計尚佳的二階系統。"},

{"_section":"易錯提醒","display":"⚠ 比對係數別把 ζ 跟 ωn 搞反","narration":"最後一個提醒:比對係數時最常見的錯誤是把 ζ 和 ωn 的位置搞反。牢記標準式:**「2ζωn 在一次項、ωn 平方在常數項」**。常數項永遠是開根號先拿 ωn,一次項再拿來解 ζ。順序不能反。還有一個很常見錯誤是算 Ts 時忘了乘 ζ,直接寫 4/ωn,這樣結果會差一個 ζ 倍,立刻錯。"}

============================================================
(以上 21 步,預估長度約 12 分鐘,符合影片目標)
============================================================

==== 輸出前必做的自我檢查 ====

輸出前請自問(違反任一條 → 重寫該題):

□ 每個計算題 steps ≥ 20 ?
□ 七種 _section 類型(題目解讀/觀念切入/公式導入/代入計算/單位檢查/結果解讀/易錯提醒)都出現,且達最低次數?
□ 題目解讀/觀念切入/公式導入/結果解讀/易錯提醒 每步 ≥ 130 字?
□ 易錯提醒 ≥ 3 處,且都具體(不是「要小心」這種空話)?
□ 代入計算是否被塞進少數幾步?應該拆 5 步以上
□ 計算結果正確、單位正確(含 SI 詞頭 k/M/G)?

==== 其他細節 ====

1. 子題 (a)(b)(c) 各自要有完整教學節奏(題目解讀 → 觀念 → 公式 → 代入 → 結果 → 易錯)
2. 填充題 / 選擇題每小題 1~2 步用 _section="填充作答",narration 60~120 字解釋答案背後觀念
3. 圖形題(自由體圖、彎矩圖、電路圖)在 display 用 ASCII 粗略示意,narration 口頭描述
4. 數字、單位絕對不可亂寫,會誤導學生
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
    """呼叫 Gemini API 進行解題"""
    from google import genai
    from google.genai import types
    
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("❌ 缺少 GEMINI_API_KEY 環境變數。")

    client = genai.Client(api_key=api_key)
    images_b64 = pdf_to_images_b64(pdf_path)
    print(f"[solve] PDF 有 {len(images_b64)} 頁,送給 Gemini Vision...")

    contents = []
    for b64 in images_b64:
        contents.append(
            types.Part.from_bytes(
                data=base64.b64decode(b64),
                mime_type="image/png"
            )
        )
    contents.append(
        "這是一份考卷。請依照系統指示,為每一題產生結構化解答 JSON。"
    )
    
    response = client.models.generate_content(
        model=MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0.2,
            max_output_tokens=MAX_TOKENS,
        )
    )

    text = response.text.strip()
    # 容錯:如果意外被包在 ```json``` 中,剝掉
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        err_path = Path(__file__).parent / "gemini_raw_output.txt"
        err_path.write_text(text, encoding="utf-8")
        sys.exit(f"Gemini JSON parse error: {e}\n   Raw output saved to {err_path}")


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
        print("[solve] 使用 Mock 模式 (不呼叫 Gemini API)")
        data = mock_output()
    else:
        data = solve_with_gemini(pdf_path)

    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    n = len(data.get("problems", []))
    print(f"✅ 已產生 {out_path} ({n} 題)")


if __name__ == "__main__":
    main()
