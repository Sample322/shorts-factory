"""Standalone worker для ACE-Step 1.5 — запускается из isolated venv.

Изолирован от main pipeline venv (ACE-Step требует жёстко
закрученные pin'ы deps которые сломают faster-whisper и stable_audio_open).

Протокол:
  stdin:  JSON {"mood": str, "duration_sec": float, "n_variants": int,
                "out_dir": str, "base_seed": int|null, "custom_hint": str}
  stdout: JSON {"variants": [{"index": int, "path": str, "preview_path": str,
                              "prompt": str, "seed": int,
                              "duration_sec": float, "mood": str}, ...]}
  stderr: лог
  exit:   0=OK, 1=ошибка (детали в stderr)

Вызывается из pipeline/music_backends/ace_step.py через subprocess.
"""

from __future__ import annotations

import json
import random as _random
import subprocess
import sys
import traceback
from pathlib import Path

# ACE-Step промпт = CSV-теги (genre, instruments, bpm, key, vibe).
# Промпты привязаны к виральным трендам TikTok/Shorts 2024-2025 для
# максимальной похожести на то что чарты крутят.
# Каждый mood — 4-6 промптов, выбирается случайно.
MOOD_PROMPTS: dict[str, list[str]] = {
    # Серьёзные/драматические сцены — конфликт, шок, эмоциональный пик
    "dramatic": [
        # Hans Zimmer Time-style cinematic
        "cinematic orchestral, deep brass swells, layered string ostinato, "
        "piano arpeggio, slow build to massive climax, hans zimmer style, "
        "instrumental, no vocals, 65bpm, c minor",
        # Ludovico Einaudi Experience-style neoclassical
        "neoclassical piano, sweeping legato strings, emotional, minor key, "
        "sustained cello pads, melancholic, einaudi style, instrumental, "
        "no vocals, 92bpm, a minor",
        # Dark trailer / Hybrid (Two Steps From Hell)
        "epic hybrid trailer, dark orchestra, hybrid drums, brass risers, "
        "massive impacts, two steps from hell style, instrumental, no vocals, "
        "100bpm, d minor",
        # Classical drill (viral TikTok 2024)
        "classical drill, fast violin vivaldi melody, sliding 808 bass, "
        "uk drill hi-hats, dramatic minor, instrumental, no vocals, 140bpm, "
        "f minor",
    ],
    # Расслабленные сцены — разговор, размышления, романтика, флешбек
    "chill": [
        # Lofi Girl / Lofi Hip Hop (vibe бесконечный)
        "lofi hip hop, vinyl crackle, jazzy piano chords, warm rhodes, "
        "mellow boombap drums, lofi girl style, instrumental, no vocals, 80bpm",
        # Chill phonk (Narvent Her Eyes)
        "chill synthwave, dreamy supersaw pads, slow 808 bass, retro drum "
        "machine, nostalgic minor melody, neon night, narvent style, "
        "instrumental, no vocals, 100bpm",
        # Bedroom pop (Billie Eilish)
        "ethereal dream pop, sparse muted piano, airy reverb pads, soft drums, "
        "melancholic minor, intimate bedroom pop, billie eilish style, "
        "instrumental, no vocals, 73bpm",
        # Sleeping At Last cinematic chill
        "soft cinematic piano, gentle strings, sparse percussion, peaceful, "
        "emotional, sleeping at last style, instrumental, no vocals, 80bpm, "
        "c major",
    ],
    # Энергичные сцены — динамика, активность, динамичный разговор
    "upbeat": [
        # Forrest Frank / Christian pop (viral 2024-25)
        "uplifting acoustic pop, strummed guitar, hand claps, warm gospel pad, "
        "joyful major key, forrest frank style, instrumental, no vocals, 110bpm",
        # Dua Lipa Levitating-style dance pop
        "club dance pop, four on the floor kick, supersaw stabs, bright minor "
        "synth, dua lipa style, instrumental, no vocals, 124bpm",
        # House meets pop (Calvin Harris)
        "future house, plucky synth lead, big room build, summer vibes, "
        "calvin harris style, instrumental, no vocals, 126bpm, a major",
        # Modern bouncy trap pop
        "trap pop, snappy 808s, bouncy synth riff, catchy melody, confident, "
        "instrumental, no vocals, 130bpm",
    ],
    # Воодушевляющие сцены — прорыв, инсайт, мощная мысль
    "inspirational": [
        # Hans Zimmer Interstellar Cornfield Chase
        "soaring cinematic, church organ pad, piano arpeggios, swelling strings, "
        "hopeful tension, interstellar style, instrumental, no vocals, 120bpm",
        # Audiomachine epic anthem
        "epic inspirational anthem, soaring strings, uplifting piano, triumphant "
        "brass climax, audiomachine style, instrumental, no vocals, 100bpm",
        # Coldplay Sky Full of Stars-style uplifting
        "uplifting cinematic pop, bright piano arpeggios, layered strings, "
        "anthemic build, coldplay style, instrumental, no vocals, 125bpm, "
        "a major",
    ],
    # Драка, погоня, экшн, агрессия — viral phonk
    "hype": [
        # Kordhell Murder In My Mind (TikTok phonk king)
        "dark aggressive phonk, distorted 808 cowbells, heavy bass slides, "
        "eerie minor synth lead, lofi tape hiss, memphis horror, kordhell style, "
        "instrumental, no vocals, 138bpm",
        # Hensonn Sahara drift phonk
        "drift phonk, deep saturated bass, snappy trap drums, hypnotic middle "
        "eastern minor melody, distorted cowbells, hensonn style, instrumental, "
        "no vocals, 130bpm",
        # Brazilian funk phonk (viral 2024)
        "brazilian phonk funk, pitched-down vocal chops, hard 808 bass, "
        "distorted kicks, dark minor piano stabs, instrumental, no vocals, "
        "140bpm",
        # Travis Scott modern trap
        "modern hard trap, booming 808 bass, sharp clap snare, dark minor "
        "piano loop, fast hi-hat triplets, travis scott style, instrumental, "
        "no vocals, 145bpm",
    ],
    # Напряжение, мистика, неловкость, триллер
    "sus": [
        # Stranger Things-style synth horror
        "dark retro synth, low drone bass, eerie arpeggios, vintage analog, "
        "stranger things style, tense, instrumental, no vocals, 90bpm, e minor",
        # Hans Zimmer Dark Knight tension
        "cinematic tension, sustained low strings, sparse percussion stabs, "
        "deep sub bass, dark knight style, instrumental, no vocals, 80bpm",
        # Dark ambient horror
        "dark ambient horror, low drone, distant metallic percussion, dissonant "
        "minor pads, psychological thriller, instrumental, no vocals, 70bpm",
    ],
    # ── НОВЫЕ MOOD'Ы (раньше падали в dramatic fallback) ──
    # Комедия, абсурд, ирония — лёгкая весёлая музыка
    "comedy": [
        # Addams Family / Pink Panther meme staple
        "quirky comedic orchestra, pizzicato strings, playful clarinet, walking "
        "bass, sneaky cartoon, addams family style, instrumental, no vocals, "
        "130bpm, e minor",
        # Curb Your Enthusiasm theme (mega-viral TikTok)
        "comedic upright bass, tuba, playful trumpet, awkward stop-and-go, curb "
        "your enthusiasm style, instrumental, no vocals, 100bpm, c major",
        # Benny Hill / circus comedy
        "bouncy circus comedy, tuba bass, snare rolls, accordion, slapstick, "
        "benny hill style, instrumental, no vocals, 140bpm, c major",
        # Wii Music / Animal Crossing playful
        "playful pizzicato, light xylophone, bouncy ukulele, cute melody, "
        "animal crossing style, major key, instrumental, no vocals, 115bpm",
        # Modern comedy sting
        "quirky kazoo, plucked strings, light percussion, comedic minor melody, "
        "wes anderson style, instrumental, no vocals, 110bpm, d minor",
    ],
    # Масштабный финал, кульминация, эпическое событие
    "epic": [
        # Two Steps From Hell Heart of Courage
        "epic orchestral, massive choir, war drums, heroic brass, soaring strings, "
        "two steps from hell style, instrumental, no vocals, 140bpm, e minor",
        # Lord of the Rings finale
        "epic fantasy orchestra, choir, taiko drums, brass fanfare, lord of "
        "the rings style, instrumental, no vocals, 120bpm, d minor",
        # Hybrid trailer (Audiomachine)
        "epic hybrid trailer, big drums, distorted brass, choir hits, riser, "
        "audiomachine style, instrumental, no vocals, 130bpm, c minor",
        # Game-of-Thrones-like
        "dark medieval epic, cello ostinato, war drums, choir, brass, game of "
        "thrones style, instrumental, no vocals, 110bpm, d minor",
    ],
    # Драка, экшн, погоня, fight scene
    "action": [
        # John Wick electronic action
        "hard electronic action, distorted synth bass, fast breakbeats, dark "
        "industrial, john wick style, instrumental, no vocals, 150bpm, f minor",
        # Mad Max drum drive
        "tribal action drums, taiko, heavy percussion, electric guitar riff, "
        "mad max style, instrumental, no vocals, 155bpm, e minor",
        # Mission Impossible-style spy tension
        "spy action, urgent strings, pulsing bass, snare hits, mission impossible "
        "style, instrumental, no vocals, 165bpm, g minor",
        # Drum and bass action
        "drum and bass action, fast breaks, reese bass, dark synth lead, "
        "instrumental, no vocals, 174bpm, a minor",
    ],
    # Эмоциональная драма, грусть, потеря, прощание
    "drama": [
        # Sad piano (River Flows In You)
        "melancholic piano solo, emotional, slow tempo, sparse strings, sad, "
        "yiruma style, instrumental, no vocals, 70bpm, b minor",
        # Max Richter On The Nature Of Daylight
        "neoclassical strings, slow, deeply emotional, sustained chords, max "
        "richter style, instrumental, no vocals, 60bpm, e minor",
        # Hans Zimmer Aurora (Interstellar emotional)
        "emotional cinematic piano, soft strings, gentle build, hopeful sadness, "
        "interstellar aurora style, instrumental, no vocals, 80bpm, c minor",
        # Joe Hisaishi Ghibli sad
        "emotional ghibli piano, gentle strings, melancholic melody, hisaishi "
        "style, instrumental, no vocals, 75bpm, f minor",
    ],
}


