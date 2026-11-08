"""Расшифровка звука для роликов, у которых нет субтитров и подписи.

Цепочка: yt-dlp качает только аудио → ffmpeg отдаёт 16 кГц моно → faster-whisper
считает текст на CPU. Модель грузится один раз на процесс воркера.
"""
from __future__ import annotations

import shutil
import tempfile
import threading
import wave
from pathlib import Path

from . import config

_MODEL = None
_LOCK = threading.Lock()


def _model():
    """Ленивая загрузка модели: первый вызов качает её в data/models."""
    global _MODEL
    with _LOCK:
        if _MODEL is None:
            from faster_whisper import WhisperModel

            config.ASR_MODEL_DIR.mkdir(parents=True, exist_ok=True)
            _MODEL = WhisperModel(
                config.ASR_MODEL_NAME,
                device="cpu",
                compute_type=config.ASR_COMPUTE_TYPE,
                cpu_threads=config.ASR_THREADS,
                download_root=str(config.ASR_MODEL_DIR),
            )
        return _MODEL


def _download_audio(url: str, folder: Path) -> Path:
    """Скачивает дорожку звука и конвертирует её в wav 16 кГц моно."""
    import yt_dlp

    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(folder / "audio.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 2,
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "wav"}],
        "postprocessor_args": {"extractaudio": ["-ar", "16000", "-ac", "1", "-vn"]},
    }
    if config.COOKIES_FILE.exists():
        opts["cookiefile"] = str(config.COOKIES_FILE)
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    files = sorted(folder.glob("audio.*"))
    if not files:
        raise RuntimeError("yt-dlp не отдал аудиодорожку")
    return files[0]


def _wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / float(handle.getframerate() or 1)


def _read_wav(path: Path):
    import numpy as np

    with wave.open(str(path), "rb") as handle:
        if handle.getsampwidth() != 2:
            raise RuntimeError("ожидался 16-битный wav")
        raw = handle.readframes(handle.getnframes())
        rate = handle.getframerate()
    return np.frombuffer(raw, dtype=np.int16).astype("float32") / 32768.0, rate


def transcribe_file(path: Path) -> str:
    audio, rate = _read_wav(path)
    if rate != 16000:
        raise RuntimeError(f"ожидалась частота 16 кГц, получено {rate}")
    model = _model()
    segments, _info = model.transcribe(
        audio,
        language=None,          # язык определяем сами: рилсы бывают ru и en
        beam_size=5,
        vad_filter=True,        # музыка и тишина не превращаются в галлюцинации
        condition_on_previous_text=False,
    )
    return " ".join(segment.text.strip() for segment in segments if segment.text.strip()).strip()


def transcribe(url: str) -> dict:
    """Возвращает {"text", "seconds", "error"} — без исключений наружу."""
    if not config.ASR_ENABLED:
        return {"text": "", "seconds": 0, "error": "расшифровка выключена (FAV_ASR_ENABLED=0)"}
    if not shutil.which("ffmpeg"):
        return {"text": "", "seconds": 0, "error": "в образе нет ffmpeg"}
    folder = Path(tempfile.mkdtemp(prefix="asr-", dir=str(config.ASR_TMP_DIR)))
    try:
        audio = _download_audio(url, folder)
        seconds = _wav_seconds(audio)
        if seconds > config.ASR_MAX_SECONDS:
            return {"text": "", "seconds": seconds,
                    "error": f"ролик длиннее {config.ASR_MAX_SECONDS} с — расшифровку пропустили"}
        text = transcribe_file(audio)
        if not text:
            return {"text": "", "seconds": seconds, "error": "речь в ролике не распознана"}
        return {"text": text, "seconds": seconds, "error": ""}
    except Exception as exc:  # noqa: BLE001 — расшифровка не должна ронять обработку
        return {"text": "", "seconds": 0, "error": f"расшифровка не удалась: {str(exc)[:160]}"}
    finally:
        shutil.rmtree(folder, ignore_errors=True)
