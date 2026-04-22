# 任務:整合 F5-TTS 到考卷檢討影片系統

## 背景

目前 `pipeline.py` 的 TTS 使用 edge-tts(台灣女聲 `zh-TW-HsiaoChenNeural`),本機有自然語音,沙箱回退到 espeak-ng。現在要加入第三個 provider:**F5-TTS (本地部署,複製我本人的聲音)**。

## 目標

讓 `pipeline.py` 可以透過設定切換三種 TTS 後端,並以 F5-TTS 產生**我本人聲音**的旁白。

**關鍵限制**:

- 學生看影片時聲音已經燒進 MP4,完全不需要 TTS 依賴。F5-TTS 只在開發/製作階段呼叫。
- 本機環境:Windows + RTX 4080 (12 GB VRAM) + CUDA 12.x
- 要能跟現有 edge-tts 流程並存,不破壞 v0 / v1 已驗證的功能

## 我的聲音樣本

我會錄一份 3-5 分鐘的 WAV 樣本放在 `voice_samples/dof_reference.wav`。格式:

- 單聲道、16 kHz 以上
- 自然講話口吻(不是朗讀腔)
- 配一個對應的文字稿 `voice_samples/dof_reference.txt`(F5-TTS 需要樣本的逐字稿作為 reference text)

**如果檔案還沒準備好**,先用 placeholder 路徑,加個檢查:如果檔案不存在就跳訊息提醒我,不要自動 fallback 到 edge-tts(我要明確知道用的是哪個 provider)。

## 開發分階段

### 階段 1:重構為 provider 抽象層

把現有的 TTS 邏輯抽成獨立模組 `tts_provider.py`。

**介面設計:**

```python
# tts_provider.py
from abc import ABC, abstractmethod
from pathlib import Path

class TTSProvider(ABC):
    @abstractmethod
    async def synthesize(self, text: str, out_path: Path) -> None: ...

    @property
    @abstractmethod
    def name(self) -> str: ...


class EdgeTTSProvider(TTSProvider): ...
class EspeakProvider(TTSProvider): ...  # fallback
class F5TTSProvider(TTSProvider): ...    # 新增


def get_provider(name: str | None = None) -> TTSProvider:
    """
    name 優先序:參數 > 環境變數 TTS_PROVIDER > 預設 'edge'
    合法值:'edge', 'espeak', 'f5_tts'
    """
```

**pipeline.py 的修改**:

- 原本 `gen_tts()`、`_try_edge_tts()`、`_espeak_tts()` 全部搬到 `tts_provider.py`
- `pipeline.py` 改成 `provider = get_provider()` 然後 `await provider.synthesize(...)`
- 保留 CLI 選項:`python3 pipeline.py problem.json review_v0 --tts edge|espeak|f5_tts`
- 預設值讀環境變數 `TTS_PROVIDER`,沒設則用 `edge`

**完成後要跑通的驗證**:

- `python3 pipeline.py problem.json out --tts edge` 產出跟以前一樣的結果
- `python3 pipeline.py problem.json out --tts espeak` 也能跑
- `--tts f5_tts` 在此階段可以先丟 `NotImplementedError`,下一階段再實作

### 階段 2:安裝 F5-TTS

**優先用官方路線:**

```bash
# 在新的 venv 裡裝(避免污染主環境)
python -m venv .venv-f5
.venv-f5\Scripts\activate  # Windows

# 裝 PyTorch (CUDA 12.x)
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121

# 裝 F5-TTS
pip install f5-tts
```

**如果 pip 版本有問題**,從 source 裝:

```bash
git clone https://github.com/SWivid/F5-TTS.git
cd F5-TTS
pip install -e .
```

**驗證 CUDA + 模型能跑**:

```python
import torch
assert torch.cuda.is_available(), "CUDA 沒抓到"
print(torch.cuda.get_device_name(0))  # 應該印出 RTX 4080
```

**第一次跑會自動下載模型權重(約 1.5 GB)**,下載完存在 HuggingFace cache。先用官方 demo 試聽:

```bash
f5-tts_infer-gradio
# 開 http://localhost:7860,上傳我的 dof_reference.wav + 文字稿,試合成一段中文
```

**聽起來不夠像本人時的調整方向**(依序試):

1. 換樣本 — 3-5 秒乾淨的樣本有時比 5 分鐘含雜音的樣本效果好
2. 試不同的 `nfe_step`(預設 32,可提高到 64 品質更好但慢)
3. 試 `cfg_strength`(預設 2.0,可調 1.5~3.0 觀察情感差異)
4. 必要時才考慮 fine-tuning(需要 30 分鐘以上樣本,比較麻煩)

### 階段 3:實作 F5TTSProvider

關鍵設計決策:

**(a) F5-TTS 的呼叫方式**

