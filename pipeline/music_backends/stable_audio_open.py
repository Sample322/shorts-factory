"""Stable Audio Open 1.0 backend (default, легкий).

Лицензия: Stability AI Community License — коммерческое использование
разрешено для проектов с годовой выручкой <$1M.

Архитектура:
- Модель ~4.5 ГБ, скачивается из HuggingFace при первом вызове.
- На RTX 4070 Ti 12 ГБ работает в fp16, ~10 сек на 30-секундный трек.
- Поддерживает batch — 3 варианта генерятся одним вызовом.

Перенесено из старого pipeline/music_gen.py без изменений в логике.
"""

from __future__ import annotations

import gc
import random as _random
import subprocess
from pathlib import Path
from typing import Callable

from ..utils import get_logger
from .base import MusicVariant

log = get_logger("music_gen.sao")


# Stable Audio Open промпт = описательная фраза (Open работает лучше с естественным
# языком чем с CSV-тегами). Стили привязаны к виральным TikTok/Shorts трендам.
# Каждый mood — 4-5 промптов, выбирается случайно.
MOOD_PROMPT_VARIANTS: dict[str, list[str]] = {
    "dramatic": [
        "epic cinematic orchestral instrumental like Hans Zimmer, deep brass swells, "
        "layered string ostinato, piano arpeggios, building tension, 65 BPM, no vocals",
        "neoclassical piano instrumental like Ludovico Einaudi, sweeping strings, "
        "minor key, melancholic and emotional, 92 BPM, no vocals",
        "epic hybrid trailer instrumental like Two Steps From Hell, dark orchestra, "
        "hybrid drums, brass risers, 100 BPM, no vocals",
        "classical drill instrumental, fast violin Vivaldi melody, sliding 808 bass, "
        "UK drill hi-hats, dramatic minor key, 140 BPM, no vocals",
    ],
    "chill": [
        "lofi hip hop instrumental like Lofi Girl, vinyl crackle, jazzy piano chords, "
        "warm Rhodes, mellow boom-bap drums, relaxing, 80 BPM, no vocals",
        "chill synthwave instrumental like Narvent Her Eyes, dreamy supersaw pads, "
        "slow 808 bass, retro 80s drum machine, neon night, 100 BPM, no vocals",
        "ethereal dream-pop instrumental like Billie Eilish, sparse muted piano, "
        "airy reverb pads, melancholic minor, bedroom-pop, 73 BPM, no vocals",
        "soft cinematic piano like Sleeping At Last, gentle strings, sparse "
        "percussion, peaceful, 80 BPM, C major, no vocals",
    ],
    "upbeat": [
        "uplifting acoustic pop like Forrest Frank, strummed guitar, hand claps, "
        "warm gospel pad, joyful major key, 110 BPM, no vocals",
        "club dance-pop like Dua Lipa Levitating, four-on-the-floor kick, supersaw "
        "stabs, bright synth, 124 BPM, no vocals",
        "future house like Calvin Harris, plucky synth lead, big room build, summer "
        "vibes, 126 BPM, A major, no vocals",
        "modern trap pop, snappy 808s, bouncy synth riff, catchy melody, confident, "
        "130 BPM, no vocals",
    ],
    "inspirational": [
        "soaring cinematic instrumental like Hans Zimmer Cornfield Chase, church organ "
        "pad, piano arpeggios, swelling strings, hopeful, 120 BPM, no vocals",
        "epic inspirational anthem like Audiomachine, soaring strings, uplifting piano, "
        "triumphant brass climax, 100 BPM, no vocals",
        "uplifting cinematic pop like Coldplay Sky Full of Stars, bright piano "
        "arpeggios, layered strings, anthemic, 125 BPM, A major, no vocals",
    ],
    "hype": [
        "dark aggressive phonk like Kordhell Murder In My Mind, distorted 808 cowbells, "
        "heavy bass slides, eerie minor synth, lofi tape hiss, 138 BPM, no vocals",
        "drift phonk like Hensonn Sahara, deep saturated bass, snappy trap drums, "
        "hypnotic Middle Eastern minor melody, 130 BPM, no vocals",
        "brazilian phonk funk, pitched-down vocal chops, hard 808 bass, distorted "
        "kicks, dark minor piano stabs, 140 BPM, no vocals",
        "modern hard trap like Travis Scott, booming 808 bass, sharp clap snare, "
        "dark minor piano loop, fast hi-hat triplets, 145 BPM, no vocals",
    ],
    "sus": [
        "dark retro synth horror like Stranger Things, low drone bass, eerie arpeggios, "
        "vintage analog, tense, 90 BPM, E minor, no vocals",
        "cinematic tension like Hans Zimmer Dark Knight, sustained low strings, sparse "
        "percussion stabs, deep sub bass, 80 BPM, no vocals",
        "dark ambient horror, low drone, distant metallic percussion, dissonant minor "
        "pads, psychological thriller, 70 BPM, no vocals",
    ],
    # ── НОВЫЕ MOOD'ы ──
    "comedy": [
        "quirky comedic orchestra like Addams Family, pizzicato strings, playful "
        "clarinet, walking bass, sneaky cartoon, 130 BPM, E minor, no vocals",
        "comedic upright bass like Curb Your Enthusiasm theme, tuba, playful trumpet, "
        "awkward stop-and-go, 100 BPM, C major, no vocals",
        "bouncy circus comedy like Benny Hill, tuba bass, snare rolls, accordion, "
        "slapstick, 140 BPM, C major, no vocals",
        "playful pizzicato strings like Animal Crossing, light xylophone, bouncy "
        "ukulele, cute melody, 115 BPM, major key, no vocals",
        "quirky kazoo and plucked strings like Wes Anderson soundtrack, light "
        "percussion, comedic minor melody, 110 BPM, D minor, no vocals",
    ],
    "epic": [
        "epic orchestral like Two Steps From Hell Heart of Courage, massive choir, "
        "war drums, heroic brass, soaring strings, 140 BPM, E minor, no vocals",
        "epic fantasy orchestra like Lord of the Rings, choir, taiko drums, brass "
        "fanfare, 120 BPM, D minor, no vocals",
        "epic hybrid trailer like Audiomachine, big drums, distorted brass, choir "
        "hits, riser, 130 BPM, C minor, no vocals",
        "dark medieval epic like Game of Thrones, cello ostinato, war drums, choir, "
        "brass, 110 BPM, D minor, no vocals",
    ],
    "action": [
        "hard electronic action like John Wick, distorted synth bass, fast breakbeats, "
        "dark industrial, 150 BPM, F minor, no vocals",
        "tribal action drums like Mad Max, taiko, heavy percussion, electric guitar "
        "riff, 155 BPM, E minor, no vocals",
        "spy action like Mission Impossible, urgent strings, pulsing bass, snare "
        "hits, 165 BPM, G minor, no vocals",
        "drum and bass action, fast breaks, reese bass, dark synth lead, 174 BPM, "
        "A minor, no vocals",
    ],
    "drama": [
        "melancholic piano solo like River Flows In You by Yiruma, emotional, slow "
        "tempo, sparse strings, sad, 70 BPM, B minor, no vocals",
        "neoclassical strings like Max Richter On The Nature Of Daylight, slow, "
        "deeply emotional, sustained chords, 60 BPM, E minor, no vocals",
        "emotional cinematic piano like Interstellar Aurora, soft strings, gentle "
        "build, hopeful sadness, 80 BPM, C minor, no vocals",
        "emotional Ghibli piano like Joe Hisaishi, gentle strings, melancholic "
        "melody, 75 BPM, F minor, no vocals",
    ],
}

