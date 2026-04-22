# ROADMAP

這個檔案放「方向性」的規劃 — 版本演進、功能路線圖。
短期可立刻做的小事在 [TODO.md](TODO.md),不要兩邊重複寫。

---

## v0 — POC(已完成)

在 Anthropic 沙箱裡驗證 pipeline 能跑。

- [x] `pipeline.py` 核心:JSON → MP4(H.264)+ SRT
- [x] 黑板風格渲染:深綠底、粉筆色階、累積顯示、最新步驟黃字突顯
- [x] Edge-TTS 雲端中文語音(fallback 到沙箱的 espeak-ng)
- [x] FFmpeg 音訊正規化(loudnorm + apad 結尾停頓)

## v1 — 本機完整產品(已完成)

**目標:把 POC 搬到 Windows 本機,接真的考卷跑完一份。**

### 1.0 — Web UI + 批次流程
- [x] `solve.py`:PDF → exam.json(原本 Claude → 改 Gemini Vision)
- [x] `app.py`:Flask Web UI,列題 / 逐段編輯 / 觸發渲染
- [x] `batch.py`:整份考卷批次渲染
- [x] `make_sample_pdf.py`:離線測試考卷

### 1.1 — 多平台相容
- [x] 沙箱 Linux 路徑硬編碼清乾淨(改 `pathlib.Path(__file__).parent`)
- [x] 字型路徑走環境變數
- [x] Windows / macOS / Linux 通用

### 1.2 — 教學內容深度
- [x] SYSTEM_PROMPT 大改:每題 20~30 step、分 `_section` 結構、強制易錯提醒、具體數值規則
- [x] few-shot 範例鎖密度(21-step 二階系統題)
- [x] MAX_TOKENS 拉到 32768

### 1.3 — 視覺品質
- [x] 字型 fallback:`msjh.ttc` 缺的 `≤`/`≥` 自動走 `seguisym.ttf`
- [x] 題目自動換行,`problem_font` 72pt → 56pt → 60pt
- [x] 標題縮成左上小字(22pt),題目成為主角
- [x] 底部預留 220px 給字幕
- [x] 步驟累積超過可視區時自動滾動,保留最新 N 步

### 1.4 — 語音品質
- [x] `pronunciation.json` 符號對照表(希臘字母、次方、分式、三角函數、拉氏轉換、`+`、`=`)
- [x] `normalize_for_tts` 替換時前後補空白 + 多重空白壓縮(解 `ζωn` 黏字)
- [x] 分式 `1/(s+3)` → `s+3 分之 1` 自動改寫(regex,只處理有括號)
- [x] SRT 按 `。！？` 切句,每句一個 cue

### 1.5 — 設定可切換
- [x] `tts_backend.py`:TTS 抽象層,edge / f5 / fallback
- [x] `tts_config.json`:選擇 backend、聲音、速度、F5 的 ref
- [x] `pipeline_config.json`:老師頭像 overlay
- [x] Web UI 聲音下拉選單 + 即時試聽
- [x] 右下角頭像 overlay(圓形 / 方形,可配 border)
- [x] F5-TTS 選用(本機聲音複製,首次下載 ~1 GB)

### 1.6 — 多考卷管理
- [x] 影片 per-exam subfolder(`videos/<exam_stem>/q1.mp4`)
- [x] `/library` 跨考卷影片瀏覽頁(含路徑穿越防禦)
- [x] `/exams` 考卷列表頁
- [x] `/upload` PDF 上傳 → Gemini 解析(支援 Mock)→ 自動進編輯
- [x] `/switch/<stem>` UI 切換編輯中的考卷
- [x] 啟動時自動遷移 repo root 的 exam JSON 到 `exams/`
- [x] 中文檔名支援(有 sanitize 避免路徑注入)

---

## v1.5 候選(短期,優先序待決定)

**接下來三個月內有機會做完的事。**

### Mathpix OCR 補強
- Gemini Vision 對複雜公式(積分、矩陣、多層分式)辨識偶爾出錯
- Mathpix API 專門做公式 OCR,可在 `solve.py` 裡加一段「對每頁的公式區塊」用 Mathpix 補強
- **優先度:中**(看真實考卷出錯率再決定)

