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

import json
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


def _music_cache_key(
    backend_name: str, mood: str, duration_sec: float, n_variants: int,
    custom_hint: str, base_seed: int | None,
    lora_repo: str | None, lora_weight: float,
) -> str:
    """Stable hash для music generation. Идентичные параметры → один файл из кеша."""
    import hashlib
    parts = [
        backend_name, mood, f"{duration_sec:.1f}", str(n_variants),
        custom_hint or "", str(base_seed or "none"),
        lora_repo or "none", f"{lora_weight:.2f}",
    ]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _try_load_music_cache(
    out_dir: Path, cache_key: str, mood: str, n_variants: int,
) -> list[MusicVariant] | None:
    """Resume: ищем готовые треки в global music cache."""
    import shutil as _sh
    cache_root = Path("cache") / "music_variants" / cache_key
    if not cache_root.exists():
        return None
    try:
        meta_files = sorted(cache_root.glob("variant_*.meta.json"))
        if len(meta_files) < n_variants:
            return None
        out_dir.mkdir(parents=True, exist_ok=True)
        variants: list[MusicVariant] = []
        for i, meta_f in enumerate(meta_files[:n_variants], 1):
            meta = json.loads(meta_f.read_text(encoding="utf-8"))
            src_wav = Path(meta["path"])
            if not src_wav.exists():
                # path в meta может быть устаревшим — пробуем relative
                src_wav = cache_root / f"variant_{meta['index']}_{meta['mood']}.wav"
            if not src_wav.exists():
                return None
            # Копируем в out_dir с новыми именами variant_1..N
            dst_wav = out_dir / f"variant_{i}_{mood}.wav"
            dst_mp3 = dst_wav.with_suffix(".mp3")
            dst_meta = dst_wav.with_suffix(".meta.json")
            _sh.copy2(src_wav, dst_wav)
            src_mp3 = src_wav.with_suffix(".mp3")
            if src_mp3.exists():
                _sh.copy2(src_mp3, dst_mp3)
            else:
                dst_mp3 = dst_wav
            _sh.copy2(meta_f, dst_meta)
            variants.append(MusicVariant(
                index=i, path=dst_wav, preview_path=dst_mp3,
                prompt=meta.get("prompt", ""), seed=meta.get("seed", 0),
                duration_sec=meta.get("duration_sec", 30.0),
                mood=meta.get("mood", mood),
            ))
        log.info(f"💾 Music cache HIT: {len(variants)} вариантов из {cache_root}")
        return variants
    except Exception as e:
        log.warning(f"Music cache load fail: {e}")
        return None


def _save_music_cache(
    cache_key: str, variants: list[MusicVariant],
) -> None:
    """Копируем сгенерированные варианты в global cache."""
    import shutil as _sh
    if not variants:
        return
    cache_root = Path("cache") / "music_variants" / cache_key
    cache_root.mkdir(parents=True, exist_ok=True)
    try:
        for v in variants:
            for src in (v.path, v.path.with_suffix(".mp3"),
                        v.path.with_suffix(".meta.json")):
                if src.exists():
                    _sh.copy2(src, cache_root / src.name)
        log.info(f"💾 Music cache SAVED: {cache_root}")
    except Exception as e:
        log.warning(f"Music cache save fail: {e}")


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
    """Делегирует активному backend'у. Поддерживает LoRA + caching."""
    backend = _current_backend()
    backend_name = getattr(backend, "name", "unknown")
    # Cache: тот же набор параметров → готовые треки
    cache_key = _music_cache_key(
        backend_name, mood, duration_sec, n_variants,
        custom_hint, base_seed, lora_repo, lora_weight,
    )
    cached = _try_load_music_cache(out_dir, cache_key, mood, n_variants)
    if cached:
        if progress_cb:
            progress_cb("loading", 100)
            progress_cb("generating", 100)
            progress_cb("saving", 100)
        return cached
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
    result = backend.generate_variants(
        out_dir=out_dir,
        mood=mood,
        duration_sec=duration_sec,
        n_variants=n_variants,
        custom_hint=custom_hint,
        base_seed=base_seed,
        progress_cb=progress_cb,
        **extra,
    )
    # Save to global cache for future runs with same params
    if result:
        _save_music_cache(cache_key, result)
    return result


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
