"""Router для music backends — выбирает между Stable Audio Open и ACE-Step.

Backend выбирается через config.yaml → music.backend:
  - stable_audio_open: default, ~10 сек/30с на 4070 Ti, лёгкий
  - ace_step:          ACE-Step 1.5 через isolated venv (продуктовый)

Старое публичное API сохранено для обратной совместимости с render.py:
  - generate_variants(out_dir, mood, ...) → list[MusicVariant]
  - is_model_available() → (ok, reason)
  - is_model_cached() → (ok, missing) — только для Stable Audio Open
  - unload_pipeline()
  - MOOD_PROMPT_VARIANTS, MOOD_PROMPTS, MusicVariant
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from .music_backends.ace_step import ACEStepBackend
from .music_backends.base import MusicVariant
from .music_backends.stable_audio_open import (
    MOOD_PROMPT_VARIANTS,
    StableAudioOpenBackend,
    is_model_cached,
)
from .utils import get_logger, load_config

log = get_logger("music_gen")

# Старый формат — оставлен для обратной совместимости (импорты из старого кода)
MOOD_PROMPTS = {k: v[0] for k, v in MOOD_PROMPT_VARIANTS.items()}


def _load_backend_name() -> str:
    """Читает music.backend из config.yaml. Default: stable_audio_open."""
    try:
        cfg_path = Path(__file__).resolve().parents[1] / "config.yaml"
        cfg = load_config(str(cfg_path))
        name = (cfg.get("music") or {}).get("backend", "stable_audio_open")
        return str(name).strip().lower()
    except Exception as e:
        log.warning(f"Не могу прочитать music.backend из config: {e}. "
                    "Использую stable_audio_open.")
        return "stable_audio_open"


# Singleton backends — лениво создаются и переиспользуются
_BACKENDS: dict[str, object] = {}


def _get_backend(name: str):
    """Возвращает singleton нужного backend'а."""
    if name in _BACKENDS:
        return _BACKENDS[name]
    if name == "ace_step":
        _BACKENDS[name] = ACEStepBackend()
    elif name == "stable_audio_open":
        _BACKENDS[name] = StableAudioOpenBackend()
    else:
        log.warning(f"Неизвестный backend '{name}', fallback на stable_audio_open")
        _BACKENDS["stable_audio_open"] = StableAudioOpenBackend()
        return _BACKENDS["stable_audio_open"]
    return _BACKENDS[name]


def _current_backend():
    """Активный backend по config + env override."""
    # ENV override для тестов: MUSIC_BACKEND=ace_step
    name = os.environ.get("MUSIC_BACKEND") or _load_backend_name()
    return _get_backend(name)


# ───────────────────────── Публичное API ─────────────────────────


def is_model_available() -> tuple[bool, str]:
    """Готов ли активный backend к работе."""
    backend = _current_backend()
    log.info(f"Активный music-backend: {backend.name}")
    return backend.is_available()


def generate_variants(
    out_dir: Path,
    mood: str,
    duration_sec: float = 30.0,
    n_variants: int = 3,
    custom_hint: str = "",
    base_seed: int | None = None,
    num_inference_steps: int = 150,  # legacy kwarg, игнорится в ace_step
    progress_cb: Callable[[str, float], None] | None = None,
    lora_repo: str | None = None,
    lora_weight: float = 0.0,
    start_index: int = 1,
) -> list[MusicVariant]:
    """Делегирует активному backend'у. Поддерживает LoRA (только ace_step)."""
    backend = _current_backend()
    # LoRA kwargs — только ace_step backend поддерживает.
    # Для других backend'ов LoRA параметры игнорируются.
    extra: dict = {}
    if hasattr(backend, "generate_variants"):
        import inspect
        sig = inspect.signature(backend.generate_variants)
        if "lora_repo" in sig.parameters:
            extra["lora_repo"] = lora_repo
            extra["lora_weight"] = lora_weight
        if "start_index" in sig.parameters:
            extra["start_index"] = start_index
    return backend.generate_variants(
        out_dir=out_dir,
        mood=mood,
        duration_sec=duration_sec,
        n_variants=n_variants,
        custom_hint=custom_hint,
        base_seed=base_seed,
        progress_cb=progress_cb,
        **extra,
    )


def unload_pipeline() -> None:
    """Освобождает VRAM активного backend'а."""
    for backend in _BACKENDS.values():
        try:
            backend.unload()
        except Exception as e:
            log.warning(f"Ошибка unload {getattr(backend, 'name', '?')}: {e}")


# Реэкспорт для существующих импортов
__all__ = [
    "MOOD_PROMPTS",
    "MOOD_PROMPT_VARIANTS",
    "MusicVariant",
    "generate_variants",
    "is_model_available",
    "is_model_cached",
    "unload_pipeline",
]
