#!/usr/bin/env python3
"""產生材料力學測試考卷 PDF (3 題,用 fpdf2 以確保文字可提取)"""
import os
from pathlib import Path
from fpdf import FPDF

pdf = FPDF(format="A4", unit="mm")
pdf.add_page()
pdf.set_margin(20)

# 載入繁體中文 TTF
FONT = os.environ.get("CLAUDE_FONT_PATH", "C:/Windows/Fonts/msjh.ttc")
pdf.add_font("NotoTC", "", FONT)

# ===== 抬頭 =====
pdf.set_font("NotoTC", size=18)
pdf.cell(0, 12, "材料力學 — 期中考試卷", new_x="LMARGIN", new_y="NEXT")
pdf.set_font("NotoTC", size=10)
pdf.set_text_color(100, 100, 100)
pdf.cell(0, 8, "國立勤益科技大學 智慧自動化工程系    2026 春季學期",
         new_x="LMARGIN", new_y="NEXT")
pdf.ln(4)
pdf.set_text_color(0, 0, 0)


def problem(num, score, body_lines):
    pdf.set_font("NotoTC", size=13)
    pdf.cell(0, 8, f"第 {num} 題 ({score} 分)", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("NotoTC", size=11)
    for line in body_lines:
        pdf.multi_cell(0, 7, line, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)


problem(1, 20, [
    "一根長度 L = 2 m 的鋼棒,截面積 A = 500 mm²,受軸向拉力 P = 50 kN。",
    "已知鋼的楊氏模量 E = 200 GPa。求:",
    "(a) 鋼棒中的正向應力 σ",
    "(b) 鋼棒的伸長量 ΔL",
])

problem(2, 25, [
    "一根簡支樑,跨距 L = 4 m,在跨中承受集中載重 P = 10 kN。",
    "假設樑截面為矩形,b = 50 mm、h = 100 mm。求:",
    "(a) 最大彎矩 M_max",
    "(b) 最大彎曲正向應力 σ_max",
])

problem(3, 25, [
    "一根懸臂樑,長度 L = 3 m,自由端受集中力 P = 5 kN。",
    "已知 E = 200 GPa、I = 8 × 10⁶ mm⁴。",
    "求自由端的撓度 δ。",
])

out_path = Path(__file__).parent / "sample_exam.pdf"
pdf.output(str(out_path))
print(f"OK: {out_path}")
