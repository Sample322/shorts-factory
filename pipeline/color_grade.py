"""Тонкая цветокоррекция для уникализации против Content ID / fingerprinting.

Применяет 5-6 малозаметных сдвигов которые человеческий глаз почти не
ловит, но они меняют пиксельный signature видео:

- saturation ±2-5%
- gamma ±0.01-0.03
- легкий warm/cool сдвиг через RGB-multiplier (±2-4 единицы на канал)
- contrast ±0.02
- (опционально) едва заметная виньетка

Цель — НЕ "сделать красиво", а сменить отпечаток. Дефолтные значения
безопасные: визуально клип выглядит почти идентично оригиналу, но
для fingerprint-алгоритмов это уже другое видео.

Каждый запуск даёт чуть разный набор параметров (через seed по job_id),
чтобы 10 клипов из одного фильма не выглядели "обработанными одинаково".
"""

from __future__ import annotations

import hashlib
import random
import subprocess
from pathlib import Path

from .utils import get_logger, nvenc_video_args

log = get_logger("color_grade")


def _seeded_params(seed_key: str) -> dict:
    """Детерминированные слабые сдвиги по seed (стабильно для одного клипа)."""
    h = hashlib.sha256(seed_key.encode("utf-8")).hexdigest()
    rng = random.Random(int(h[:16], 16))

    return {
        # eq filter — saturation/contrast/gamma/brightness
        "saturation": round(1.0 + rng.uniform(-0.04, 0.06), 3),  # 0.96-1.06
        "contrast":   round(1.0 + rng.uniform(-0.02, 0.03), 3),  # 0.98-1.03
        "gamma":      round(1.0 + rng.uniform(-0.025, 0.025), 3),
        "brightness": round(rng.uniform(-0.015, 0.015), 3),
        # RGB-multiplier — тёплый/холодный сдвиг
        # R/G/B шкала 0..1 (1 = без изменений). Сдвиги ±2-4%
        "rr": round(1.0 + rng.uniform(-0.03, 0.04), 3),
        "gg": round(1.0 + rng.uniform(-0.02, 0.02), 3),
        "bb": round(1.0 + rng.uniform(-0.04, 0.03), 3),
        # Лёгкий hue-shift в градусах ±3
        "hue_deg": round(rng.uniform(-3.0, 3.0), 2),
    }


def apply_color_grade(
    video_in: Path,
    video_out: Path,
    cfg: dict,
    seed_key: str = "default",
    add_vignette: bool = False,
) -> Path:
    """Накладывает тонкий цветокор и пере-кодирует через NVENC.

    seed_key: любая строка (job_id, clip_id) — для воспроизводимости.
    add_vignette: добавить лёгкую виньетку (опционально).
    """
    p = _seeded_params(seed_key)

    # filter graph: colorchannelmixer (RGB сдвиг) -> eq (sat/contrast/gamma)
    #              -> hue (slight hue shift) -> (optional vignette)
    fc_parts = [
        f"colorchannelmixer=rr={p['rr']}:gg={p['gg']}:bb={p['bb']}",
        f"eq=saturation={p['saturation']}:contrast={p['contrast']}:"
        f"gamma={p['gamma']}:brightness={p['brightness']}",
        f"hue=h={p['hue_deg']}",
    ]
    if add_vignette:
        # Очень слабая виньетка ~5-7% затемнение по углам
        fc_parts.append("vignette=PI/6")

    vf = ",".join(fc_parts)

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(video_in),
        "-vf", vf,
        *nvenc_video_args(cfg),
        "-c:a", "copy",
        str(video_out),
    ]
    subprocess.run(cmd, check=True)
    log.info(
        f"  Цветокор: sat={p['saturation']}, gamma={p['gamma']}, "
        f"RGB=({p['rr']},{p['gg']},{p['bb']}), hue={p['hue_deg']}°"
    )
    return video_out
