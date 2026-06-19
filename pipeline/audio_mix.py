"""Микс речи + фоновой музыки с sidechain-дакингом."""

import random
import subprocess
from pathlib import Path

from .utils import get_logger, has_audio_stream

log = get_logger("audio_mix")


def pick_music(mood: str, music_dir: Path) -> Path | None:
    """Выбирает случайный трек из подпапки настроения."""
    if not music_dir.exists():
        return None

    mood_dir = music_dir / mood
    if not mood_dir.exists():
        log.warning(f"Папка {mood_dir} не существует, fallback на 'chill'")
        mood_dir = music_dir / "chill"

    if not mood_dir.exists():
        return None

    tracks = (
        list(mood_dir.glob("*.mp3"))
        + list(mood_dir.glob("*.wav"))
        + list(mood_dir.glob("*.m4a"))
    )
    if not tracks:
        log.warning(f"В {mood_dir} нет треков, музыка не добавлена")
        return None

    return random.choice(tracks)


def mix_with_music(
    video_in: Path, music_path: Path, video_out: Path, cfg: dict,
    music_volume_override: float | None = None,
) -> Path:
    """Накладывает музыку под речь с автоматическим дакингом.

    music_volume_override — переопределение громкости (0.0-1.0), если None
    то берётся из cfg["audio"]["music_volume"].

    Если в исходном видео нет аудио — просто кладём музыку как саундтрек.
    """
    a = dict(cfg["audio"])  # копия чтобы не мутировать cfg
    if music_volume_override is not None:
        a["music_volume"] = max(0.0, min(1.0, music_volume_override))
    has_voice = has_audio_stream(video_in)

    # aformat-префикс — насильственный downmix обоих входов в stereo.
    # Без него 5.1 surround источники (BluRay rip) ломают AAC encoder:
    # "Unsupported channel layout 6 channels".
    af = "aformat=sample_fmts=fltp:channel_layouts=stereo:sample_rates=48000"

    if not has_voice:
        # Просто музыка как саундтрек, нормализованная по громкости.
        fc = (
            f"[1:a]{af},aloop=loop=-1:size=2e+09,volume={a['music_volume']},"
            f"loudnorm=I={a['loudness_target_lufs']}:TP=-1.5:LRA=11[out]"
        )
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(video_in), "-i", str(music_path),
            "-filter_complex", fc,
            "-map", "0:v", "-map", "[out]",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", cfg["video"]["audio_bitrate"],
            "-ac", "2", "-ar", "48000",
            "-shortest", str(video_out),
        ]
        subprocess.run(cmd, check=True)
        return video_out

    # ВАЖНО: правильный порядок sidechaincompress!
    # Первый вход = signal который компрессится (МУЗЫКА).
    # Второй вход = sidechain trigger (ГОЛОС — когда он громкий, музыка дакается).
    #
    # Раньше было [0:a][music] — голос сжимался под музыку, а в amix
    # шёл голос + сжатый голос = музыки в финале не было слышно.
    fc = (
        f"[0:a]{af}[voice];"
        f"[1:a]{af},aloop=loop=-1:size=2e+09,volume={a['music_volume']}[music];"
        f"[music][voice]sidechaincompress=threshold={a['duck_threshold']}:ratio={a['duck_ratio']}:"
        f"attack={a['duck_attack_ms']}:release={a['duck_release_ms']}[ducked_music];"
        f"[voice][ducked_music]amix=inputs=2:duration=first:dropout_transition=0[mixed];"
        f"[mixed]loudnorm=I={a['loudness_target_lufs']}:TP=-1.5:LRA=11[out]"
    )

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(video_in), "-i", str(music_path),
        "-filter_complex", fc,
        "-map", "0:v", "-map", "[out]",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", cfg["video"]["audio_bitrate"],
        "-ac", "2", "-ar", "48000",
        "-shortest", str(video_out),
    ]
    subprocess.run(cmd, check=True)
    return video_out
