"""Транскрипция через faster-whisper с word-level таймингами.

Архитектура:
1. Pre-extract: ffmpeg вытаскивает первую аудиодорожку → моно 16 кГц WAV.
   Это критично для длинных видео и multi-track/5.1 файлов — Whisper
   нативно не умеет выбирать дорожку и плохо стримит большие mkv.
2. Streaming-транскрипция: faster-whisper возвращает итератор, мы
   репортуем прогресс в callback по позиции каждого сегмента.
3. OOM-fallback: автоматически даунгрейдим compute_type/модель.
"""

from __future__ import annotations

import gc
import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import torch
from faster_whisper import WhisperModel

from .utils import file_hash, get_logger

log = get_logger("transcribe")


# Модели для OOM-фоллбэка (в порядке уменьшения VRAM)
_MODEL_FALLBACK_CHAIN = ["large-v3", "large-v2", "medium", "small", "base"]
# Compute types в порядке уменьшения качества/памяти
_COMPUTE_FALLBACK_CHAIN = ["float16", "int8_float16", "int8"]

# КРИТИЧНО: держим живые ссылки на WhisperModel на уровне модуля.
# Без этого Python GC после return из transcribe_many() освобождает
# локальную переменную → WhisperModel.__del__() → CUDA free → access
# violation 0xc0000374 (heap corruption) → worker умирает молча.
# Воркер всё равно скоро завершится сам, ОС освободит VRAM при exit.
_MODEL_LIVE_REFS: list = []


@dataclass
class TranscribeProgress:
    """Прогресс транскрипции, передаваемый в callback."""

    position_sec: float       # текущая позиция в видео (секунды)
    total_sec: float          # общая длительность видео
    segments_done: int        # сколько сегментов уже распознано
    elapsed_sec: float        # сколько секунд прошло реального времени
    speed_x: float            # во сколько раз быстрее реального времени

    @property
    def pct(self) -> float:
        return min(100.0, (self.position_sec / self.total_sec) * 100) if self.total_sec else 0.0

    @property
    def eta_sec(self) -> float:
        if self.speed_x <= 0 or self.position_sec >= self.total_sec:
            return 0.0
        remaining = self.total_sec - self.position_sec
        return remaining / self.speed_x


def _ffprobe_duration(path: Path) -> float:
    """Длительность видео в секундах через ffprobe (0 если не удалось)."""
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        return float(r.stdout.strip() or 0)
    except (subprocess.SubprocessError, ValueError, FileNotFoundError):
        return 0.0


def _extract_audio(
    video_path: Path,
    out_wav: Path,
    progress_cb: Callable[[float], None] | None = None,
) -> Path:
    """Извлекает первую аудиодорожку в моно 16 кГц WAV.

    Это решает несколько проблем:
    - 5.1 surround → даунмикс в моно
    - Множественные аудиодорожки → берём только первую (-map 0:a:0)
    - Whisper не нагружает CPU/GPU на decode большого mkv
    """
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    duration = _ffprobe_duration(video_path)

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-stats", "-progress", "pipe:1",
        "-i", str(video_path),
        "-map", "0:a:0",   # только первая аудиодорожка
        "-ac", "1",        # моно
        "-ar", "16000",    # 16 кГц — родная частота Whisper
        "-c:a", "pcm_s16le",
        "-vn",             # без видео
        str(out_wav),
    ]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    # Парсим out_time_ms из -progress pipe:1 для прогресса
    if proc.stdout is not None:
        for line in proc.stdout:
            if line.startswith("out_time_ms=") and progress_cb and duration > 0:
                try:
                    us = int(line.split("=", 1)[1].strip())
                    pos_sec = us / 1_000_000
                    progress_cb(min(100.0, pos_sec / duration * 100))
                except (ValueError, IndexError):
                    pass

    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg extract audio failed (code {proc.returncode})")
    return out_wav


