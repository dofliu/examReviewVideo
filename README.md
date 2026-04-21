# 考卷檢討影片自動生成系統

把一份期中考 PDF 丟進去,自動產生每題的黑板解題影片(含旁白 + SRT 字幕)。

---

## 架構

```
考卷 PDF
    │
    ▼ solve.py  (Claude Vision 讀題 + 解題)
exam.json  ─────────────┐
    │                   │
    ▼ Web UI (app.py)   │ 逐段編輯
    │   確認            │
    ▼ batch.py          │
   pipeline.py  ←───────┘
    │
    ▼
多支 MP4 + SRT
```

---

## 安裝

### 系統需求
- Python 3.10+
- FFmpeg
- 中文 TTF 字型 (建議 Noto Sans CJK TC)

### macOS
```bash
brew install ffmpeg font-noto-sans-cjk
pip install -r requirements.txt
```

### Ubuntu / WSL
```bash
sudo apt install ffmpeg fonts-noto-cjk
pip install -r requirements.txt
```

### Windows
安裝 FFmpeg、Noto Sans CJK TC 字型後執行 `pip install -r requirements.txt`。
`pipeline.py` 裡的 `FONT_PATH` 要改成本機 Noto CJK 的絕對路徑,例如:
```python
FONT_PATH = r"C:\Windows\Fonts\NotoSansCJK-Regular.ttc"
```

### requirements.txt
```
edge-tts>=7.0
Pillow>=10.0
mutagen>=1.47
anthropic>=0.40
pdfplumber>=0.11
pymupdf>=1.24
fpdf2>=2.8
Flask>=3.0
reportlab>=4.0
```

---

## 使用流程

### 完整流程(一行指令三步走)

```bash
# 1. PDF → exam.json  (呼叫 Claude Vision)
export ANTHROPIC_API_KEY=sk-ant-xxxx
python3 solve.py my_exam.pdf

# 2. 啟動 Web UI 編輯確認
python3 app.py my_exam.json
# 瀏覽器打開 http://localhost:5000

# 3. 在 UI 裡點「儲存並渲染」或「批次渲染全部」
```

也可以跳過 Web UI 直接批次跑:
```bash
python3 batch.py my_exam.json ./videos
```

### 離線測試(不消耗 API credits)
```bash
python3 solve.py sample_exam.pdf exam.json --mock
python3 batch.py exam.json ./videos
python3 app.py exam.json
```

---

## 檔案說明

| 檔案 | 角色 | 輸入 | 輸出 |
|---|---|---|---|
| `pipeline.py` | v0 核心渲染引擎 | 單題 JSON | MP4 + SRT |
| `solve.py` | PDF 讀題 + 解題 | PDF | exam.json |
| `batch.py` | 批次呼叫 pipeline | exam.json | 多支 MP4 |
| `app.py` | Web UI 編輯介面 | exam.json | 編輯 + 觸發渲染 |
| `make_sample_pdf.py` | 測試用考卷產生器 | — | sample_exam.pdf |

---

## JSON Schema

### v0 單題格式 (pipeline.py 吃這個)
```json
{
  "title": "期中考 第一題",
  "subtitle": "一元二次方程式",
  "problem": "a² + 2a + 1 = 0, 求 a = ?",
  "steps": [
    {
      "display": "(a + 1)² = 0",
      "narration": "同學們,這是一元二次方程式的標準形式..."
    }
  ]
}
```

### v1 整份考卷格式 (solve.py → app.py)
```json
{
  "exam_title": "材料力學 — 期中考",
  "problems": [
    {
      "id": "q1",
      "number": "第 1 題",
      "score": 20,
      "problem": "鋼棒 L=2m, A=500mm²...",
      "steps": [ {...display+narration...} ]
    }
  ]
}
```

- **display** = 寫在黑板上,精煉(公式、數值、關鍵字),建議 ≤ 40 字
- **narration** = 老師講解,口語自然,含停頓標點,50~120 字

---

## 客製化

### 換聲音 (`pipeline.py`)
```python
VOICE = "zh-TW-HsiaoChenNeural"  # 台灣女聲 (預設)
# VOICE = "zh-TW-YunJheNeural"    # 台灣男聲
# VOICE = "zh-TW-HsiaoYuNeural"   # 年輕女聲
```

查詢全部聲音:`edge-tts --list-voices | grep zh-TW`

### 換講話速度
```python
RATE = "-5%"   # 預設稍慢,改 "+10%" 變快
```

### 換黑板顏色 (`pipeline.py`)
```python
BG_COLOR = (30, 58, 46)          # 深綠 (預設)
# BG_COLOR = (20, 20, 20)        # 純黑
# BG_COLOR = (35, 50, 70)        # 深藍
```

### 換 TTS 引擎
預設的 fallback 順序是:Edge TTS (需網路) → espeak-ng (離線但機械感)。
想升級到更自然的聲音(複製你自己的聲音),在 `pipeline.py` 的 `gen_tts()` 裡加入 ElevenLabs 呼叫即可。

---

## 已知限制 / 未來擴充

**v1 目前做到這裡:**
- PDF → AI 解題 → JSON ✓
- Web UI 逐段編輯 ✓
- 批次渲染整份考卷 ✓

**還沒做 (v2+):**
- Mathpix 公式 OCR (目前 Claude Vision 自己 OCR 公式,碰到積分/矩陣/分式複雜案例可能要補強)
- 單步驟重新生成按鈕 (目前只能整題重跑)
- 自由體圖、SCADA 曲線這類工程圖輔助 (需要 AI 產 matplotlib / TikZ)
- ElevenLabs voice cloning 整合
- 多使用者 / 權限管理
- 影片動畫效果 (目前是硬切,沒有淡入或打字機效果)

---

## 工作流程建議

**考完試後的實際操作節奏:**

1. 把考卷 PDF 掃描或匯出
2. `python3 solve.py exam.pdf` → 拿到 exam.json(大概 30 秒~ 1 分鐘)
3. `python3 app.py exam.json` → 瀏覽器打開
4. **人工 review**:每題打開確認,重點檢查數值、公式、符號是否正確(符合你對 AI 數值的嚴格把關原則)
5. 改完後點「批次渲染全部」,去泡杯咖啡(大概每題 1~2 分鐘)
6. 回來把 `videos/*.mp4` 上傳到 YouTube 或 Moodle,貼到班上 Line 群
