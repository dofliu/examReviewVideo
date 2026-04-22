# TODO

短期可以立刻做、小而具體的事項。
大方向規劃看 [ROADMAP.md](ROADMAP.md),這邊放 actionable items。

規則:
- 完成的打勾,每隔一陣子把勾完的搬去 CHANGELOG 或刪掉
- 新增項目加日期當引用(方便之後追)
- 優先度標示:🔴 高 / 🟡 中 / 🟢 低

---

## 🔴 高優先

### 實戰驗證
- [ ] **跑一份真正剛考完的期中考 PDF 完整流程**
  - 上傳 → 解析 → 逐題 review → 渲染 → 聽 3 支輸出
  - 紀錄:哪些 step Gemini 寫錯、哪些發音不準、哪些版面卡到
  - 這份「真實錯誤清單」是後續優化依據

### Bug / 小修
- [ ] **確認 CLI `python app.py <不存在的檔案>` 有友善錯誤**
  - 目前 `argparse` + 手動 `sys.exit`,但剛做完 exam_json 改 optional,要確認 error path
- [ ] **`/upload` 上傳超大 PDF(> 20 MB)會不會卡 Flask?**
  - Flask 預設沒檔案大小限制,可能要加 `MAX_CONTENT_LENGTH`

### 效率
- [ ] **單步驟重生成按鈕**(ROADMAP v1.5)
  - 影響最大的效率提升,列為下一個 milestone 首選
  - 需要:pipeline 支援只做 TTS + concat 某一個 clip,不要整題重跑

---

## 🟡 中優先

### 聲音品質
- [ ] **F5-TTS 穩定化實驗**
  - 準備 3 支不同品質的 ref 音檔(自錄 / YouTube / 播客)
  - 各跑同一段 gen_text,比較幻覺程度
  - 找出 ref 品質 → 輸出穩定度的關係,寫進 README
- [ ] **錄音腳本工具**:`tools/record_ref_script.py`
  - 產生一份適合當 F5 ref 的朗讀腳本(10~12 秒、抑揚頓挫)
  - 你錄完直接放 voices/

### 內容深度
- [ ] **觀察 Gemini 輸出的 step 數實際分布**
  - 現在 prompt 要 20~30 步但看到 22~24
  - 要不要調 prompt 說「至少 25 步」?或是別管,這樣就夠了?
- [ ] **Pronunciation map 缺漏收集**
  - 跑幾份考卷後列出 F5 / Edge 念錯的字,補進 `pronunciation.json`
  - 候選未加但可能需要:`-` → `減`(注意 `-1` 是負一不是減一)、`×10⁶` 念法

### UI / UX
- [ ] **Library 頁加刪除按鈕**
  - 現在只能看、不能管理,刪舊影片要去檔案總管
  - 加個「🗑 刪除這份考卷的全部影片」,含確認對話框
- [ ] **Exam 列表加刪除 / 重新命名**
  - 同上,對 exam JSON 本身操作
- [ ] **考卷列表上傳 PDF 後的預覽**
  - Gemini 解完直接進編輯頁有點突兀
  - 或許中間插一個「這是辨識結果,check 一下」的概覽頁?

### 渲染細節
- [ ] **`display` 超長會 overflow?**
  - 步驟文字現在有換行但字型大,2 行還 OK,3 行以上可能溢出
  - 需要:動態縮字或警告
- [ ] **Subtitle 段落底色**
  - 目前底部留白給字幕但黑色背景沒對比
  - 考慮 SRT 加 `{\an2}` tag 或播放器端處理

---

## 🟢 低優先

### 新功能
- [ ] **字幕燒進影片選項**(ROADMAP v1.5)
- [ ] **Publish 工作流**:自動上傳 YouTube / Moodle
- [ ] **Email 通知**:批次渲染完成寄信給自己

### 技術債
- [ ] **`pipeline.py` 拆檔**(800+ 行)
  - 候選切法:render / tts / srt / photo overlay 各一檔
  - 優先度低,能跑就好
- [ ] **app.py 的 HTML template 拆出 `templates/` 資料夾**
  - 頁面再多就值得拆,目前 4 個頁還能忍
- [ ] **單元測試**
  - 至少 `normalize_for_tts`、`sanitize_exam_name`、`wrap_text_for_font` 這些純函式該有 pytest

### 文件
- [ ] **寫一份「操作手冊」給研究室助理**
  - Kiwi、Christian 之後接手時有 reference
  - 包含:設 API key、上傳流程、錯誤排除
- [ ] **做一個 demo 影片**
  - 自己的 YouTube 頻道開專區介紹這個系統

---

## 已知問題(未決,先記著)

- **F5-TTS 幻覺**:ref 12 秒 cutoff + ref_text 對齊是主因,用 YouTube 抽音軌的 ref 品質不穩。v2 處理。
- **Gemini 偶爾寫錯單位**:硬規則是人工 review,不是系統 bug。
- **edge-tts 停用了 `zh-TW-YunJheNeural`**:台灣男聲目前無選項,只能用大陸男聲。沒辦法。
- **Windows 終端 cp950 吃不下 emoji**:已用 `sys.stdout.reconfigure` 解決,但有新工具檔要記得加。

---

## 已完成(偶爾清一清)

搬到 ROADMAP 的 v1.x 去。這裡只保留最近 1~2 週的。

- [x] 2026-04-22 批次 / 網頁管理介面(`/exams` / `/upload` / `/switch` / `/library`)
- [x] 2026-04-22 影片 per-exam subfolder
- [x] 2026-04-22 Web UI 聲音選單 + 試聽
- [x] 2026-04-22 F5-TTS backend + fallback
- [x] 2026-04-22 老師頭像 overlay
- [x] 2026-04-22 SRT 按句切
- [x] 2026-04-22 題目 / 步驟自動換行 + 底部字幕預留 + 滾動
- [x] 2026-04-21 字型 fallback(`≤` `≥` 不再 tofu)
- [x] 2026-04-21 Pronunciation map