F5-TTS 支援 Python API 直接呼叫(不要用 subprocess,會拖慢批次處理):

```python
from f5_tts.api import F5TTS

class F5TTSProvider(TTSProvider):
    def __init__(self, ref_audio: Path, ref_text: str,
                 model: str = "F5-TTS"):
        self.ref_audio = ref_audio
        self.ref_text = ref_text
        # 延遲載入模型(避免 import 時就吃 VRAM)
        self._model = None

    def _ensure_loaded(self):
        if self._model is None:
            self._model = F5TTS(model_type="F5-TTS")

    async def synthesize(self, text: str, out_path: Path):
        self._ensure_loaded()
        # F5-TTS 的 infer() API 輸出 numpy array
        # 用 soundfile 寫成 WAV,再轉 MP3 統一格式
        ...
```

**(b) 配置管理**

在專案根目錄加 `config/tts.yaml`(或 `.env`,看哪個順):

```yaml
f5_tts:
  ref_audio: voice_samples/dof_reference.wav
  ref_text_file: voice_samples/dof_reference.txt
  model_type: F5-TTS  # 或 E2-TTS
  nfe_step: 32
  cfg_strength: 2.0
```

**不要把聲音樣本路徑寫死在程式碼裡**。未來換樣本或訓練新模型時只改 config,不動 code。

**(c) 效能考量**

- 批次渲染整份考卷時(7 題、每題 5~8 個 step),模型應該只載入一次,不要每個 step 都重新載入
- `F5TTSProvider` 實例應該在 `pipeline.py` 的整個執行期間共用
- 一個 step ~ 10-20 字旁白,4080 上推理應該在 1-2 秒內

**(d) 錯誤處理**

- 樣本檔案不存在 → 明確報錯,不要自動 fallback
- CUDA OOM → catch 後給出清楚訊息(可能併發跑了其他 GPU 工作)
- 推理失敗 → log 完整 exception,不要吞掉

### 階段 4:整合測試

跑一次完整流程驗證:

```bash
# 1. 用 mock 資料(不呼叫 Claude API)
python3 solve.py sample_exam.pdf exam.json --mock

# 2. 用 F5-TTS 渲染一題
python3 batch.py exam.json ./videos_f5 --only q1 --tts f5_tts

# 3. 播放 videos_f5/q1.mp4,確認:
#    - 聲音聽起來像我本人
#    - 時間同步正確(字幕對得上)
#    - 沒有爆音、截斷、尾巴被吃掉等問題
```

跟 edge-tts 版本做 A/B 比較:

```bash
python3 batch.py exam.json ./videos_edge --only q1 --tts edge
# 兩支影片一起聽,確認 F5-TTS 版本確實是我的聲音而且品質可接受
```

### 階段 5:更新文件

- 更新 `README.md` 加上 F5-TTS 安裝與使用章節
- 更新 `requirements.txt`(但 F5-TTS 的 torch 版本差異太大,建議單獨放 `requirements-f5.txt`)
- 在 `CLAUDE.md` 的「目前進度」章節把 v1.5 TTS 升級標記為完成

## 硬性規則(再次強調)

1. **不要自動 `git commit`**,每階段完成跟我確認後再 commit
2. **不要破壞現有 edge-tts 流程**,重構後 `--tts edge` 必須跟以前行為完全一致
3. **Windows 路徑要用 pathlib**,不要寫死正斜線
4. **F5-TTS 模型載入只做一次**,批次處理不能每題重載
5. **聲音樣本路徑絕對不要寫死在 code**,一律走 config

## 交付物清單

完成後我應該看到:

- [ ] `tts_provider.py`(抽象層 + 三個 provider)
- [ ] `pipeline.py` 改為使用抽象層,CLI 新增 `--tts` 選項
- [ ] `config/tts.yaml`(TTS 相關設定)
- [ ] `requirements-f5.txt`(F5-TTS 專用依賴)
- [ ] `voice_samples/` 目錄(放我的樣本,加到 `.gitignore` 避免上傳)
- [ ] `docs/F5-TTS-SETUP.md`(安裝踩坑筆記,給未來的我或研究生參考)
- [ ] A/B 測試影片:`videos_edge/q1.mp4` vs `videos_f5/q1.mp4`

## 優先順序

**階段 1 是重構,獨立於 F5-TTS 能不能順利安裝**,先做完 → 確認 edge-tts 回歸測試通過 → commit。然後才進入階段 2 安裝 F5-TTS。如果階段 2 卡住(CUDA 問題、模型下載問題),先回來告訴我,不要硬撐。

## 進度回報方式

每階段完成時給我:

1. 做了什麼 / 改了哪些檔案
2. 驗證結果(哪些指令跑過、輸出是什麼)
3. 下一階段預計怎麼做 / 有沒有風險點

不要大塊 output 程式碼給我讀,要看 code 我會自己開檔案。