def log(msg: str) -> None:
    """stderr — чтобы не мешать JSON stdout."""
    sys.stderr.write(f"[acestep_worker] {msg}\n")
    sys.stderr.flush()


def pick_prompt(mood: str, custom_hint: str = "") -> str:
    variants = MOOD_PROMPTS.get(mood) or MOOD_PROMPTS["dramatic"]
    base = _random.choice(variants)
    if custom_hint:
        return f"{base}, {custom_hint}"
    return base


def make_mp3_preview(wav_path: Path) -> Path:
    """Конвертит WAV в MP3 ~500KB для UI-плеера."""
    mp3 = wav_path.with_suffix(".mp3")
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", str(wav_path),
                "-codec:a", "libmp3lame", "-b:a", "160k",
                str(mp3),
            ],
            check=True, capture_output=True, timeout=30,
        )
        return mp3
    except Exception as e:
        log(f"MP3 preview fail, fallback на WAV: {e}")
        return wav_path


def _ensure_lora_compatible_layout(lora_repo: str, checkpoint_dir: str) -> bool:
    """Готовит LoRA репо под формат который ожидает ACE-Step.

    Community LoRA публикуются в PEFT-формате (adapter_model.safetensors +
    adapter_config.json). ACE-Step ищет diffusers-формат
    (pytorch_lora_weights.safetensors). Создаём alias-копию чтобы оба
    формата работали.

    Returns: True если LoRA готова к использованию, False если что-то не так.
    """
    if not lora_repo or lora_repo == "none":
        return False
    try:
        from huggingface_hub import snapshot_download
        import shutil

        snap_dir = Path(snapshot_download(lora_repo, cache_dir=checkpoint_dir))
        expected = snap_dir / "pytorch_lora_weights.safetensors"
        if expected.exists():
            log(f"LoRA {lora_repo}: diffusers-формат уже есть")
            return True
        # PEFT формат — копируем в diffusers имя
        peft_file = snap_dir / "adapter_model.safetensors"
        if peft_file.exists():
            shutil.copy2(peft_file, expected)
            log(
                f"LoRA {lora_repo}: PEFT → diffusers alias создан "
                f"(adapter_model.safetensors → pytorch_lora_weights.safetensors)"
            )
            return True
        # Иногда LoRA называется иначе
        candidates = list(snap_dir.glob("*.safetensors"))
        if candidates:
            # Берём первый .safetensors который не основная модель
            for cand in candidates:
                if "lora" in cand.name.lower() or "adapter" in cand.name.lower():
                    shutil.copy2(cand, expected)
                    log(
                        f"LoRA {lora_repo}: fallback alias {cand.name} → "
                        "pytorch_lora_weights.safetensors"
                    )
                    return True
        log(
            f"LoRA {lora_repo}: не найден .safetensors файл с weights. "
            f"Содержимое: {[f.name for f in snap_dir.iterdir()]}"
        )
        return False
    except Exception as e:
        log(f"LoRA {lora_repo} prep упал: {e}")
        return False


