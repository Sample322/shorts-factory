"""Vocal isolation через demucs — решение "No restriction" Content ID claims.

Проблема: Content ID матчит оригинальный фирменный score сериала, который
"подтекает" под dialogue track в исходном видео. Даже после нашего
sidechain-ducking фоновая музыка слышна → YouTube её узнаёт → claim.

Решение: demucs `htdemucs` разделяет audio на 4 stems:
  - vocals (только речь/крики/вокал)
  - drums, bass, other (остальная музыка)

Берём только vocals.wav и используем его как dialogue track вместо
оригинального audio из клипа. Content ID больше не имеет что матчить
в музыкальной части — там тишина.

Установка:
    pip install demucs
    # модель ~800MB скачается автоматом при первом вызове

Использование:
    from pipeline.vocal_isolation import isolate_vocals
    voice_only_video = isolate_vocals(clip_path, work_dir, cfg)

Возвращает путь к видео где original audio заменён на voice-only track.
Если demucs не установлен или vocal_isolation_enabled=false → возвращает
исходный путь без изменений.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from .utils import get_logger

log = get_logger("vocal_isolation")

_DEMUCS_AVAILABLE: bool | None = None


def _check_demucs_available() -> bool:
    """Кеширует проверку доступности demucs."""
    global _DEMUCS_AVAILABLE
    if _DEMUCS_AVAILABLE is not None:
        return _DEMUCS_AVAILABLE
    try:
        import demucs.separate  # noqa: F401
        _DEMUCS_AVAILABLE = True
    except ImportError:
        _DEMUCS_AVAILABLE = False
    return _DEMUCS_AVAILABLE


def isolate_vocals(
    video_in: Path,
    work_dir: Path,
    cfg: dict,
) -> Path:
    """Отделяет голос от музыки в видео-клипе.

    Шаги:
    1. Извлекаем audio из видео через ffmpeg → input.wav
    2. demucs --two-stems=vocals разделяет → vocals.wav + no_vocals.wav
    3. Заменяем audio в видео на vocals.wav → output.mp4
    4. Возвращаем путь к новому видео

    Если demucs не доступен ИЛИ вылетел — возвращаем video_in без изменений.
    """
    safety = cfg.get("safety", {})
    if not safety.get("vocal_isolation_enabled", False):
        return video_in

    if not _check_demucs_available():
        log.warning(
            "vocal_isolation_enabled=true, но demucs не установлен. "
            "Запусти: pip install demucs. Пропускаю изоляцию."
        )
        return video_in

    model = safety.get("vocal_isolation_model", "htdemucs")
    work_dir.mkdir(parents=True, exist_ok=True)

    audio_wav = work_dir / "input_audio.wav"
    demucs_out_root = work_dir / "demucs_out"
    video_out = work_dir / f"{video_in.stem}_voice_only.mp4"

    try:
        # 1. Извлекаем audio
        log.info(f"  🎤 Vocal isolation: извлекаю audio из {video_in.name}")
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", str(video_in),
                "-vn", "-acodec", "pcm_s16le",
                "-ar", "44100", "-ac", "2",
                str(audio_wav),
            ],
            check=True,
        )

        # 2. demucs --two-stems=vocals (быстрее чем 4-stem split)
        # sys.executable — venv python с правильным окружением (torch + demucs)
        log.info(f"  🎤 Vocal isolation: запускаю demucs {model} (~30 сек)")
        subprocess.run(
            [
                sys.executable, "-m", "demucs",
                "--two-stems", "vocals",
                "-n", model,
                "-o", str(demucs_out_root),
                str(audio_wav),
            ],
            check=True,
        )

        # demucs кладёт результат в <out>/<model>/<input_stem>/vocals.wav
        vocals_wav = demucs_out_root / model / audio_wav.stem / "vocals.wav"
        if not vocals_wav.exists():
            log.warning(
                f"demucs не создал vocals.wav (ожидал {vocals_wav}), "
                "fallback на оригинал"
            )
            return video_in

        # 3. Заменяем audio в видео на vocals (видео copy, audio re-encode)
        log.info("  🎤 Vocal isolation: подменяю audio в видео")
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", str(video_in),
                "-i", str(vocals_wav),
                "-map", "0:v", "-map", "1:a",
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k",
                "-ac", "2", "-ar", "48000",
                "-shortest",
                str(video_out),
            ],
            check=True,
        )

        log.info(f"  ✅ Vocal isolation готова: {video_out.name}")
        return video_out

    except subprocess.CalledProcessError as e:
        log.warning(
            f"vocal_isolation упал, fallback на оригинал: {e}"
        )
        return video_in
    except Exception as e:  # noqa: BLE001
        log.warning(f"vocal_isolation неожиданная ошибка: {e}")
        return video_in
    finally:
        # Чистим временные файлы
        try:
            if audio_wav.exists():
                audio_wav.unlink()
            if demucs_out_root.exists():
                shutil.rmtree(demucs_out_root, ignore_errors=True)
        except OSError:
            pass
