"""AI-narration: LLM пишет пересказ сцены → Edge TTS озвучивает.

Решает "No restriction" claims радикально: оригинальный диалог актёров
полностью заменяется на synthetic narration. Content ID больше не имеет
что матчить ни в голосе, ни в музыке (vocal_isolation + наша narration).

Workflow:
1. write_narration_script(seg, transcript_excerpt) → LLM пишет ~N слов
   уложенных в длительность клипа на темпе 2.5 слова/сек.
2. synthesize(text, voice) → Edge TTS возвращает MP3 + word boundaries.
3. word_segments_from_boundaries(...) → формат для subtitle.build_ass.

Edge TTS бесплатный, без ключа, через Microsoft Speech Service.
Требует интернет (online TTS).
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Iterable

from openai import OpenAI

from .utils import get_logger

log = get_logger("narration")


# Лучшие голоса для русского контента (Edge TTS)
RU_VOICES = {
    "dmitry_male": "ru-RU-DmitryNeural",       # мужской, нейтральный
    "svetlana_female": "ru-RU-SvetlanaNeural", # женская, тёплая
}
# Английские для EN-контента
EN_VOICES = {
    "guy_male": "en-US-GuyNeural",
    "aria_female": "en-US-AriaNeural",
}
ES_VOICES = {
    "alvaro_male": "es-ES-AlvaroNeural",
    "elvira_female": "es-ES-ElviraNeural",
}

ALL_VOICES = {**RU_VOICES, **EN_VOICES, **ES_VOICES}


def pick_voice(language: str, gender: str = "male") -> str:
    """Возвращает Edge TTS voice id по языку и полу."""
    voices = {
        "ru": RU_VOICES,
        "en": EN_VOICES,
        "es": ES_VOICES,
    }.get(language, RU_VOICES)
    if gender == "female":
        for k, v in voices.items():
            if "female" in k:
                return v
    for k, v in voices.items():
        if "male" in k:
            return v
    return next(iter(voices.values()))


def _build_transcript_excerpt(
    transcript_segments: list[dict], clip_start: float, clip_end: float
) -> str:
    """Текст транскрипта в диапазоне клипа."""
    parts: list[str] = []
    for s in transcript_segments:
        ss = float(s.get("start", 0))
        se = float(s.get("end", ss))
        if se >= clip_start and ss <= clip_end:
            txt = str(s.get("text", "")).strip()
            if txt:
                parts.append(txt)
    return " ".join(parts)


def write_narration_script(
    seg_title: str,
    seg_hook: str,
    seg_description: str,
    music_mood: str,
    transcript_segments: list[dict],
    clip_start: float,
    clip_end: float,
    duration_sec: float,
    cfg: dict,
) -> str | None:
    """Через LLM генерит narration text. Возвращает строку или None если все LLM провалились.

    Использует ту же LLM-цепочку что analyze.py: Kimi → Gemini → Groq → OpenRouter → Ollama.
    """
    excerpt = _build_transcript_excerpt(transcript_segments, clip_start, clip_end)
    if not excerpt:
        excerpt = seg_description or seg_hook or seg_title

    # Target слов: ~2.5 слов/сек × длительность клипа, минус 10% safety
    target_words = max(15, int(duration_sec * 2.2))

    prompt_template = Path("prompts/narration_script.md").read_text(encoding="utf-8")
    prompt = prompt_template.format(
        duration_sec=int(duration_sec),
        target_words=target_words,
        title=seg_title,
        hook=seg_hook,
        description=seg_description,
        music_mood=music_mood,
        transcript_excerpt=excerpt[:4000],  # safety cap
    )

    system_msg = (
        "Ты — РАССКАЗЧИК-блогер. Описываешь сцену зрителю от третьего лица. "
        "ЗАПРЕЩЕНО: озвучивать реплики персонажей, копировать фразы из транскрипта, "
        "писать прямой речью, использовать кавычки и тире-диалоги. "
        "РАЗРЕШЕНО: рассказывать ЧТО происходит, ПОЧЕМУ важно, ЧЕМ кончится. "
        "Отвечай ТОЛЬКО narration-текстом одной строкой. Без JSON, без markdown."
    )

    # Анти-копирование: транскрипт-слова которые модель НЕ должна перепечатывать
    # (учитываем только содержательные слова >= 4 букв чтобы не банить "не", "и", "что")
    excerpt_words = {
        w.lower() for w in re.split(r"[^\w]+", excerpt) if len(w) >= 4
    }

    # Универсальный helper для OpenAI-compat провайдеров
    def try_openai_compat(p_key: str, label: str) -> str | None:
        p_cfg = cfg.get(p_key, {})
        if not p_cfg.get("enabled") or not p_cfg.get("api_key"):
            return None
        try:
            headers = {}
            if p_cfg.get("user_agent"):
                headers["User-Agent"] = p_cfg["user_agent"]
            if p_key == "openrouter":
                headers["HTTP-Referer"] = p_cfg.get(
                    "http_referer", "https://localhost"
                )
                headers["X-Title"] = p_cfg.get("app_title", "ShortsFactory")
            client = OpenAI(
                api_key=p_cfg["api_key"],
                base_url=p_cfg.get("base_url"),
                default_headers=headers or None,
            )
            models = [p_cfg.get("model")]
            for m in p_cfg.get("models", []):
                if m not in models:
                    models.append(m)
            if p_cfg.get("fallback_model") and p_cfg["fallback_model"] not in models:
                models.append(p_cfg["fallback_model"])
            for model in [m for m in models if m]:
                try:
                    resp = client.chat.completions.create(
                        model=model,
                        messages=[
                            {"role": "system", "content": system_msg},
                            {"role": "user", "content": prompt},
                        ],
                        temperature=0.4,  # ниже = строже инструкции
                        max_tokens=512,
                    )
                    raw = (resp.choices[0].message.content or "").strip()
                    cleaned = _clean_narration(raw)
                    n_words = len(cleaned.split()) if cleaned else 0

                    if not cleaned or n_words <= 5:
                        log.warning(f"{label} {model}: пустой narration")
                        continue
                    if n_words > target_words * 1.4:
                        log.warning(
                            f"{label} {model}: слишком длинный ({n_words} > {target_words * 1.4:.0f})"
                        )
                        continue

                    # АНТИ-ДУБЛЯЖ: считаем какой % слов narration совпадает
                    # с транскриптом. Если >50% — модель тупо перепечатала реплики.
                    overlap_score = _transcript_overlap(cleaned, excerpt_words)
                    if overlap_score > 0.45:
                        log.warning(
                            f"{label} {model}: narration слишком похож на транскрипт "
                            f"({overlap_score:.0%} overlap) — модель дублирует реплики, скипаю"
                        )
                        continue

                    # АНТИ-ПРЯМАЯ-РЕЧЬ: если кавычки/тире-диалоги — дубляж
                    if _has_direct_speech_markers(cleaned):
                        log.warning(
                            f"{label} {model}: narration содержит прямую речь, скипаю"
                        )
                        continue

                    log.info(
                        f"{label} {model}: narration {n_words} слов, "
                        f"overlap {overlap_score:.0%}"
                    )
                    return cleaned
                except Exception as e:
                    log.warning(f"{label} {model} ошибка narration: {e}")
                    continue
        except Exception as e:
            log.warning(f"{label} init ошибка: {e}")
        return None

    for prov_key, label in (
        ("kimi", "Kimi"),
        ("gemini", "Gemini"),
        ("groq", "Groq"),
        ("openrouter", "OpenRouter"),
    ):
        text = try_openai_compat(prov_key, label)
        if text:
            return text

    # Ollama fallback
    try:
        import ollama
        ollama_cfg = cfg.get("ollama", {})
        cli = ollama.Client(host=ollama_cfg.get("host", "http://localhost:11434"))
        model = ollama_cfg.get("primary_model", "qwen2.5:14b-instruct-q4_K_M")
        log.info(f"Narration через Ollama {model}...")
        resp = cli.chat(
            model=model,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": prompt},
            ],
            options={"temperature": 0.4, "num_predict": 512},
        )
        raw = resp["message"]["content"].strip()
        cleaned = _clean_narration(raw)
        if not cleaned:
            return None
        n_words = len(cleaned.split())
        if n_words <= 5 or n_words > target_words * 1.4:
            log.warning(f"Ollama narration вне таргета ({n_words} слов)")
            return None
        overlap = _transcript_overlap(cleaned, excerpt_words)
        if overlap > 0.45:
            log.warning(
                f"Ollama narration слишком похож на транскрипт ({overlap:.0%}), скипаю"
            )
            return None
        if _has_direct_speech_markers(cleaned):
            log.warning("Ollama narration содержит прямую речь, скипаю")
            return None
        log.info(f"Ollama narration: {n_words} слов, overlap {overlap:.0%}")
        return cleaned
    except Exception as e:
        log.warning(f"Ollama narration ошибка: {e}")

    return None


def _transcript_overlap(narration: str, excerpt_words: set[str]) -> float:
    """Доля содержательных слов narration которые совпадают с транскриптом.

    Считаем только слова >= 4 букв (отсекаем "и", "не", "что", артикли).
    >= 0.5 = модель дублирует реплики, не пересказывает.
    """
    if not excerpt_words:
        return 0.0
    narr_words = [w.lower() for w in re.split(r"[^\w]+", narration) if len(w) >= 4]
    if not narr_words:
        return 0.0
    matched = sum(1 for w in narr_words if w in excerpt_words)
    return matched / len(narr_words)


def _has_direct_speech_markers(text: str) -> bool:
    """True если narration содержит прямую речь персонажей.

    Маркеры: «»-кавычки, обычные "" в начале предложения, тире-диалоги
    в стиле "— Привет, — сказал он.", «—» в начале строки.
    """
    # Тире-диалог в начале (или после переноса)
    if re.search(r"(?:^|\n)\s*[—–-]\s+\w", text):
        return True
    # Прямая речь в кавычках (более 3 слов внутри = это явно реплика)
    quote_patterns = [
        r'"[^"]{15,}"',
        r'«[^»]{15,}»',
        r"'[^']{15,}'",
    ]
    for pat in quote_patterns:
        if re.search(pat, text):
            return True
    # "Х сказал/сказала/говорит/спросил/крикнул" — маркер косвенной озвучки
    speech_verbs = [
        "сказал", "сказала", "говорит", "спрашивает", "спросил",
        "крикнул", "кричит", "отвечает", "ответил", "шепчет",
    ]
    for v in speech_verbs:
        # "Рик сказал:" или ": крикнул Морти"
        if re.search(rf"\b{v}\b\s*[:—]", text, re.IGNORECASE):
            return True
    return False


def _clean_narration(raw: str) -> str:
    """Очищает narration от мета-обёрток: <think>, markdown, кавычки в начале."""
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    raw = re.sub(r"```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```", "", raw)
    raw = raw.strip()
    # Снять кавычки если LLM завернул всё в "..."
    if raw.startswith(('"', '«', "'")) and raw.endswith(('"', '»', "'")):
        raw = raw[1:-1].strip()
    # Убрать префиксы типа "Narration:", "Текст:"
    raw = re.sub(r"^(narration|текст|сценарий|ответ)\s*[:\-—]\s*", "", raw, flags=re.IGNORECASE)
    return raw.strip()


async def _tts_to_mp3(text: str, voice: str, mp3_path: Path) -> dict:
    """Edge TTS → MP3. Возвращает {audio_bytes_count, sentence_boundaries}.

    Edge TTS 7.x больше не отдаёт WordBoundary (только SentenceBoundary),
    поэтому word-level timings получаем отдельно через Whisper в _whisper_align.
    """
    import edge_tts

    communicate = edge_tts.Communicate(text, voice)
    audio_bytes = b""
    sentence_bounds: list[dict] = []

    async for chunk in communicate.stream():
        t = chunk.get("type")
        if t == "audio":
            audio_bytes += chunk["data"]
        elif t == "SentenceBoundary":
            start_sec = chunk["offset"] / 10_000_000.0
            end_sec = start_sec + chunk["duration"] / 10_000_000.0
            sentence_bounds.append({
                "text": chunk["text"],
                "start": round(start_sec, 3),
                "end": round(end_sec, 3),
            })

    mp3_path.write_bytes(audio_bytes)
    return {"bytes": len(audio_bytes), "sentences": sentence_bounds}


def _whisper_align_words(mp3_path: Path, language: str = "ru") -> list[dict]:
    """Прогоняет MP3 через локальный Whisper для word-level timings.

    Используется потому что Edge TTS 7.x больше не отдаёт WordBoundary
    chunks. Whisper-medium даёт точные word timestamps.
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        log.warning("faster_whisper не установлен, sentence-level fallback")
        return []

    try:
        # Маленькая модель для скорости (narration короткий, не нужен medium)
        model = WhisperModel("tiny", device="cuda", compute_type="float16")
        segments, _ = model.transcribe(
            str(mp3_path),
            language=language,
            word_timestamps=True,
            beam_size=1,
        )
        words: list[dict] = []
        for seg in segments:
            if not seg.words:
                continue
            for w in seg.words:
                words.append({
                    "word": w.word.strip(),
                    "start": round(float(w.start), 3),
                    "end": round(float(w.end), 3),
                })
        return words
    except Exception as e:
        log.warning(f"Whisper align упал: {e}")
        return []