def generate(
    out_dir: Path,
    mood: str,
    duration_sec: float,
    n_variants: int,
    custom_hint: str,
    base_seed: int | None,
    lora_repo: str | None = None,
    lora_weight: float = 0.0,
    start_index: int = 1,
) -> list[dict]:
    """Генерит n_variants треков через ACE-Step. Возвращает list[dict]
    готовых к сериализации MusicVariant."""
    import torch

    if base_seed is None:
        base_seed = int(torch.seed() & 0xFFFFFFFF)

    log(f"Загружаю ACE-Step pipeline...")
    from acestep.pipeline_ace_step import ACEStepPipeline

    # bfloat16 если железо умеет (Ampere+), иначе float16.
    dtype_str = "bfloat16" if torch.cuda.is_bf16_supported() else "float16"
    log(f"dtype={dtype_str}, device=cuda:0")

    # ВАЖНО: checkpoint_dir в ASCII path (без кириллицы как у дефолтного
    # ~/.cache/ace-step/). Иначе ACE-Step упрётся в mojibake на Windows.
    # Чекпойнт качается через tools/acestep_download.py заранее.
    import os as _os
    ckpt_dir = _os.environ.get(
        "ACESTEP_CHECKPOINT_DIR",
        r"C:\shorts-factory\cache\ace-step-models",
    )
    log(f"checkpoint_dir={ckpt_dir}")

    pipe = ACEStepPipeline(
        checkpoint_dir=ckpt_dir,
        device_id=0,
        dtype=dtype_str,
        torch_compile=False,
    )

    # Pre-prep LoRA: ACE-Step ожидает diffusers-формат,
    # community LoRA приходит в PEFT-формате — кладём alias.
    # Если prep упал — отключаем LoRA, не валим job (graceful degradation).
    effective_lora = "none"
    effective_weight = 0.0
    if lora_repo and lora_repo != "none" and lora_weight > 0:
        if _ensure_lora_compatible_layout(lora_repo, ckpt_dir):
            effective_lora = lora_repo
            effective_weight = float(lora_weight)
        else:
            log(
                f"⚠️  LoRA {lora_repo} не готова, генерю без LoRA "
                "(база ACE-Step)"
            )

    variants: list[dict] = []
    out_dir.mkdir(parents=True, exist_ok=True)
    prompt = pick_prompt(mood, custom_hint)
    log(f"Prompt: {prompt}")

    for i in range(n_variants):
        seed = base_seed + i
        variant_idx = start_index + i
        wav_path = out_dir / f"variant_{variant_idx}_{mood}.wav"

        # LoRA параметры: используем effective_lora — если prep упал, тут "none"
        lora_arg = effective_lora
        current_weight = effective_weight
        log(
            f"Вариант {variant_idx}/{start_index + n_variants - 1} "
            f"(seed={seed}, lora={lora_arg}, lora_weight={current_weight})..."
        )
        # ACE-Step API (verified 0.2.0):
        #   __call__(prompt, lyrics, audio_duration, infer_step=60,
        #            guidance_scale=15, scheduler_type='euler', cfg_type='apg',
        #            omega_scale=10, manual_seeds=[...], save_path=..., format='wav')
        # infer_step=27 — компромисс скорость/качество (default 60 слишком долго
        # на консьюмерском GPU, 27 даёт ~10 сек/30с трек на 4070 Ti).
        try:
            pipe(
                format="wav",
                prompt=prompt,
                lyrics="",
                audio_duration=float(duration_sec),
                infer_step=27,
                guidance_scale=15.0,
                scheduler_type="euler",
                cfg_type="apg",
                omega_scale=10,
                manual_seeds=[seed],
                save_path=str(wav_path),
                lora_name_or_path=lora_arg,
                lora_weight=float(current_weight),
            )
        except TypeError as te:
            log(f"ACE-Step pipeline signature mismatch: {te}")
            raise
        except Exception as e:
            # Если LoRA включена и pipe() упал — пробуем без LoRA.
            # Любая ошибка с активной LoRA трактуется как несовместимость
            # (target modules, tensor shapes, format mismatch и т.д.).
            # Без LoRA если опять падает — это уже не LoRA-проблема, raise.
            if lora_arg != "none":
                log(
                    f"⚠️  LoRA {lora_arg} несовместима с ACE-Step "
                    f"({type(e).__name__}: {str(e)[:200]})"
                )
                log("    Перегенерю этот вариант без LoRA (база ACE-Step)")
                # Отключаем LoRA для ВСЕХ оставшихся вариантов в этом запуске
                effective_lora = "none"
                effective_weight = 0.0
                lora_arg = "none"
                current_weight = 0.0
                try:
                    pipe(
                        format="wav",
                        prompt=prompt,
                        lyrics="",
                        audio_duration=float(duration_sec),
                        infer_step=27,
                        guidance_scale=15.0,
                        scheduler_type="euler",
                        cfg_type="apg",
                        omega_scale=10,
                        manual_seeds=[seed],
                        save_path=str(wav_path),
                        lora_name_or_path="none",
                        lora_weight=0.0,
                    )
                except Exception as e2:
                    log(f"❌ Даже без LoRA pipe упал: {e2}")
                    raise
            else:
                raise

        # ACE-Step может сохранить файл в save_path ИЛИ в save_path + ".wav"/_idx.wav
        # — проверяем оба варианта
        if not wav_path.exists():
            # Пробуем варианты которые ACE-Step мог создать рядом
            alt = list(wav_path.parent.glob(f"{wav_path.stem}*.wav"))
            if alt:
                # Переименовываем первый найденный в наш expected name
                alt[0].replace(wav_path)
                log(f"  ACE-Step сохранил как {alt[0].name}, переименовал в {wav_path.name}")
            else:
                raise RuntimeError(
                    f"ACE-Step не создал файл {wav_path}. "
                    f"Содержимое директории: {list(wav_path.parent.iterdir())[:5]}"
                )

        preview = make_mp3_preview(wav_path)

        meta_json = wav_path.with_suffix(".meta.json")
        meta_data = {
            "index": variant_idx,
            "path": str(wav_path),
            "preview_path": str(preview),
            "prompt": prompt,
            "seed": seed,
            "duration_sec": float(duration_sec),
            "mood": mood,
            "model": "ACE-Step v1-3.5B",
            "infer_step": 27,
            "guidance_scale": 15.0,
            "scheduler": "euler",
            "cfg_type": "apg",
            "dtype": dtype_str,
            "lora_repo": lora_arg,
            "lora_weight": float(current_weight),
            "lora_requested": lora_repo or "none",  # что просили в UI
            "lora_used": lora_arg,  # что реально применилось (м.б. "none" если fallback)
        }
        import json as _json
        meta_json.write_text(
            _json.dumps(meta_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        variants.append({
            "index": variant_idx,
            "path": str(wav_path),
            "preview_path": str(preview),
            "prompt": prompt,
            "seed": seed,
            "duration_sec": float(duration_sec),
            "mood": mood,
        })
        log(f"[OK] {wav_path.name} (mood={mood}, prompt={prompt[:60]}...)")

    return variants


def main() -> int:
    try:
        raw = sys.stdin.read()
        # PowerShell pipe пишет UTF-8 BOM; обрезаем если есть
        if raw.startswith("﻿"):
            raw = raw[1:]
        params = json.loads(raw)
    except json.JSONDecodeError as e:
        log(f"Bad stdin JSON: {e}")
        return 1

    try:
        variants = generate(
            out_dir=Path(params["out_dir"]),
            mood=params["mood"],
            duration_sec=float(params["duration_sec"]),
            n_variants=int(params["n_variants"]),
            custom_hint=params.get("custom_hint", ""),
            base_seed=params.get("base_seed"),
            lora_repo=params.get("lora_repo"),
            lora_weight=float(params.get("lora_weight", 0.0)),
            start_index=int(params.get("start_index", 1)),
        )
    except Exception:
        log("Generation crashed:")
        log(traceback.format_exc())
        return 1

    sys.stdout.write(json.dumps({"variants": variants}, ensure_ascii=False))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
