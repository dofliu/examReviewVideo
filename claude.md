# CLAUDE.md — 考卷檢討影片自動生成系統

## 專案目的

把一份期中考 PDF 丟進系統,自動產生每題的「黑板解題影片」(含旁白配音 + SRT 字幕),用於考後檢討。讓學生考完當週就有完整解題影片可看,省去我錄影/直播的時間。

## 關於我

- **劉瑞弘 (Dof)** — 國立勤益科技大學 智慧自動化工程系 副教授
- 教學科目:材料力學、自動控制、風力發電系統、C/Python 程式設計
- DOF Lab: doflab.cc
- 開發環境:Windows 為主 (必要時 WSL)

## 技術棧

- **Python 3.10+** 主開發語言
- **FFmpeg** 影片合成
- **Anthropic API / Claude Opus 4.7 Vision** PDF 讀題 + 解題
- **edge-tts** TTS,預設聲音 `zh-TW-HsiaoChenNeural`
- **Pillow** 黑板畫面渲染
- **Flask** 編輯確認的 Web UI
- **pymupdf / fpdf2** PDF 處理

## 架構

```
考卷 PDF
    │
    ▼ solve.py      (Claude Vision 讀題 + 解題)
exam.json
    │
    ▼ app.py        (Flask Web UI,逐段編輯確認 ← 人工關卡)
exam.json (edited)
    │
    ▼ batch.py      (批次呼叫 v0 pipeline)
pipeline.py
    │
    ▼
多支 MP4 + SRT
```

### 檔案角色

| 檔案 | 角色 | 輸入 | 輸出 |
|---|---|---|---|
| `pipeline.py` | v0 核心渲染 | 單題 JSON | MP4 + SRT |
| `solve.py` | PDF 讀題 + 解題 | PDF | exam.json |
| `batch.py` | 批次呼叫 pipeline | exam.json | 多支 MP4 |
| `app.py` | Web UI 編輯介面 | exam.json | 編輯 + 觸發渲染 |
| `make_sample_pdf.py` | 測試考卷產生器 | — | sample_exam.pdf |

## 硬規則 (不可妥協)

1. **AI 產出的數值不能未經人工 review 就當最終答案。** 適用於每一個 step、每一個公式、每一個數字。Web UI 的存在就是為了逐段檢查。這條是我長期以來的學術誠信原則,不接受任何折衷。
2. **不要自動 `git commit`。** 所有變更等我明確確認後再 commit。
3. **Linux-only 路徑要改乾淨。** 沙箱時期殘留的 `/home/claude` 硬編碼,全部換成 `pathlib.Path(__file__).parent` 相對定位。
4. **字型路徑不要寫死。** 用環境變數 `CLAUDE_FONT_PATH` 或設定檔,要能在 Windows / macOS / Linux 都跑。
5. **修 bug 前先跟我討論**,除非是顯而易見的 typo。

## 環境差異 — 從沙箱搬到本機要修的地方

這包程式碼是在 Anthropic 沙箱 (Ubuntu 24) 裡做的 POC,以下路徑/設定要改:

- **`pipeline.py`**
  - `FONT_PATH = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"` → 本機路徑或讀環境變數
  - `WORK_DIR = Path("/home/claude/work")` → `Path(__file__).parent / "work"`
  - `OUTPUT_DIR = Path("/home/claude")` → `Path(__file__).parent / "output"` 或參數化
- **`batch.py`**:從 `/home/claude` 搬檔的邏輯連動調整
- **`app.py`**:Flask 在 Windows 上 `host="0.0.0.0"` 可能被防火牆擋,開發時建議 `127.0.0.1`

沙箱沒有 edge-tts 的網路權限,所以 `pipeline.py` 有 espeak-ng fallback。本機只要裝好 edge-tts 會自動走自然語音,不用改 code。

## 目前進度

### ✅ v0 完成 (沙箱驗證過)

- pipeline.py:JSON → MP4 (1080p, H.264, AAC) + SRT 字幕
- 黑板風格渲染、累積式步驟顯示、最新步驟黃色粉筆突顯

### ✅ v1 完成 (Mock 資料驗證過)

- solve.py:PDF → Claude Vision → exam.json,含 `--mock` 離線測試模式
- app.py:Flask Web UI,列題 → 逐段編輯 display/narration → 觸發渲染
- batch.py:整份考卷批次渲染

### 🔄 v1 實戰驗證待做

- 用真的剛考完的期中考 PDF 跑一次 solve.py,檢驗 Claude Vision 解題品質
- 根據實戰結果修 bug、調 prompt、調渲染細節

### 📋 v1.5 候選功能 (優先序待我決定)

- Mathpix API 整合 — 對 Claude Vision 辨識錯的公式做補強
- 單步驟重生成按鈕 — 目前只能整題重跑,效率不夠
- ElevenLabs voice cloning — 複製我本人聲音,增強課程識別度
- 工程圖輔助 — 自由體圖、彎矩圖等,AI 產 matplotlib / TikZ
- 發布工作流 — `publish.py` 批次上傳 YouTube

## JSON Schema

### v0 單題格式 (`pipeline.py` 吃這個)

```json
{
  "title": "期中考 第一題",
  "subtitle": "選填副標",
  "problem": "題目原文",
  "steps": [
    {"display": "黑板顯示內容", "narration": "老師口語旁白"}
  ]
}
```

### v1 整份考卷格式 (`solve.py` / `app.py`)

```json
{
  "exam_title": "材料力學 — 期中考",
  "problems": [
    {
      "id": "q1",
      "number": "第 1 題",
      "score": 20,
      "problem": "題目原文",
      "steps": [{"display": "...", "narration": "..."}]
    }
  ]
}
```

- `display` ≤ 40 字,精煉(公式、數值、關鍵字)
- `narration` 50~120 字,口語自然、含停頓標點

## 開發偏好 / 溝通風格

- **直接、精簡。** 不要客套開場/結尾、不要過度解釋。
- **技術討論用繁體中文**,程式碼註解也以繁中為主。
- **架構層面的決策先列選項 + trade-off**,別直接動手做一個版本丟給我。
- **Bullet point 可以用,但以實用為主**,不要為湊格式寫廢話。
- 每次交付前,先簡述「改了什麼、為什麼、有哪些可能的副作用」。

## 我熟的 / 不熟的

**熟:** Python、Windows/Linux、MCP、RAG、SCADA、風力發電、工業通訊協定 (Modbus TCP / OPC UA)、IEC 61400、學術論文寫作

**不太熟但願意學:** 前端細節、複雜 CSS 動畫、React 生態、雲端部署 (AWS/GCP)

## 相關背景 Context

- 實驗室有兩位研究生會接觸到這個 repo:Kiwi (RAG domain-focused)、Christian (RAG architecture)
- 這個工具未來可能整合進 IAE 系課程網站或我的 YouTube 頻道
- 影片輸出要考慮檔案大小控制,單題目標 < 3 MB(1 分鐘左右)
