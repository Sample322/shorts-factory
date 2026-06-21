"""Эвристический скор готового клипа: how likely to perform.

Простой rule-based scoring 0..100 для отображения в UI gallery.
Не replacement для real CTR, но даёт быстрый сигнал какие клипы "точно зайдут".
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .utils import get_logger

log = get_logger("clip_scorer")


def _audio_loudness_peak_db(video_path: Path) -> float:
    """Возвращает peak уровень в dB через ffmpeg ebur128 / volumedetect."""
    try:
        r = subprocess.run(
            [
                "ffmpeg", "-i", str(video_path), "-vn",
                "-af", "volumedetect", "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=30,
        )
        for line in r.stderr.splitlines():
            if "max_volume" in line:
                # "max_volume: -3.4 dB"
                parts = line.split(":")
                if len(parts) >= 2:
                    db_str = parts[1].strip().split()[0]
                    try:
                        return float(db_str)
                    except ValueError:
                        continue
    except Exception:
        pass
    return -30.0  # safe pessimistic default


def score_clip(
    clip_video: Path,
    clip_meta: dict,
) -> dict:
    """Скорим клип. Возвращает dict с полями:
    - score: 0..100
    - signals: dict with individual signals
    - verdict: "🔥 топ" / "👍 хорошо" / "🤷 средне" / "⚠️ слабо"
    """
    signals: dict = {}
    score = 50  # baseline

    duration = float(clip_meta.get("duration", 0))
    if 25 <= duration <= 45:
        score += 10
        signals["duration_optimal"] = True
    elif duration < 15 or duration > 60:
        score -= 15
        signals["duration_bad"] = True

    title = str(clip_meta.get("title", ""))
    if 20 <= len(title) <= 60:
        score += 5
        signals["title_optimal_len"] = True
    if any(c in title for c in "!?…"):
        score += 5
        signals["title_has_emotion"] = True

    hook = str(clip_meta.get("hook", ""))
    if hook and len(hook) >= 4:
        score += 8
        signals["hook_present"] = True

    music_mood = str(clip_meta.get("music_mood", ""))
    high_engagement_moods = {"hype", "epic", "dramatic", "action", "comedy"}
    if music_mood in high_engagement_moods:
        score += 5
        signals["mood_high_engagement"] = True

    # Audio peak — клип должен быть громким (peak > -6 dB)
    if clip_video.exists():
        peak_db = _audio_loudness_peak_db(clip_video)
        signals["peak_db"] = round(peak_db, 1)
        if peak_db > -6:
            score += 10
        elif peak_db > -12:
            score += 3
        else:
            score -= 5

    score = max(0, min(100, score))
    if score >= 75:
        verdict = "🔥 топ"
    elif score >= 60:
        verdict = "👍 хорошо"
    elif score >= 40:
        verdict = "🤷 средне"
    else:
        verdict = "⚠️ слабо"

    return {
        "score": score,
        "signals": signals,
        "verdict": verdict,
    }