NEGATIVE_PROMPT = "low quality, vocals, lyrics, speech, distortion, harsh noise"

MODEL_ID = "stabilityai/stable-audio-open-1.0"
_REQUIRED_FILES = [
    "model_index.json",
    "scheduler/scheduler_config.json",
    "text_encoder/config.json",
    "text_encoder/model.safetensors",
    "tokenizer/special_tokens_map.json",
    "tokenizer/spiece.model",
    "tokenizer/tokenizer_config.json",
    "transformer/config.json",
    "transformer/diffusion_pytorch_model.safetensors",
    "vae/config.json",
    "vae/diffusion_pytorch_model.safetensors",
    "projection_model/config.json",
    "projection_model/diffusion_pytorch_model.safetensors",
]


def _pick_prompt(mood: str) -> str:
    variants = MOOD_PROMPT_VARIANTS.get(mood) or MOOD_PROMPT_VARIANTS["dramatic"]
    return _random.choice(variants)


def _build_prompt(mood: str, custom_hint: str = "") -> str:
    base = _pick_prompt(mood)
    if custom_hint:
        return f"{base}, {custom_hint}"
    return base


def is_model_cached() -> tuple[bool, list[str]]:
    try:
        from huggingface_hub import try_to_load_from_cache
    except ImportError:
        return False, ["huggingface_hub not installed"]
    missing: list[str] = []
    for rel in _REQUIRED_FILES:
        path = try_to_load_from_cache(MODEL_ID, rel)
        if path is None:
            missing.append(rel)
    return len(missing) == 0, missing


