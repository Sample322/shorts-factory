"""Удаление длинных пауз между фразами в видеоклипе.

Стратегия:
1. ffmpeg `silencedetect` находит интервалы тишины с порогом dB.
2. Слишком короткие тишины (естественные паузы в речи) НЕ трогаем —
   режем только те что длиннее `min_silence_sec`.
3. Между фразами оставляем небольшую "подушку" `keep_padding_sec`,
   чтобы не звучало как джамп-кат «на стыке слов».
4. Видео + аудио пере-склеиваются через concat-демультиплексор.

Безопасные дефолты:
  min_silence_sec=0.7  — режем только реально длинные паузы
  keep_padding_sec=0.15 — оставляем 150мс воздуха с каждой стороны
  noise_threshold_db=-32 — стандартный порог "тишины" для речи
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

from .utils import get_logger, has_audio_stream, nvenc_video_args

log = get_logger("silence_cut")


def _detect_silences(
    video_path: Path,
    noise_threshold_db: float = -32.0,
    min_silence_sec: float = 0.7,
) -> list[tuple[float, float]]:
    """Возвращает [(start_sec, end_sec)] всех пауз длиннее порога."""
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats",
        "-i", str(video_path),
        "-af", f"silencedetect=noise={noise_threshold_db}dB:d={min_silence_sec}",
        "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    output = proc.stderr  # silencedetect пишет в stderr

    silences: list[tuple[float, float]] = []
    current_start: float | None = None
    for line in output.splitlines():
        m_start = re.search(r"silence_start:\s*([\d.]+)", line)
        if m_start:
            current_start = float(m_start.group(1))
            continue
        m_end = re.search(
            r"silence_end:\s*([\d.]+)\s*\|\s*silence_duration:\s*([\d.]+)",
            line,
        )
        if m_end and current_start is not None:
            silences.append((current_start, float(m_end.group(1))))
            current_start = None
    return silences


def _ffprobe_duration(path: Path) -> float:
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True, text=True, timeout=15,
        )
        return float(r.stdout.strip() or 0)
    except (subprocess.SubprocessError, ValueError):
        return 0.0


def _build_keep_intervals(
    silences: list[tuple[float, float]],
    total_duration: float,
    keep_padding_sec: float = 0.15,
    min_segment_sec: float = 0.3,
) -> list[tuple[float, float]]:
    """Из списка тишин строит "что оставить" с подушкой по краям."""
    keeps: list[tuple[float, float]] = []
    cursor = 0.0
    for sil_start, sil_end in silences:
        # Конец полезного сегмента = начало тишины + подушка
        seg_end = min(sil_start + keep_padding_sec, total_duration)
        # Начало следующего полезного сегмента = конец тишины - подушка
        next_start = max(sil_end - keep_padding_sec, 0.0)

        if seg_end - cursor >= min_segment_sec:
            keeps.append((cursor, seg_end))
        cursor = next_start

    # Хвост
    if total_duration - cursor >= min_segment_sec:
        keeps.append((cursor, total_duration))
    return keeps


def remove_silences(
    video_in: Path,
    video_out: Path,
    cfg: dict,
    noise_threshold_db: float = -32.0,
    min_silence_sec: float = 0.7,
    keep_padding_sec: float = 0.15,
) -> tuple[Path, dict]:
    """Режет паузы. Возвращает (output_path, stats).

    stats = {removed_sec, original_sec, final_sec, segments_kept}
    Если резать нечего — копирует файл as-is и возвращает stats с removed_sec=0.
    """
    duration = _ffprobe_duration(video_in)
    if duration <= 0:
        log.warning(f"Не могу определить длительность {video_in.name}, пропускаю")
        if video_out != video_in:
            import shutil
            shutil.copy(video_in, video_out)
        return video_out, {"removed_sec": 0, "original_sec": 0,
                           "final_sec": 0, "segments_kept": 1}

    silences = _detect_silences(video_in, noise_threshold_db, min_silence_sec)
    if not silences:
        log.info(f"  Тишин длиннее {min_silence_sec}с не найдено")
        import shutil
        shutil.copy(video_in, video_out)
        return video_out, {"removed_sec": 0, "original_sec": duration,
                           "final_sec": duration, "segments_kept": 1}

    keeps = _build_keep_intervals(
        silences, duration, keep_padding_sec=keep_padding_sec
    )
    if not keeps or len(keeps) == 1 and keeps[0] == (0, duration):
        import shutil
        shutil.copy(video_in, video_out)
        return video_out, {"removed_sec": 0, "original_sec": duration,
                           "final_sec": duration, "segments_kept": 1}

    has_audio = has_audio_stream(video_in)
    n = len(keeps)

    # --- ПРАВИЛЬНЫЙ порядок входов для concat ---
    # FFmpeg concat ожидает inputs в порядке "сначала всё первого сегмента,
    # потом всё второго" — [v0][a0][v1][a1][v2][a2]...
    # А НЕ [v0][v1][v2][a0][a1][a2] — это вызывает Media type mismatch.
    parts: list[str] = []
    inputs_order: list[str] = []
    for i, (s, e) in enumerate(keeps):
        parts.append(
            f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS[v{i}]"
        )
        inputs_order.append(f"[v{i}]")
        if has_audio:
            # aformat — нормализуем layout в stereo чтобы concat не валился
            # на 5.1 surround source ("Unknown channel layouts not supported").
            parts.append(
                f"[0:a]atrim=start={s:.3f}:end={e:.3f},"
                f"asetpts=PTS-STARTPTS,"
                f"aformat=sample_fmts=fltp:channel_layouts=stereo:sample_rates=48000"
                f"[a{i}]"
            )
            inputs_order.append(f"[a{i}]")

    a_flag = 1 if has_audio else 0
    if has_audio:
        parts.append(
            f"{''.join(inputs_order)}concat=n={n}:v=1:a={a_flag}[outv][outa]"
        )
    else:
        parts.append(
            f"{''.join(inputs_order)}concat=n={n}:v=1:a=0[outv]"
        )
    filter_complex = ";".join(parts)

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(video_in),
        "-filter_complex", filter_complex,
        "-map", "[outv]",
    ]
    if has_audio:
        cmd += ["-map", "[outa]", "-c:a", "aac",
                "-b:a", cfg["video"]["audio_bitrate"]]
    cmd += [
        *nvenc_video_args(cfg),
        str(video_out),
    ]
    subprocess.run(cmd, check=True)

    final_dur = sum(e - s for s, e in keeps)
    removed = duration - final_dur
    log.info(
        f"  Тишина: убрано {removed:.1f}с из {duration:.1f}с "
        f"(оставлено {n} сегментов)"
    )
    return video_out, {
        "removed_sec": round(removed, 2),
        "original_sec": round(duration, 2),
        "final_sec": round(final_dur, 2),
        "segments_kept": n,
    }
