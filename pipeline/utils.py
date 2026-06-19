"""Общие утилиты: загрузка конфига, хеш файла, logger."""

import hashlib
import logging
import os
import subprocess
import sys
from pathlib import Path

import yaml

# Гарантируем что ffmpeg и cuDNN доступны в PATH для subprocess-вызовов
_FFMPEG_DIR = r"C:\ffmpeg\bin"
_CUDNN_DIR = str(Path(sys.prefix) / "Lib" / "site-packages" / "nvidia" / "cudnn" / "bin")
_CUBLAS_DIR = str(Path(sys.prefix) / "Lib" / "site-packages" / "nvidia" / "cublas" / "bin")

for _dir in [_FFMPEG_DIR, _CUDNN_DIR, _CUBLAS_DIR]:
    if _dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _dir + os.pathsep + os.environ.get("PATH", "")


def _deep_merge(base: dict, override: dict) -> dict:
    """Return base recursively updated by override."""
    result = dict(base)
    for key, value in (override or {}).items():
        if (
            isinstance(value, dict)
            and isinstance(result.get(key), dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    local_path = Path(path).with_name("config.local.yaml")
    if local_path.exists():
        with open(local_path, "r", encoding="utf-8") as f:
            local_cfg = yaml.safe_load(f) or {}
        cfg = _deep_merge(cfg, local_cfg)
    return cfg


def file_hash(path: Path, chunk_size: int = 65536) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()[:16]


def nvenc_video_args(cfg: dict) -> list[str]:
    """Возвращает стандартный набор ffmpeg-аргументов для NVENC.

    Гарантирует browser-compatible выход:
    - pix_fmt yuv420p (h264 в браузерах НЕ играет yuv444p / yuv422p)
    - profile high (NOT High 4:4:4 Predictive)
    - movflags +faststart (moov atom в начало для стриминга)

    Без этих флагов NVENC может унаследовать pix_fmt от ffmpeg-фильтров
    (например после eq/colorchannelmixer выходит yuv444p) — браузер
    тогда показывает чёрный экран, хотя метаданные читает.
    """
    return [
        "-c:v", cfg["video"]["codec"],
        "-preset", cfg["video"]["preset"],
        "-pix_fmt", "yuv420p",
        "-profile:v", "high",
        "-movflags", "+faststart",
    ]


def has_audio_stream(path: Path) -> bool:
    """Проверяет через ffprobe есть ли в файле хотя бы одна аудиодорожка."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "a",
                "-show_entries", "stream=index",
                "-of", "csv=p=0",
                str(path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        return bool(result.stdout.strip())
    except (subprocess.SubprocessError, FileNotFoundError):
        # Если ffprobe недоступен — предполагаем что аудио есть (старое поведение)
        return True


def get_logger(name: str = "factory") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)

    # Принудительно ставим UTF-8 на stdout (Streamlit/pythonw на Windows
    # открывают stdout в cp1251 по умолчанию — emoji/✓/→ валят логгер
    # с UnicodeEncodeError и убивают процесс).
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s", "%H:%M:%S")
    )
    # На всякий случай — если reconfigure не сработал, заменяем плохие байты
    # вместо краша.
    if hasattr(handler.stream, "buffer"):
        try:
            import io
            handler.stream = io.TextIOWrapper(
                handler.stream.buffer, encoding="utf-8", errors="replace",
                line_buffering=True,
            )
        except Exception:
            pass
    logger.addHandler(handler)
    return logger