class StableAudioOpenBackend:
    """Реализует MusicBackend через diffusers + StableAudioPipeline."""

    name = "stable_audio_open"

    def __init__(self) -> None:
        self._pipeline = None

    def is_available(self) -> tuple[bool, str]:
        try:
            import diffusers  # noqa: F401
            import torch
            import torchsde  # noqa: F401
            import accelerate  # noqa: F401
        except ImportError as e:
            return False, f"Нет зависимости: {e}"

        if not torch.cuda.is_available():
            return False, "Нет CUDA — модель работает только на GPU"
        free_gb = torch.cuda.mem_get_info()[0] / 1024**3
        if free_gb < 6:
            return False, f"Мало свободной VRAM ({free_gb:.1f} ГБ < 6 ГБ)"

        cached, missing = is_model_cached()
        if not cached:
            return False, (
                f"Модель не скачана полностью. Нет файлов: {missing[:3]}"
                f"{'...' if len(missing) > 3 else ''}. "
                "Сайдбар → AI-музыка → «⬇️ Скачать модель»."
            )
        return True, "OK"

    def _load_pipeline(self, max_retries: int = 3):
        if self._pipeline is not None:
            return self._pipeline

        import time as _time

        import torch
        from diffusers import StableAudioPipeline

        cached, missing = is_model_cached()
        if not cached:
            raise RuntimeError(
                f"Модель не скачана полностью. Нет файлов: {missing}."
            )

        last_err: Exception | None = None
        for attempt in range(max_retries):
            try:
                log.info(
                    f"Загружаю Stable Audio Open 1.0 из кеша "
                    f"(local_files_only, попытка {attempt + 1})..."
                )
                self._pipeline = StableAudioPipeline.from_pretrained(
                    MODEL_ID,
                    torch_dtype=torch.float16,
                    local_files_only=True,
                ).to("cuda")
                log.info("Модель готова")
                return self._pipeline
            except Exception as e:
                last_err = e
                log.warning(f"Загрузка не удалась (попытка {attempt + 1}): {e}")
                gc.collect()
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                if attempt < max_retries - 1:
                    _time.sleep(2 ** attempt)
        raise RuntimeError(
            f"Не удалось загрузить модель после {max_retries} попыток: {last_err}"
        )

    def generate_variants(
        self,
        out_dir: Path,
        mood: str,
        duration_sec: float = 30.0,
        n_variants: int = 3,
        custom_hint: str = "",
        base_seed: int | None = None,
        progress_cb: Callable[[str, float], None] | None = None,
    ) -> list[MusicVariant]:
        import soundfile as sf
        import torch

        out_dir.mkdir(parents=True, exist_ok=True)
        prompt = _build_prompt(mood, custom_hint)
        if base_seed is None:
            base_seed = torch.seed() & 0xFFFFFFFF

        if progress_cb:
            progress_cb("loading", 0)
        pipe = self._load_pipeline()
        if progress_cb:
            progress_cb("loading", 100)

        log.info(
            f"Генерирую {n_variants} вариантов музыки '{mood}' "
            f"({duration_sec:.0f}с, seed={base_seed})"
        )
        log.info(f"  Prompt: {prompt}")

        generator = torch.Generator("cuda").manual_seed(base_seed)
        if progress_cb:
            progress_cb("generating", 0)

        result = pipe(
            prompt,
            negative_prompt=NEGATIVE_PROMPT,
            num_inference_steps=150,
            audio_end_in_s=float(duration_sec),
            num_waveforms_per_prompt=n_variants,
            generator=generator,
        )
        audios = result.audios

        if progress_cb:
            progress_cb("generating", 100)
            progress_cb("saving", 0)

        sr = pipe.vae.sampling_rate
        variants: list[MusicVariant] = []
        for i in range(audios.shape[0]):
            wav = audios[i].T.float().cpu().numpy()
            path = out_dir / f"variant_{i + 1}_{mood}.wav"
            sf.write(str(path), wav, sr)

            preview_path = path.with_suffix(".mp3")
            try:
                subprocess.run(
                    [
                        "ffmpeg", "-y", "-loglevel", "error",
                        "-i", str(path),
                        "-codec:a", "libmp3lame", "-b:a", "160k",
                        str(preview_path),
                    ],
                    check=True, capture_output=True, timeout=30,
                )
            except Exception as e:
                log.warning(f"  MP3 preview не создан, fallback на WAV: {e}")
                preview_path = path

            # Meta JSON рядом с WAV для UI debug
            import json as _json
            meta_path = path.with_suffix(".meta.json")
            meta_path.write_text(
                _json.dumps({
                    "index": i + 1,
                    "path": str(path),
                    "preview_path": str(preview_path),
                    "prompt": prompt,
                    "seed": base_seed + i,
                    "duration_sec": duration_sec,
                    "mood": mood,
                    "model": "Stable Audio Open 1.0",
                    "num_inference_steps": 150,
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            variants.append(MusicVariant(
                index=i + 1,
                path=path,
                preview_path=preview_path,
                prompt=prompt,
                seed=base_seed + i,
                duration_sec=duration_sec,
                mood=mood,
            ))
            log.info(
                f"  [OK] Вариант {i + 1}: {path.name} (mood={mood}, "
                f"prompt={prompt[:60]}...)"
            )

        if progress_cb:
            progress_cb("saving", 100)

        return variants

    def unload(self) -> None:
        if self._pipeline is None:
            return
        try:
            import torch
            del self._pipeline
            self._pipeline = None
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            log.info("Stable Audio Open выгружена из VRAM")
        except Exception as e:
            log.warning(f"Ошибка выгрузки: {e}")