def _load_model_with_fallback(
    preferred_model: str, device: str, preferred_compute: str
) -> tuple[WhisperModel, str, str]:
    """Загружает модель с fallback по цепочке если OOM/ошибка."""
    # Строим уникальные цепочки начиная с предпочитаемых значений
    models = [preferred_model] + [m for m in _MODEL_FALLBACK_CHAIN if m != preferred_model]
    computes = [preferred_compute] + [c for c in _COMPUTE_FALLBACK_CHAIN if c != preferred_compute]

    last_err: Exception | None = None
    for model_name in models:
        for compute in computes:
            try:
                log.info(f"Загружаю Whisper {model_name} / {compute} на {device}...")
                model = WhisperModel(model_name, device=device, compute_type=compute)
                return model, model_name, compute
            except (torch.cuda.OutOfMemoryError, RuntimeError, ValueError) as e:
                last_err = e
                log.warning(f"Не получилось загрузить {model_name}/{compute}: {e}")
                gc.collect()
                if device == "cuda":
                    torch.cuda.empty_cache()
                continue
    raise RuntimeError(f"Не удалось загрузить ни одну модель Whisper: {last_err}")


def transcribe(
    video_path: Path,
    cache_dir: Path,
    cfg: dict,
    progress_cb: Callable[[str, TranscribeProgress | float | None], None] | None = None,
    preloaded_model: tuple[WhisperModel, str, str] | None = None,
) -> dict:
    """Возвращает dict с ключами: text, segments, word_segments, language.

    progress_cb(stage, payload) вызывается с:
    - stage="extract_audio", payload=float (% от 0 до 100)
    - stage="transcribe", payload=TranscribeProgress

    preloaded_model: (model, model_name, compute_type) — для batch-режима
    (transcribe_many). Если None, модель грузится локально (одноразовый
    вызов). Передача preloaded ИЗБЕГАЕТ повторной загрузки 1.5-3 ГБ в VRAM
    и CUDA SegFault при создании второго экземпляра WhisperModel в одном
    процессе.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"transcript_{file_hash(video_path)}.json"

    if cache_file.exists():
        log.info(f"Использую кеш транскрипта: {cache_file.name}")
        if progress_cb:
            progress_cb("cache_hit", None)
        return json.loads(cache_file.read_text(encoding="utf-8"))

    wcfg = cfg["whisper"]
    device = wcfg["device"]
    duration = _ffprobe_duration(video_path)
    log.info(f"Длительность видео: {duration:.0f} сек ({duration/60:.1f} мин)")

    # --- ШАГ 1: Извлекаем аудио в WAV ---
    audio_wav = cache_dir / f"audio_{file_hash(video_path)}.wav"
    if audio_wav.exists() and audio_wav.stat().st_size > 1000:
        log.info(f"Использую кеш аудио: {audio_wav.name}")
    else:
        log.info("Извлекаю первую аудиодорожку в моно 16 кГц WAV...")
        t0 = time.monotonic()

        def _audio_cb(pct: float) -> None:
            if progress_cb:
                progress_cb("extract_audio", pct)

        _extract_audio(video_path, audio_wav, _audio_cb)
        log.info(
            f"Аудио извлечено за {time.monotonic() - t0:.1f} сек "
            f"({audio_wav.stat().st_size / 1024 / 1024:.1f} МБ)"
        )

    # --- ШАГ 2: Используем preloaded или загружаем модель ---
    if preloaded_model is not None:
        model, actual_model, actual_compute = preloaded_model
    else:
        model, actual_model, actual_compute = _load_model_with_fallback(
            wcfg["model"], device, wcfg["compute_type"]
        )
        if actual_model != wcfg["model"] or actual_compute != wcfg["compute_type"]:
            log.warning(
                f"Whisper {wcfg['model']}/{wcfg['compute_type']} не влез, "
                f"использую {actual_model}/{actual_compute}"
            )
        # Сохраняем ссылку чтобы GC не дёрнул __del__ → CUDA crash
        _MODEL_LIVE_REFS.append(model)

    # --- ШАГ 3: Транскрибируем стримом, репортуя прогресс ---
    log.info("Транскрибирую с word-level таймингами и VAD-фильтром...")
    t_start = time.monotonic()

    def _do_transcribe(use_vad: bool):
        return model.transcribe(
            str(audio_wav),
            language=wcfg["language"],
            beam_size=5,
            word_timestamps=True,
            vad_filter=use_vad,
            vad_parameters=dict(min_silence_duration_ms=500) if use_vad else None,
        )

    try:
        raw_segments, info = _do_transcribe(use_vad=True)
    except (torch.cuda.OutOfMemoryError, RuntimeError) as e:
        log.warning(f"OOM/ошибка с VAD ({e}), повтор без VAD")
        gc.collect()
        if device == "cuda":
            torch.cuda.empty_cache()
        raw_segments, info = _do_transcribe(use_vad=False)

    segments: list[dict] = []
    word_segments: list[dict] = []

    last_report = 0.0
    for seg in raw_segments:
        seg_dict = {
            "text": seg.text.strip(),
            "start": round(seg.start, 3),
            "end": round(seg.end, 3),
        }
        segments.append(seg_dict)

        if seg.words:
            for w in seg.words:
                word_segments.append(
                    {
                        "word": w.word.strip(),
                        "start": round(w.start, 3),
                        "end": round(w.end, 3),
                        "score": round(w.probability, 3),
                    }
                )

        # Репортуем прогресс не чаще 1 раза в секунду
        now = time.monotonic()
        if progress_cb and (now - last_report) > 1.0:
            elapsed = now - t_start
            speed_x = (seg.end / elapsed) if elapsed > 0 else 0
            progress_cb(
                "transcribe",
                TranscribeProgress(
                    position_sec=seg.end,
                    total_sec=duration or seg.end,
                    segments_done=len(segments),
                    elapsed_sec=elapsed,
                    speed_x=speed_x,
                ),
            )
            last_report = now

    total_elapsed = time.monotonic() - t_start
    speed = (duration / total_elapsed) if total_elapsed > 0 and duration > 0 else 0
    log.info(
        f"Транскрипция готова за {total_elapsed:.0f} сек "
        f"(скорость {speed:.1f}x реального времени): "
        f"{len(segments)} сегментов, {len(word_segments)} слов"
    )

    # ВАЖНО: сохраняем кеш ПЕРЕД освобождением модели и CUDA-памяти.
    # Если CUDA при empty_cache() кинет ошибку — мы хотя бы не потеряем
    # 20+ минут транскрипции и сможем подцепить её с кеша при перезапуске.
    full_text = " ".join(s["text"] for s in segments)
    output = {
        "language": info.language,
        "text": full_text,
        "segments": segments,
        "word_segments": word_segments,
        "duration": duration,
        "whisper_model": actual_model,
        "whisper_compute": actual_compute,
    }

    # Атомарная запись: пишем во временный файл потом os.replace.
    # Если процесс умрёт посреди json.dumps — старый кеш не пострадает.
    tmp_cache = cache_file.with_suffix(".json.tmp")
    tmp_cache.write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    import os
    os.replace(tmp_cache, cache_file)
    log.info(
        f"Сохранён транскрипт: {cache_file.name} "
        f"({len(segments)} сегментов, {len(word_segments)} слов)"
    )

    # КРИТИЧНО: НЕ вызываем torch.cuda.empty_cache() и не делаем del model.
    # CUDA SegFault при освобождении модели — access violation, который
    # убивает процесс целиком, минуя Python try/except. Worker всё равно
    # завершится через несколько секунд после возврата — ОС освободит VRAM.
    # Эта проблема воспроизводилась стабильно: worker молча умирал между
    # "Сохранён транскрипт" и следующим этапом в render.py.
    return output


def transcribe_many(
    videos: list[Path],
    cache_dir: Path,
    cfg: dict,
    progress_cb: Callable[[str, "TranscribeProgress | float | None"], None] | None = None,
) -> dict:
    """Транскрибирует несколько видео и сшивает в один транскрипт.

    Тайминги переводятся в ГЛОБАЛЬНУЮ шкалу (offset = сумма длительностей
    предыдущих видео). Каждый segment/word получает поля `source_path` и
    `local_start`/`local_end` — для последующего cut из правильного файла.

    Returns:
        dict с теми же ключами что и transcribe(), плюс:
        - `video_boundaries`: list[{path, offset, duration, label}]
        - segments[*].source_path, local_start, local_end
        - word_segments[*].source_path, local_start, local_end
    """
    if not videos:
        raise ValueError("transcribe_many: пустой список видео")
    if len(videos) == 1:
        # Один файл — оборачиваем результат transcribe() в multi-формат.
        t = transcribe(videos[0], cache_dir, cfg, progress_cb=progress_cb)
        dur = float(t.get("duration") or 0)
        bnd = [{
            "path": str(videos[0]),
            "offset": 0.0,
            "duration": dur,
            "label": videos[0].name,
        }]
        for s in t.get("segments", []):
            s["source_path"] = str(videos[0])
            s["local_start"] = s.get("start", 0.0)
            s["local_end"] = s.get("end", 0.0)
        for w in t.get("word_segments", []):
            w["source_path"] = str(videos[0])
            w["local_start"] = w.get("start", 0.0)
            w["local_end"] = w.get("end", 0.0)
        t["video_boundaries"] = bnd
        return t

    boundaries: list[dict] = []
    merged_segments: list[dict] = []
    merged_words: list[dict] = []
    merged_text_parts: list[str] = []
    offset = 0.0
    language = "ru"

    # КРИТИЧНО: грузим Whisper ОДИН РАЗ, переиспользуем для всех видео.
    # Раньше transcribe() создавала новый WhisperModel внутри → второе
    # видео ловило OOM/CUDA SegFault при попытке выделить VRAM под
    # вторую модель (первая ещё там, GC не освобождает CUDA сразу).
    # Проверяем не все ли видео в кеше — тогда модель не нужна.
    all_cached = all(
        (cache_dir / f"transcript_{file_hash(v)}.json").exists()
        for v in videos
    )
    preloaded = None
    if not all_cached:
        wcfg = cfg["whisper"]
        log.info("Загружаю Whisper один раз для всего батча видео...")
        preloaded = _load_model_with_fallback(
            wcfg["model"], wcfg["device"], wcfg["compute_type"]
        )
        # Сохраняем ссылку чтобы GC не освободил модель после выхода
        # из transcribe_many (это вызывало CUDA access violation).
        _MODEL_LIVE_REFS.append(preloaded[0])

    for idx, video in enumerate(videos, 1):
        log.info(f"=== Видео {idx}/{len(videos)}: {video.name} ===")
        t = transcribe(
            video, cache_dir, cfg,
            progress_cb=progress_cb,
            preloaded_model=preloaded,
        )
        dur = float(t.get("duration") or 0)
        boundaries.append({
            "path": str(video),
            "offset": offset,
            "duration": dur,
            "label": video.name,
        })
        language = t.get("language") or language

        # Сдвигаем все тайминги сегментов на offset
        for s in t.get("segments", []):
            local_start = float(s.get("start", 0.0))
            local_end = float(s.get("end", 0.0))
            merged_segments.append({
                "text": s.get("text", ""),
                "start": round(local_start + offset, 3),
                "end": round(local_end + offset, 3),
                "source_path": str(video),
                "local_start": round(local_start, 3),
                "local_end": round(local_end, 3),
            })
        for w in t.get("word_segments", []):
            local_start = float(w.get("start", 0.0))
            local_end = float(w.get("end", 0.0))
            merged_words.append({
                "word": w.get("word", ""),
                "start": round(local_start + offset, 3),
                "end": round(local_end + offset, 3),
                "score": w.get("score", 0.0),
                "source_path": str(video),
                "local_start": round(local_start, 3),
                "local_end": round(local_end, 3),
            })

        # Текст с заголовком источника — даёт LLM контекст
        merged_text_parts.append(f"=== ВИДЕО {idx}: {video.name} ===")
        merged_text_parts.append(t.get("text", ""))
        offset += dur

    log.info(
        f"Multi-transcribe готов: {len(videos)} видео, общая длительность "
        f"{offset:.0f} сек, {len(merged_segments)} сегментов"
    )

    return {
        "language": language,
        "text": "\n\n".join(merged_text_parts),
        "segments": merged_segments,
        "word_segments": merged_words,
        "duration": offset,
        "video_boundaries": boundaries,
    }


def resolve_source_for_time(
    global_time: float, boundaries: list[dict]
) -> tuple[Path, float]:
    """Маппит глобальную метку времени → (исходный путь, локальное время).

    Используется для cut: знаем segment.start/end в глобальной шкале,
    нужно понять из какого видео резать.
    """
    if not boundaries:
        raise ValueError("resolve_source_for_time: пустые boundaries")
    for b in boundaries:
        off = b["offset"]
        dur = b["duration"]
        if off <= global_time < off + dur:
            return Path(b["path"]), global_time - off
    # За пределами всех — clamp в последний boundary
    last = boundaries[-1]
    return Path(last["path"]), max(0.0, global_time - last["offset"])
