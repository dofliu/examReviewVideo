"""TTS 後端抽象層
====================

支援兩種 backend:
- `edge`  — Microsoft Edge TTS(雲端免費,預設)
- `f5`    — F5-TTS(本機聲音複製,需 CUDA + `pip install f5-tts`)

切換靠 `tts_config.json` 的 `backend` 欄位。F5 呼叫失敗(缺套件、缺 ref、CUDA 不可用)
會自動 fallback 到 edge,不會讓整條 pipeline 卡死。

使用方式:
    backend = load_tts_backend()
    await backend.synthesize(text, Path("out.mp3"))
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path


CONFIG_PATH = Path(__file__).parent / "tts_config.json"


# ---------- 抽象介面 ----------
class TTSBackend(ABC):
    name: str = "base"

    @abstractmethod
    async def synthesize(self, text: str, out_path: Path) -> bool:
        """產生 mp3 到 out_path,成功回 True,失敗回 False(不 raise)"""


# ---------- Edge TTS (雲端) ----------
class EdgeTTS(TTSBackend):
    name = "edge"

    def __init__(self, voice: str = "zh-TW-HsiaoChenNeural", rate: str = "-5%"):
        self.voice = voice
        self.rate = rate

    async def synthesize(self, text: str, out_path: Path) -> bool:
        try:
            import edge_tts  # lazy import
            await edge_tts.Communicate(text, self.voice, rate=self.rate).save(str(out_path))
            return True
        except Exception as e:
            print(f"[edge-tts] failed: {e}")
            return False


# ---------- F5-TTS (本機聲音複製) ----------
class F5TTS(TTSBackend):
    name = "f5"

    def __init__(
        self,
        ref_audio: str,
        ref_text: str,
        model: str = "F5TTS_v1_Base",
        remove_silence: bool = True,
        speed: float = 1.0,
        lead_trim_sec: float = 0.3,
    ):
        """speed = 1.0 原速,<1 變慢,>1 變快
        lead_trim_sec = 每段輸出最前面砍掉的秒數 (F5 偶爾會洩漏 ref audio 前緣)"""
        self.ref_audio = ref_audio
        self.ref_text = ref_text
        self.model = model
        self.remove_silence = remove_silence
        self.speed = speed
        self.lead_trim_sec = lead_trim_sec
        self._api = None

    def _lazy_init(self):
        """首次使用才載入 F5-TTS 跟模型(載入約 10~20 秒,之後快取)"""
        if self._api is not None:
            return
        ref_path = Path(self.ref_audio)
        if not ref_path.exists():
            raise FileNotFoundError(f"F5 ref_audio 不存在: {self.ref_audio}")
        if not self.ref_text.strip():
            raise ValueError("F5 ref_text 不可為空,請在 tts_config.json 填逐字稿")
        # 延遲載入,避免沒安裝 f5-tts 時整個 module 掛掉
        from f5_tts.api import F5TTS as F5API
        self._api = F5API(model=self.model)

    async def synthesize(self, text: str, out_path: Path) -> bool:
        try:
            self._lazy_init()
            # F5 是同步 API,丟到 thread pool 不阻塞 asyncio
            wav_path = out_path.with_suffix(".wav")
            await asyncio.to_thread(
                self._api.infer,
                ref_file=self.ref_audio,
                ref_text=self.ref_text,
                gen_text=text,
                file_wave=str(wav_path),
                remove_silence=self.remove_silence,
                speed=self.speed,
            )
            # 下游 pipeline 吃 mp3;順便砍掉前 lead_trim_sec 秒的 ref 洩漏
            ff = ["ffmpeg", "-y", "-loglevel", "error"]
            if self.lead_trim_sec > 0:
                ff += ["-ss", f"{self.lead_trim_sec:.3f}"]
            ff += ["-i", str(wav_path), "-b:a", "128k", str(out_path)]
            subprocess.run(ff, check=True)
            wav_path.unlink(missing_ok=True)
            return True
        except Exception as e:
            print(f"[F5-TTS] failed: {e}")
            return False


# ---------- Fallback wrapper ----------
class FallbackTTS(TTSBackend):
    """主 backend 失敗時,自動改用 fallback(通常是 edge)"""

    def __init__(self, primary: TTSBackend, fallback: TTSBackend):
        self.primary = primary
        self.fallback = fallback
        self.name = f"{primary.name}+fallback({fallback.name})"
        self._primary_disabled = False

    async def synthesize(self, text: str, out_path: Path) -> bool:
        # 一旦 primary 失敗過,後續都直接走 fallback,避免每步都重試
        if not self._primary_disabled:
            if await self.primary.synthesize(text, out_path):
                return True
            print(f"[tts] primary '{self.primary.name}' 失敗,後續改用 '{self.fallback.name}'")
            self._primary_disabled = True
        return await self.fallback.synthesize(text, out_path)


# ---------- 載入器 ----------
def load_tts_backend(config_path: Path | None = None) -> TTSBackend:
    """讀 tts_config.json,回傳已裝好 fallback 的 backend。
    沒有設定檔時走 edge 預設值。
    """
    path = config_path or CONFIG_PATH
    if path.exists():
        cfg = json.loads(path.read_text(encoding="utf-8"))
    else:
        cfg = {}

    edge_cfg = cfg.get("edge", {}) or {}
    edge = EdgeTTS(
        voice=edge_cfg.get("voice", "zh-TW-HsiaoChenNeural"),
        rate=edge_cfg.get("rate", "-5%"),
    )

    backend_name = cfg.get("backend", "edge")
    if backend_name == "f5":
        f5cfg = cfg.get("f5", {}) or {}
        primary = F5TTS(
            ref_audio=f5cfg.get("ref_audio", "./voices/teacher_ref.wav"),
            ref_text=f5cfg.get("ref_text", ""),
            model=f5cfg.get("model", "F5TTS_v1_Base"),
            remove_silence=f5cfg.get("remove_silence", True),
            speed=float(f5cfg.get("speed", 1.0)),
            lead_trim_sec=float(f5cfg.get("lead_trim_sec", 0.3)),
        )
        return FallbackTTS(primary, edge)
    return edge
