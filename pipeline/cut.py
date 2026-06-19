"""Нарезка сегментов через FFmpeg с GPU-кодированием (NVENC) и fallback на CPU."""

import subprocess
from pathlib import Path

from .utils import get_logger, has_audio_stream

log = get_logger("cut")


def cut_segment(video_path: Path, start: float, end: float, output: Path) -> Path:
    """Точная нарезка по start/end секундам.

    Использует NVENC для скорости, fallback на libx264 если NVENC недоступен.
    Точный seek (-ss ПОСЛЕ -i) для кадровой точности; это медленнее, но
    избавляет от смещения тайминга на следующих шагах (субтитры, музыка).
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    duration = end - start
    has_audio = has_audio_stream(video_path)

    base_cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(video_path),
        "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
    ]
    # -ac 2 -ar 48000: принудительный downmix в стерео.
    # Без этого 5.1 BluRay rip-ы (6 channels) ломают AAC encoder и concat
    # filter на следующих этапах ("Unsupported channel layout 6 channels").
    audio_args = (
        ["-c:a", "aac", "-b:a", "192k", "-ac", "2", "-ar", "48000"]
        if has_audio else ["-an"]
    )
    tail = ["-avoid_negative_ts", "make_zero", str(output)]

    # Сначала пробуем NVENC. Явный pix_fmt yuv420p обязателен —
    # без него browser получает yuv444p и не воспроизводит.
    nvenc_cmd = base_cmd + [
        "-c:v", "h264_nvenc", "-preset", "p4", "-cq", "20",
        "-pix_fmt", "yuv420p", "-profile:v", "high",
        "-movflags", "+faststart",
        *audio_args, *tail,
    ]
    try:
        subprocess.run(nvenc_cmd, check=True, capture_output=True)
        return output
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"").decode("utf-8", errors="ignore")
        log.warning(f"NVENC недоступен, fallback на libx264: {stderr[:200]}")

    cpu_cmd = base_cmd + [
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-profile:v", "high",
        "-movflags", "+faststart",
        *audio_args, *tail,
    ]
    subprocess.run(cpu_cmd, check=True)
    return output