def _split_sentences_to_words(sentences: list[dict]) -> list[dict]:
    """Fallback: равномерно распределяет слова внутри SentenceBoundary.

    Используется если Whisper align не сработал. Качество хуже точных
    word-level timings, но всё равно лучше чем sentence-level subs.
    """
    out: list[dict] = []
    for s in sentences:
        text = str(s.get("text", "")).strip()
        if not text:
            continue
        words = [w for w in re.split(r"\s+", text) if w]
        if not words:
            continue
        s_start = float(s["start"])
        s_end = float(s["end"])
        word_dur = (s_end - s_start) / len(words)
        for i, w in enumerate(words):
            w_start = s_start + i * word_dur
            w_end = s_start + (i + 1) * word_dur
            out.append({
                "word": w,
                "start": round(w_start, 3),
                "end": round(w_end, 3),
            })
    return out


def synthesize(
    text: str,
    voice: str,
    out_dir: Path,
    name_prefix: str = "narration",
    language: str = "ru",
) -> tuple[Path, list[dict]] | None:
    """TTS-генерация. Возвращает (mp3_path, word_segments) или None при ошибке.

    word_segments — в формате subtitle.build_ass: [{"word", "start", "end"}].
    Edge TTS 7.x: sentence boundaries → Whisper align (точно) или
    equal split (fallback) для word-level.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    mp3_path = out_dir / f"{name_prefix}.mp3"
    try:
        loop = asyncio.new_event_loop()
        try:
            tts_meta = loop.run_until_complete(_tts_to_mp3(text, voice, mp3_path))
        finally:
            loop.close()
        if not mp3_path.exists() or mp3_path.stat().st_size < 1024:
            log.warning(f"Edge TTS вернул битый MP3: {mp3_path.stat().st_size} байт")
            return None
        log.info(
            f"Edge TTS OK: {mp3_path.name} "
            f"({mp3_path.stat().st_size / 1024:.0f}KB, "
            f"{len(tts_meta['sentences'])} предложений)"
        )

        # Получаем word-level timings: Whisper align > equal split
        words = _whisper_align_words(mp3_path, language=language)
        if words:
            log.info(f"Whisper align: {len(words)} слов с точными timings")
        else:
            words = _split_sentences_to_words(tts_meta["sentences"])
            log.info(f"Fallback equal-split: {len(words)} слов")

        return mp3_path, words
    except Exception as e:
        log.warning(f"Edge TTS упал: {e}")
        return None


def shift_word_timings(words: list[dict], offset_sec: float) -> list[dict]:
    """Сдвигает все word boundary тайминги на offset_sec.

    Используется чтобы привязать narration word timings к timeline клипа.
    """
    return [
        {
            "word": w["word"],
            "start": round(w["start"] + offset_sec, 3),
            "end": round(w["end"] + offset_sec, 3),
        }
        for w in words
    ]
