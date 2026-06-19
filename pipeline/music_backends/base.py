"""Базовые типы для music backends.

MusicVariant — DTO одного сгенерированного трека.
MusicBackend — Protocol который реализуют все backends.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol


@dataclass
class MusicVariant:
    """Одна сгенерированная музыкальная дорожка.

    path = WAV (lossless, для финального микса в audio_mix).
    preview_path = MP3 ~500 КБ (для UI-плеера без таймаутов).
    """

    index: int
    path: Path
    preview_path: Path
    prompt: str
    seed: int
    duration_sec: float
    mood: str


class MusicBackend(Protocol):
    """Протокол music-backend.

    Все реализации (StableAudioOpen / ACE-Step / ...) должны его выполнять.
    """

    def is_available(self) -> tuple[bool, str]:
        """Готов ли backend к работе.

        Returns:
            (ok, reason). reason — текст для UI если не ok.
            Проверяет зависимости, веса, VRAM.
        """
        ...

    def generate_variants(
        self,
        out_dir: Path,
        mood: str,
        duration_sec: float,
        n_variants: int,
        custom_hint: str,
        base_seed: int | None,
        progress_cb: Callable[[str, float], None] | None,
    ) -> list[MusicVariant]:
        """Генерирует n_variants треков под mood.

        progress_cb(stage, pct) для UI:
        stage in ("loading", "generating", "saving").
        """
        ...

    def unload(self) -> None:
        """Освобождает модель из VRAM. После генерации обязательно зовём,
        иначе Whisper/demucs не загрузятся."""
        ...
