"""Post-processing effects: speed control + watermark overlay.

Применяется как финальный шаг после mix_with_music если включено в config.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .utils import get_logger

log = get_logger("post_effects")


def apply_speed(
    video_in: Path,
    video_out: Path,
    speed: float,
    audio_bitrate: str = "192k",
) -> Path:
    """Ускоряет видео + аудио на factor (1.0=normal, 1.1=10% faster).

    speed < 1.0 замедление, > 1.0 ускорение. atempo принимает 0.5-2.0,
    для значений вне диапазона нужна цепочка — но мы держим 0.8-1.5.
    """
    if abs(speed - 1.0) < 0.01:
        # No-op — копируем
        import shutil
        shutil.copy(video_in, video_out)
        return video_out

    speed = max(0.5, min(2.0, speed))
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(video_in),
        "-filter_complex",
        f"[0:v]setpts=PTS/{speed}[v];[0:a]atempo={speed}[a]",
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-c:a", "aac", "-b:a", audio_bitrate,
        str(video_out),
    ]
    subprocess.run(cmd, check=True)
    return video_out


def apply_watermark(
    video_in: Path,
    video_out: Path,
    logo_path: Path,
    position: str = "bottom_right",
    opacity: float = 0.8,
    scale_pct: float = 8.0,
    margin: int = 30,
) -> Path:
    """Накладывает PNG логотип поверх видео.

    position: top_left, top_right, bottom_left, bottom_right, center
    opacity: 0.0-1.0
    scale_pct: размер лого как % от ширины видео
    margin: пиксели от края
    """
    if not logo_path.exists():
        log.warning(f"Watermark logo не найден: {logo_path}")
        import shutil
        shutil.copy(video_in, video_out)
        return video_out

    pos_map = {
        "top_left": f"{margin}:{margin}",
        "top_right": f"W-w-{margin}:{margin}",
        "bottom_left": f"{margin}:H-h-{margin}",
        "bottom_right": f"W-w-{margin}:H-h-{margin}",
        "center": "(W-w)/2:(H-h)/2",
    }
    pos = pos_map.get(position, pos_map["bottom_right"])

    # Scale logo + opacity + overlay
    fc = (
        f"[1:v]scale=iw*{scale_pct / 100}*main_w/iw:-1,"
        f"format=rgba,colorchannelmixer=aa={opacity}[wm];"
        f"[0:v][wm]overlay={pos}[v]"
    )
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(video_in),
        "-i", str(logo_path),
        "-filter_complex", fc,
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-c:a", "copy",
        str(video_out),
    ]
    subprocess.run(cmd, check=True)
    return video_out