### 單步驟重生成
- 目前一題卡住只能整題重跑,浪費 TTS + 渲染時間
- Web UI 在每個 step 旁邊加個「重生成此步」按鈕
- 對應後端路由:只重跑該 step 的 TTS + 重組 MP4(ffmpeg concat)
- **優先度:高**(最實用的效率提升)

### 字幕燒進影片
- 目前 SRT 外掛,依賴播放器
- `ffmpeg -vf subtitles=...` 一個 filter 就搞定,可控字型/大小/顏色
- Web UI 加 checkbox:「要輸出硬字幕版本」
- **優先度:低**(YouTube 直接上 SRT 也可以)

### 工程圖 AI 輔助
- 自由體圖、彎矩圖、方塊圖、電路圖
- 方向:Gemini 產 matplotlib / TikZ code,本地執行畫圖
- 每題的 steps 可帶 `image` 欄位動態切圖(v1.6 已支援 schema)
- **優先度:中**(風能 / 材料力學類課程需要)

### 發布工作流
- `publish.py`:批次上傳 YouTube(yt-dlp 反向、或 YouTube Data API)
- 自動標題、描述、標籤、縮圖
- 可能 Moodle 上傳也整合
- **優先度:低**(手動上傳還 OK)

---

## v2 — 聲音複製與臨場感(中期)

**目標:讓影片真的像「劉老師在講課」,而不只是好聽的 TTS。**

### TTS 聲音複製品質穩定
- v1.6 的 F5-TTS 整合基礎在,但實測幻覺嚴重
- 要重跑系統化實驗:ref 長度、ref 品質、gen_text 分段策略、speed、lead_trim
- 候選方案:XTTS v2、GPT-SoVITS、更新版 F5
- 可能需要提供「錄音腳本產生器」讓你錄高品質 ref

### 靜態頭像 overlay(已在 v1.5 完成基本版)
- [x] 右下角圓形 overlay
- 進階:頭像微晃 / 縮放動效 → 真人感更強

### 嘴型對齊(Lip-sync)
- 候選模型(本機 RTX 4080 都能跑):
  - **MuseTalk**(騰訊 2024,品質好速度快)
  - **SadTalker**(最成熟,表情偶爾怪)
  - **Wav2Lip**(嘴型最準但需動畫輸入)
  - **V-Express / EchoMimic**(2024 最新,安裝較麻煩)
- 流程:每段 narration 產 talking-head MP4 → FFmpeg overlay 到右下角 → 串進整支
- **技術風險中高,先做 v1.5 其他再來**

---

## v3+ — 平台化(長期)

### 多使用者 / 權限
- 如果要開給實驗室其他老師用,要有帳號系統
- 多個使用者的 exam / video 隔離

### 課程網站整合
- 直接推進 IAE 系的課程網站 / Moodle
- 學生端:掃題目 QR code → 跳到該題影片

### AI 批改助手
- 學生上傳答案掃描 → 跟 exam.json 的標準解法比對
- 產生個人化回饋(你哪一步錯了、建議重看哪段影片)

### 國際化
- Gemini prompt 支援英文考卷 → 英文 narration
- 其他語系看需求

---

## 技術債 / 重構候選

- `pipeline.py` 800+ 行,可拆 `render.py` / `compose.py` / `tts.py`
- `app.py` 的 template 全寫在字串裡,頁面多了會難維護;之後可能要拆 Jinja 檔
- 同名工具字串 helper 分散(sanitize / wrap / normalize 在不同檔),可統一
- 測試覆蓋為 0,核心邏輯該加 pytest

---

## 決策紀錄

幾個會影響方向的過去決定:

- **用 Gemini 不用 Claude**:Gemini 2.5 Flash 輸出 token 上限高(64K)、視覺能力夠、便宜 ~10x
- **黑板主題維持深綠**:已驗證學生接受度高,不改主題
- **TTS 不燒字幕預設**:YouTube 可以分離上傳 SRT,保持選擇性
- **UI 寫在 Flask template 字串**:POC 階段方便,不拆出去
- **不 refactor pipeline.py**:現在能跑就好,v1.5 完成再重構
