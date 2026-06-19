"""LLM-генерация SEO-метаданных (title/description/tags) для YouTube Shorts.

Основано на research-отчёте «YouTube Shorts SEO 2026, ниша кино/сериалы»:
- Title: 55-60 симв, формула [Эмодзи][Хук][Название][Год/Сезон]
- Description: конкурентный Shorts-формат: сильная первая строка + тематический
  hashtag-cloud без нерелевантного over-tagging.
- Tags: 8-18 штук, по tier'ам (Tier1 название RU+EN, Tier2 тип сцены,
  Tier3 категория, Tier4 год/жанр), уникальные на видео
- Multilang: ru → русские хэштеги, en → English, es → español
- #shorts ОБЯЗАТЕЛЬНО, в title запрещено (тратит 7 символов даром)
"""

from __future__ import annotations

import json
import re
from typing import Any

import ollama
from openai import OpenAI

from .utils import get_logger
from .youtube_upload import VideoMetadata

log = get_logger("seo")

# ───────────────────────── Banned tokens ─────────────────────────
# Хэштеги-карго-культ. Research: YouTube алгоритм 2026 их игнорирует
# или штрафует за метадата-спам. Strip и из description, и из tags.
_BANNED_HASHTAGS: set[str] = {
    "viral", "trending", "fyp", "fypシ", "foryou", "foryoupage",
    "viralvideo", "viralshorts",
    "shortsvideo", "shortsfeed", "shortsviral",
    "bestmoments", "mustwatch", "epicmoments", "topscenes",
    "best", "top", "trend", "trendy",
}

# Year hashtags тоже считаются спамом
_BANNED_HASHTAG_REGEX = re.compile(
    r"#(?:" + "|".join(re.escape(t) for t in _BANNED_HASHTAGS) +
    r"|20\d{2}|19\d{2})\b",
    re.IGNORECASE,
)

# ───────────────────── Scene emoji by mood ─────────────────────
# music_mood из analyze.py → эмодзи для title (research таблица).
_MOOD_EMOJI: dict[str, str] = {
    "dramatic":      "🔥",
    "hype":          "⚡",
    "sus":           "😱",
    "chill":         "❤️",
    "upbeat":        "😂",
    "inspirational": "⭐",
    "action":        "⚔️",
    "comedy":        "😂",
    "drama":         "💔",
    "epic":          "🏆",
}

_FALLBACK_EMOJI = "🎬"

# ───────────────────── Lang-specific defaults ─────────────────────
# Базовые хэштеги (всегда мьюшим в description если LLM не поставил)
_DEFAULT_HASHTAGS: dict[str, list[str]] = {
    "ru": ["#shorts", "#шортс", "#кино", "#фильм", "#сериал", "#момент"],
    "en": ["#shorts", "#youtubeshorts", "#movie", "#film", "#scene", "#tvseries"],
    "es": ["#shorts", "#pelicula", "#escena", "#serie", "#clip"],
}

# Базовые скрытые tags (если LLM пустой)
_DEFAULT_TAGS: dict[str, list[str]] = {
    "ru": ["момент из фильма", "сцена из сериала", "кино", "фильм", "сериал"],
    "en": ["movie clip", "tv series scene", "best moments",
           "movie scene", "film clip"],
    "es": ["clip de pelicula", "escena de pelicula", "mejores momentos",
           "pelicula", "serie"],
}

_SYSTEM = (
    "Ты — SEO-специалист по YouTube Shorts, специализируешься на нише "
    "«кино, сериалы, мультсериалы». Знаешь алгоритм YouTube Shorts 2026 "
    "(test-and-expand модель: первые 100-500 зрителей решают судьбу видео, "
    "теги определяют какая аудитория увидит первой). "
    "Отвечай СТРОГО JSON-объектом по схеме. Никакого текста до или после."
)


def _detect_language(text: str, fallback: str = "ru") -> str:
    """Простое определение языка по символам.

    Используется когда вызывающая сторона не передала language явно.
    """
    if not text:
        return fallback
    has_cyrillic = bool(re.search(r"[А-Яа-яЁё]", text))
    if has_cyrillic:
        return "ru"
    has_spanish = bool(re.search(
        r"\b(de|que|escena|película|pelicula|serie|momento|épico|epico)\b",
        text, re.IGNORECASE,
    ))
    if has_spanish:
        return "es"
    return "en"


def _build_prompt(
    clip_title: str,
    clip_description: str,
    clip_hook: str,
    clip_tags_hint: list[str],
    music_mood: str,
    source_context: str,
    language: str = "ru",
) -> str:
    keywords_hint = ", ".join(clip_tags_hint) if clip_tags_hint else "(нет)"
    emoji_hint = _MOOD_EMOJI.get(music_mood, _FALLBACK_EMOJI)

    lang_rules = {
        "ru": (
            "ЯЗЫК ВЫХОДА: русский. Описание и title на русском. "
            "Tags: 60% русских + 40% английских (имена брендов, актёров — "
            "на оригинале). Hashtags: #shorts #шортс #youtubeshorts + русские "
            "и английские тематические (#кино #сериал #seriesclips #movieclips)."
        ),
        "en": (
            "OUTPUT LANGUAGE: English. Description and title in English. "
            "Tags: English. Hashtags: #shorts + #movie #film #scene "
            "#tvseries + niche."
        ),
        "es": (
            "IDIOMA DE SALIDA: español. Descripción y título en español. "
            "Tags: en español. Hashtags: #shorts + #pelicula #escena "
            "#serie + nicho."
        ),
    }.get(language, "")

    return f"""Сгенерируй SEO-метаданные для YouTube Shorts (ниша: кино/сериалы/мультсериалы).

ВХОДНЫЕ ДАННЫЕ:
- Тема клипа: {clip_title}
- Что происходит в сцене: {clip_description}
- Сильная фраза/цитата (hook): {clip_hook}
- Подсказка тегов от редактора: {keywords_hint}
- Настроение сцены: {music_mood} (рекомендованный эмодзи: {emoji_hint})
- ИСТОЧНИК (фильм/сериал, год/сезон, персонажи, актёры): {source_context}

КРИТИЧЕСКИ ВАЖНО:
- Не выдумывай название, год, сезон, персонажей, актёров или цитаты. Если точных данных нет — не добавляй их.
- Не делай клип “киношным трейлером”. Нужен короткий, честный YouTube Shorts-hook по факту сцены.
- Вытащи главный удерживающий элемент: конфликт, шутку, неловкость, резкую фразу, поворот, реакцию или мини-развязку.
- Title и description должны обещать именно то, что зритель увидит в клипе. Обманный кликбейт ухудшает досмотр.

{lang_rules}

══════ TITLE (Заголовок) ══════
ФОРМУЛА: [Эмодзи по типу сцены] [Хук/Описание 2-4 слова] [Название фильма/сериала если известно] [Год/Сезон если известен]

ПРАВИЛА:
- 55-60 символов (НЕ больше 60, НЕ меньше 45)
- НАЧИНАЙ с эмодзи (1 шт, не больше)
- Ключевое слово (название фильма/сериала или имя персонажа) — в первой половине title
- Title должен ТОЧНО описывать содержание сцены (не кликбейт!)
- Можно использовать цитату из клипа в кавычках если она цепляющая
- ЗАПРЕЩЕНО в title: #shorts, любые хэштеги, ALL CAPS, "SHOCKING",
  "YOU WON'T BELIEVE", "ТОП-N", "🔥🔥🔥" (множественные эмодзи подряд)
- НЕ обещай того, чего нет в клипе — swipe-away = смерть видео

ШАБЛОНЫ ХОРОШИХ TITLE (не копируй, заполни только фактами из входа):
- ru: «😂 [короткий крючок сцены]. [название] [сезон/год если известен]»
- en: «😱 [short scene hook] | [title] [season/year if known]»
- es: «😢 [gancho corto] | [titulo] [temporada/año si se conoce]»

══════ DESCRIPTION (Описание) ══════
СТРУКТУРА (конкурентный формат Shorts):
- Строка 1: эмоциональный hook по сцене + 6-10 релевантных хэштегов
- Строка 2: краткий контекст — источник/сезон только если есть во входных данных
- Строка 3: мягкий CTA («Подпишись на канал»)
- Строка 4: тематический hashtag-cloud 12-20 штук через пробел

ПРАВИЛА:
- 180-700 символов всего
- Первые 125 символов — preview snippet в поиске. Втисни туда ключевое слово
- #shorts ОБЯЗАТЕЛЬНО, #шортс и #youtubeshorts разрешены
- Хэштеги должны быть связаны с источником, жанром, сценой или нишей кино/сериалов
- Можно использовать RU+EN слой: #русскиесериалы #seriesclips #movieclips #mystery #thriller

🚫 ЗАПРЕЩЁНО В DESCRIPTION:
- #viral, #trending, #fyp, #foryou, #shortsvideo
- #bestmoments, #mustwatch, #epicmoments, #topscenes
- #2025, #2026 (любые годы как хэштеги)
- Ссылки (http*)
- Timestamps
- Все CAPS во всём описании; первая hook-строка может быть капсом

══════ TAGS (скрытые, для поля snippet.tags) ══════
СТРУКТУРА по tier'ам:

Tier 1 (обязательно — название):
- Название фильма/сериала на языке аудитории (точное)
- Название на оригинальном языке (английском) если контент западный

Tier 2 (тип контента):
- "момент из фильма" / "movie clip" / "clip de película"
- "сцена из сериала" / "tv series scene" / "escena de serie"

Tier 3 (категория):
- "кино" / "фильм" / "movie" / "film" / "pelicula"

Tier 4 (конкретика):
- Имя главного персонажа, только если оно есть во входных данных
- Имя актёра если узнаваемый
- Жанр одним словом (action / комедия / drama)
- Год если важен для трендовости (как ОТДЕЛЬНЫЙ тег, НЕ как хэштег)

ПРАВИЛА:
- 8-18 тегов
- Каждый 1-3 слова, ≤30 символов
- Суммарно ≤500 символов
- УНИКАЛЬНЫЕ под этот клип (НЕ универсальный набор)
- БЕЗ дубликатов разных написаний (название + слитное название + аббревиатура = выбери ОДНО)
- БЕЗ хэштег-символа # в тегах (это поле для plain тегов)

🚫 ЗАПРЕЩЁНЫЕ ТЕГИ (НЕ ВСТАВЛЯТЬ):
viral, trending, fyp, foryou, foryoupage, viralvideo,
shortsvideo, shortsfeed, shortsviral,
bestmoments, mustwatch, epicmoments, topscenes, best, top

══════ CATEGORY_ID ══════
"24" Entertainment — ПО УМОЛЧАНИЮ для нарезок фильмов/сериалов
"22" People & Blogs — если контент о реакциях/обзорах
"1"  Film & Animation — для мультфильмов и анимации
"10" Music — только если центр контента — музыка

══════ ФОРМАТ ОТВЕТА (строго JSON, ничего кроме) ══════
{{
  "title": "string (55-60 симв, начинается с 1 эмодзи)",
  "description": "строка с \\n переносами, 150-400 симв, 3-5 хэштегов в конце",
  "tags": ["тег1", "тег2", "тег3", "тег4", "тег5", "тег6"],
  "category_id": "24"
}}
"""


def _build_local_seo_prompt(
    clip_title: str,
    clip_description: str,
    clip_hook: str,
    clip_tags_hint: list[str],
    music_mood: str,
    source_context: str,
    language: str,
) -> str:
    tags_hint = ", ".join(clip_tags_hint) if clip_tags_hint else "(нет)"
    return f"""Сгенерируй SEO JSON для YouTube Shorts по сцене из кино/сериала.

Язык: {language}
Источник: {source_context}
Сцена: {clip_title}
Описание сцены: {clip_description}
Цитата/hook: {clip_hook}
Подсказки тегов: {tags_hint}
Настроение: {music_mood}

Правила:
- Не выдумывай названия, годы, сезоны, персонажей, актеров или цитаты.
- Title должен честно обещать только то, что есть в сцене.
- Description: 1-2 короткие строки по фактам сцены; hashtag-cloud добавит код.
- Tags: 8-12 plain tags без #, связанные с источником, сценой и жанром.
- category_id: "24".

Ответь только JSON:
{{
  "title": "короткий hook + источник/сезон если известен",
  "description": "1-2 строки по фактам сцены",
  "tags": ["tag1", "tag2"],
  "category_id": "24"
}}
"""


def generate_seo(
    cfg: dict,
    clip_title: str,
    clip_description: str,
    clip_hook: str = "",
    clip_tags_hint: list[str] | None = None,
    music_mood: str = "",
    source_context: str = "",
    language: str | None = None,
) -> VideoMetadata:
    """Генерирует SEO-метаданные через доступную LLM. Fallback на эвристику если LLM лёг."""
    clip_tags = clip_tags_hint or []

    # Auto-detect language если не передан
    if not language:
        language = _detect_language(
            f"{clip_title} {clip_description} {source_context}", "ru"
        )

    if cfg.get("seo", {}).get("llm_enabled", True) is False:
        fallback_meta = _fallback_seo(
            clip_title, clip_description, music_mood, language, source_context
        )
        return _apply_metadata_strategy(
            fallback_meta, cfg, music_mood, language, source_context,
            clip_title, clip_description, clip_tags,
        )

    prompt = _build_prompt(
        clip_title=clip_title,
        clip_description=clip_description,
        clip_hook=clip_hook,
        clip_tags_hint=clip_tags,
        music_mood=music_mood,
        source_context=source_context,
        language=language,
    )

    # 1. Kimi (если включён и есть ключ)
    kimi_cfg = cfg.get("kimi", {})
    if kimi_cfg.get("enabled") and kimi_cfg.get("api_key"):
        default_headers = {}
        if kimi_cfg.get("user_agent"):
            default_headers["User-Agent"] = kimi_cfg["user_agent"]
        try:
            client = OpenAI(
                api_key=kimi_cfg["api_key"],
                base_url=kimi_cfg.get("base_url", "https://api.moonshot.cn/v1"),
                default_headers=default_headers or None,
            )
            resp = client.chat.completions.create(
                model=kimi_cfg.get("model", "moonshot-v1-32k"),
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.5,
                max_tokens=4096,
                response_format={"type": "json_object"},
            )
            raw = resp.choices[0].message.content or ""
            data = _parse_json(raw)
            if data:
                meta = _build_metadata_if_grounded(
                    data, music_mood, language, source_context,
                    clip_title, clip_description, clip_tags, "Kimi", cfg,
                )
                if meta:
                    return meta
                log.warning("Kimi SEO ответ отклонён, пробую следующий SEO-провайдер")
            else:
                log.warning("Kimi вернул невалидный JSON, пробую следующий SEO-провайдер")
        except Exception as e:
            log.warning(f"Kimi SEO ошибка ({_sanitize_error(e)}), пробую следующий SEO-провайдер")
    else:
        log.info("Kimi выключен")

    # 1.5. Gemini / Groq (OpenAI-compat, free tier) — между Kimi и OpenRouter
    for prov_key, prov_label in (("gemini", "Gemini"), ("groq", "Groq")):
        p_cfg = cfg.get(prov_key, {})
        if not p_cfg.get("enabled") or not p_cfg.get("api_key"):
            continue
        p_models = [p_cfg["model"]]
        if p_cfg.get("fallback_model"):
            p_models.append(p_cfg["fallback_model"])
        try:
            p_client = OpenAI(
                api_key=p_cfg["api_key"],
                base_url=p_cfg["base_url"],
            )
        except Exception as e:
            log.warning(f"{prov_label} SEO client init упал: {_sanitize_error(e)}")
            continue
        for p_model in p_models:
            try:
                p_resp = p_client.chat.completions.create(
                    model=p_model,
                    messages=[
                        {"role": "system", "content": _SYSTEM},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.5,
                    max_tokens=4096,
                    response_format={"type": "json_object"},
                )
                p_raw = p_resp.choices[0].message.content or ""
                p_data = _parse_json(p_raw)
                if p_data:
                    meta = _build_metadata_if_grounded(
                        p_data, music_mood, language, source_context,
                        clip_title, clip_description, clip_tags, prov_label, cfg,
                    )
                    if meta:
                        return meta
                    continue
                log.warning(f"{prov_label} SEO {p_model}: невалидный JSON")
            except Exception as e:
                log.warning(f"{prov_label} SEO {p_model} ошибка: {_sanitize_error(e)}")
                continue

    # 2. OpenRouter free модели (цепочка)
    or_cfg = cfg.get("openrouter", {})
    if or_cfg.get("enabled") and or_cfg.get("api_key"):
        or_models = list(or_cfg.get("models") or [])
        if not or_models:
            if or_cfg.get("model"):
                or_models.append(or_cfg["model"])
            if or_cfg.get("fallback_model"):
                or_models.append(or_cfg["fallback_model"])

        or_client = OpenAI(
            api_key=or_cfg["api_key"],
            base_url=or_cfg.get("base_url", "https://openrouter.ai/api/v1"),
            default_headers={
                "HTTP-Referer": or_cfg.get("http_referer", "https://localhost"),
                "X-Title": or_cfg.get("app_title", "ShortsFactory"),
            },
        )
        for or_model in or_models:
            try:
                or_resp = or_client.chat.completions.create(
                    model=or_model,
                    messages=[
                        {"role": "system", "content": _SYSTEM},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.5,
                    max_tokens=4096,
                    response_format={"type": "json_object"},
                )
                or_raw = or_resp.choices[0].message.content or ""
                or_data = _parse_json(or_raw)
                if or_data:
                    meta = _build_metadata_if_grounded(
                        or_data, music_mood, language, source_context,
                        clip_title, clip_description, clip_tags, "OpenRouter", cfg,
                    )
                    if meta:
                        return meta
                    continue
                log.warning(f"OpenRouter SEO {or_model} вернул невалидный JSON")
            except Exception as e:
                log.warning(f"OpenRouter SEO {or_model} ошибка: {_sanitize_error(e)}")
                continue

    # 3. Локальная Ollama (основной путь для полностью локального режима)
    local_prompt = _build_local_seo_prompt(
        clip_title=clip_title,
        clip_description=clip_description,
        clip_hook=clip_hook,
        clip_tags_hint=clip_tags,
        music_mood=music_mood,
        source_context=source_context,
        language=language,
    )
    local_meta = _generate_seo_ollama(
        cfg, local_prompt, music_mood, language, source_context,
        clip_title, clip_description, clip_tags,
    )
    if local_meta:
        return local_meta

    # 4. Эвристический fallback
    fallback_meta = _fallback_seo(
        clip_title, clip_description, music_mood, language, source_context
    )
    return _apply_metadata_strategy(
        fallback_meta, cfg, music_mood, language, source_context,
        clip_title, clip_description, clip_tags,
    )


def _parse_json(raw: str) -> dict | None:
    raw = re.sub(r"```json\s*\n?", "", raw)
    raw = re.sub(r"\n?```\s*$", "", raw).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
    return None


def _sanitize_error(value: object) -> str:
    text = str(value)
    text = re.sub(r"sk-[A-Za-z0-9_-]+", "sk-***", text)
    text = re.sub(r"sk-or-v1-[A-Za-z0-9]+", "sk-or-v1-***", text)
    text = re.sub(r"user_[A-Za-z0-9]+", "user_***", text)
    text = re.sub(r"'user_id':\s*'[^']+'", "'user_id': '***'", text)
    return text


def _installed_ollama_models(cli: ollama.Client) -> set[str] | None:
    try:
        resp = cli.list()
    except Exception as e:
        log.warning(f"Не удалось получить список моделей Ollama для SEO: {_sanitize_error(e)}")
        return None

    raw_models = resp.get("models", []) if isinstance(resp, dict) else getattr(resp, "models", [])
    names: set[str] = set()
    for item in raw_models:
        if isinstance(item, dict):
            name = item.get("model") or item.get("name")
        else:
            name = getattr(item, "model", None) or getattr(item, "name", None)
        if name:
            names.add(str(name))
    return names


def _generate_seo_ollama(
    cfg: dict,
    prompt: str,
    music_mood: str,
    language: str,
    source_context: str,
    clip_title: str,
    clip_description: str,
    clip_tags_hint: list[str],
) -> VideoMetadata | None:
    o_cfg = cfg.get("ollama", {})
    models = [model for model in (o_cfg.get("primary_model"),) if model]
    if o_cfg.get("seo_use_fallback_model", False) and o_cfg.get("fallback_model"):
        models.append(o_cfg["fallback_model"])
    if not models:
        return None

    cli = ollama.Client(
        host=o_cfg.get("host", "http://localhost:11434"),
        timeout=float(o_cfg.get("seo_timeout_sec", 90)),
    )
    installed_models = _installed_ollama_models(cli)
    options = {
        "temperature": float(o_cfg.get("seo_temperature", o_cfg.get("temperature", 0.35))),
        "num_predict": int(o_cfg.get("seo_num_predict", 2048)),
        "num_ctx": int(o_cfg.get("seo_num_ctx", 8192)),
    }

    for model in models:
        if installed_models is not None and model not in installed_models:
            log.warning(f"Ollama SEO модель не установлена: {model}")
            continue
        try:
            log.info(f"Ollama SEO: пробую {model}")
            resp = cli.chat(
                model=model,
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                options=options,
                format="json",
            )
            raw = resp["message"]["content"] if isinstance(resp, dict) else resp.message.content
            data = _parse_json(raw or "")
            if data:
                meta = _build_metadata_if_grounded(
                    data, music_mood, language, source_context,
                    clip_title, clip_description, clip_tags_hint, "Ollama", cfg,
                )
                if meta:
                    return meta
                continue
            log.warning(f"Ollama SEO {model} вернул невалидный JSON")
        except Exception as e:
            log.warning(f"Ollama SEO {model} ошибка: {_sanitize_error(e)}")
            continue
    return None


def _strip_banned_hashtags(text: str) -> str:
    """Удаляет банный хэштеги из строки. Сжимает лишние пробелы."""
    cleaned = _BANNED_HASHTAG_REGEX.sub("", text)
    # Сжимаем 2+ пробела в один, чистим пробелы вокруг \n
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r" *\n *", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _is_banned_tag(tag: str) -> bool:
    t = re.sub(r"[#\s]", "", tag.lower())
    if t in _BANNED_HASHTAGS:
        return True
    if re.fullmatch(r"(19|20)\d{2}", t):
        return True
    return False


def _ensure_emoji_in_title(title: str, music_mood: str) -> str:
    """Если title не начинается с эмодзи — добавляет по mood."""
    # Простая heuristic: эмодзи = U+1F300-U+1FAFF, U+2600-U+27BF
    starts_with_emoji = bool(re.match(
        r"^[\U0001F300-\U0001FAFF☀-➿✀-➿]", title
    ))
    if starts_with_emoji:
        return title
    emoji = _MOOD_EMOJI.get(music_mood, _FALLBACK_EMOJI)
    return f"{emoji} {title}"


def _ensure_hashtags(description: str, language: str, tags: list[str]) -> str:
    """Гарантирует 3-5 хэштегов в конце description.

    Если LLM не поставил — добавляем дефолтные lang-specific.
    Если поставил, но без #shorts — добавляем.
    """
    desc = description.rstrip()
    existing = re.findall(r"#[\w]+", desc, re.UNICODE)
    existing_lower = {h.lower() for h in existing}

    # #shorts обязательно
    if "#shorts" not in existing_lower and "#Shorts" not in existing:
        existing.append("#shorts")

    # Доводим до 3 хэштегов из lang defaults + tags
    if len(existing) < 3:
        defaults = _DEFAULT_HASHTAGS.get(language, _DEFAULT_HASHTAGS["en"])
        for h in defaults:
            if h.lower() not in existing_lower:
                existing.append(h)
                existing_lower.add(h.lower())
            if len(existing) >= 4:
                break

    # Если в description ещё нет блока хэштегов — добавим
    has_hash_line = "#" in desc.split("\n")[-1] if desc else False
    if not has_hash_line:
        desc = desc + "\n\n" + " ".join(existing[:5])
    return desc


_RU_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

_MOOD_HASHTAGS: dict[str, list[str]] = {
    "comedy": ["комедия", "юмор", "смешно", "russiancomedy", "funny"],
    "upbeat": ["комедия", "юмор", "энергия", "funny", "seriesclips"],
    "sus": ["мистика", "mystery", "thriller", "напряжение", "атмосфера"],
    "dramatic": ["драма", "dramascenes", "напряжение", "сильнаясцена"],
    "drama": ["драма", "dramascenes", "сильнаясцена"],
    "hype": ["экшн", "actionscene", "напряжение", "динамика"],
    "action": ["экшн", "actionscene", "драка", "динамика"],
    "epic": ["эпично", "epicscene", "кульминация"],
    "chill": ["диалог", "атмосфера", "сцена"],
    "inspirational": ["мотивация", "сильнаясцена", "inspiration"],
}

_BROAD_HASHTAGS_RU = [
    "сериал", "кино", "русскиесериалы", "сценыизсериалов",
    "нарезки", "лучшиемоменты", "атмосфера", "seriesclips", "movieclips",
]
_BROAD_HASHTAGS_EN = ["seriesclips", "movieclips", "dramascenes", "scene", "shorts"]
_SHORT_HASHTAGS_RU = ["shorts", "шортс", "youtubeshorts"]
_SHORT_HASHTAGS_EN = ["shorts", "youtubeshorts"]
_GROWTH_HASHTAGS = ["viralshorts", "trendingshorts"]
_FILM_ONLY_TAGS = {"момент из фильма", "фильм", "film clip"}


def _translit_ru(text: str) -> str:
    out: list[str] = []
    for ch in text.lower().replace("ё", "е"):
        out.append(_RU_TRANSLIT.get(ch, ch))
    return "".join(out)


def _extract_source_title(source_context: str) -> str:
    text = source_context.strip()
    if not text:
        return ""
    quoted = re.search(r"[\"«](.+?)[\"»]", text)
    if quoted:
        return quoted.group(1).strip()
    cleaned = re.sub(
        r"\b(сериал|фильм|мультсериал|мультфильм|series|movie|film)\b",
        "",
        text,
        flags=re.IGNORECASE,
    )
    cleaned = re.split(
        r",|\||\b\d+\s*(?:сезон|season)\b|\b(?:19|20)\d{2}\b",
        cleaned,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    return cleaned.strip(" :—-")


def _source_acronym(source_title: str) -> str:
    words = re.findall(r"[A-Za-zА-Яа-яЁё]{2,}", source_title)
    if not 2 <= len(words) <= 5:
        return ""
    return "".join(word[0] for word in words).lower().replace("ё", "е")


def _clean_hashtag_value(value: str, allow_growth: bool = False) -> str:
    cleaned = value.strip().lstrip("#").lower().replace("ё", "е")
    cleaned = re.sub(r"[\s_]+", "", cleaned)
    cleaned = re.sub(r"[^0-9a-zа-я]", "", cleaned, flags=re.IGNORECASE)
    growth_allowed = allow_growth and cleaned in _GROWTH_HASHTAGS
    if len(cleaned) < 2 or cleaned.isdigit() or (_is_banned_tag(cleaned) and not growth_allowed):
        return ""
    return cleaned


def _hashtag(value: str, allow_growth: bool = False) -> str:
    cleaned = _clean_hashtag_value(value, allow_growth)
    return f"#{cleaned}" if cleaned else ""


def _dedupe_hashtags(candidates: list[str], limit: int, allow_growth: bool = False) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        tag = item if item.startswith("#") else _hashtag(item, allow_growth)
        if not tag:
            continue
        if item.startswith("#") and not _clean_hashtag_value(item, allow_growth):
            continue
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(tag)
        if len(out) >= limit:
            break
    return out


def _clip_hashtag_candidates(tags: list[str], clip_title: str, clip_description: str) -> list[str]:
    candidates = list(tags)
    for text in (clip_title, clip_description):
        for word in re.findall(r"[A-Za-zА-Яа-яЁё]{4,}", text):
            if len(candidates) >= 10:
                break
            candidates.append(word)
    return candidates


def _build_hashtag_cloud(
    cfg: dict,
    language: str,
    music_mood: str,
    source_context: str,
    clip_title: str,
    clip_description: str,
    tags: list[str],
) -> list[str]:
    seo_cfg = cfg.get("seo", {})
    max_count = int(seo_cfg.get("max_description_hashtags", 22))
    source_title = _extract_source_title(source_context)
    candidates: list[str] = []

    if source_title:
        candidates.append(source_title)
        translit = _translit_ru(source_title)
        if translit and translit != source_title.lower():
            candidates.append(translit)
        acronym = _source_acronym(source_title)
        if acronym:
            candidates.append(acronym)

    is_series = bool(re.search(r"\b(сериал|series|season|сезон)\b", source_context, re.IGNORECASE))
    filtered_tags = [
        tag for tag in tags
        if not (is_series and str(tag).strip().lower().replace("ё", "е") in _FILM_ONLY_TAGS)
    ]
    candidates.extend(_clip_hashtag_candidates(filtered_tags, clip_title, clip_description))
    candidates.extend(_MOOD_HASHTAGS.get(music_mood, []))

    if seo_cfg.get("include_youtube_shorts_hashtag", True):
        candidates.extend(_SHORT_HASHTAGS_RU if language == "ru" else _SHORT_HASHTAGS_EN)
    elif seo_cfg.get("include_russian_shorts_hashtag", True) and language == "ru":
        candidates.extend(["shorts", "шортс"])
    else:
        candidates.append("shorts")

    candidates.extend(_BROAD_HASHTAGS_RU if language == "ru" else _BROAD_HASHTAGS_EN)

    allow_growth = bool(seo_cfg.get("include_growth_hashtags", False))
    if allow_growth:
        candidates.extend(_GROWTH_HASHTAGS)

    return _dedupe_hashtags(candidates, max_count, allow_growth)


def _strip_description_hashtags(description: str) -> list[str]:
    no_hashtags = re.sub(r"#[\wа-яА-ЯёЁ]+", "", description, flags=re.UNICODE)
    no_hashtags = _strip_banned_hashtags(no_hashtags)
    lines = []
    for line in no_hashtags.splitlines():
        line = re.sub(r"\s+", " ", line).strip(" -—")
        if line:
            lines.append(line)
    return lines


def _strip_leading_emoji(text: str) -> str:
    return re.sub(r"^[\U0001F300-\U0001FAFF☀-➿✀-➿]\s*", "", text).strip()


def _trim_words(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].strip()
    return cut or text[:limit].strip()


def _opening_hook(
    meta: VideoMetadata,
    cfg: dict,
    music_mood: str,
    language: str,
    clip_title: str,
    clip_description: str,
) -> str:
    hook = _strip_leading_emoji(clip_title or meta.title or clip_description)
    if not hook:
        hook = _strip_leading_emoji(meta.title)
    hook = _trim_words(hook, 48)
    if cfg.get("seo", {}).get("uppercase_opening_hook", True) and language == "ru":
        hook = hook.upper()
    if not re.search(r"[.!?…]$", hook):
        hook = hook + "…"
    emoji = _MOOD_EMOJI.get(music_mood, _FALLBACK_EMOJI)
    if emoji not in hook:
        hook = f"{hook}{emoji}"
    return hook


def _plain_hidden_tag(value: str) -> str:
    value = value.strip().lstrip("#")
    value = value.replace('"', "").replace("«", "").replace("»", "")
    value = re.sub(r"[,;:|]", " ", value)
    value = re.sub(r"[_]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    if not value or len(value) > 30 or _is_banned_tag(value):
        return ""
    return value


def _expand_hidden_tags(
    meta: VideoMetadata,
    cfg: dict,
    language: str,
    source_context: str,
    hashtags: list[str],
) -> list[str]:
    max_tags = int(cfg.get("seo", {}).get("max_hidden_tags", 18))
    source_title = _extract_source_title(source_context)
    is_series = bool(re.search(r"\b(сериал|series|season|сезон)\b", source_context, re.IGNORECASE))
    candidates: list[str] = list(meta.tags)
    if source_title:
        candidates.extend([source_title, _translit_ru(source_title), _source_acronym(source_title)])
    candidates.extend(tag.lstrip("#") for tag in hashtags)
    candidates.extend(_DEFAULT_TAGS.get(language, _DEFAULT_TAGS["en"]))
    if language == "ru":
        candidates.extend(["русский сериал", "сцены из сериалов", "series clips", "movie clips"])

    out: list[str] = []
    seen: set[str] = set()
    total = 0
    for candidate in candidates:
        if is_series and str(candidate).strip().lower().replace("ё", "е") in _FILM_ONLY_TAGS:
            continue
        tag = _plain_hidden_tag(str(candidate))
        key = re.sub(r"\W", "", tag.lower())
        if not tag or key in seen:
            continue
        cost = len(tag) + (2 if " " in tag else 0) + (1 if out else 0)
        if total + cost > 500:
            break
        seen.add(key)
        out.append(tag)
        total += cost
        if len(out) >= max_tags:
            break
    return out


def _first_line_hashtags(hashtags: list[str], count: int) -> list[str]:
    required_names = {"#shorts", "#шортс", "#youtubeshorts"}
    required = [tag for tag in hashtags if tag.lower() in required_names]
    regular = [tag for tag in hashtags if tag.lower() not in required_names]
    room_for_regular = max(0, count - len(required))
    return (regular[:room_for_regular] + required)[:count]


def _apply_metadata_strategy(
    meta: VideoMetadata,
    cfg: dict,
    music_mood: str,
    language: str,
    source_context: str,
    clip_title: str,
    clip_description: str,
    clip_tags_hint: list[str],
) -> VideoMetadata:
    seo_cfg = cfg.get("seo", {})
    if not seo_cfg.get("enabled", True):
        return meta
    if seo_cfg.get("description_style", "competitor_balanced") != "competitor_balanced":
        return meta

    hashtags = _build_hashtag_cloud(
        cfg, language, music_mood, source_context,
        clip_title, clip_description, list(meta.tags) + clip_tags_hint,
    )
    if not hashtags:
        return meta

    first_count = int(seo_cfg.get("first_line_hashtags", 9))
    first_line_tags = _first_line_hashtags(hashtags, max(3, first_count))
    body_lines = _strip_description_hashtags(meta.description)
    context = body_lines[0] if body_lines else (clip_description or source_context)
    context = _trim_words(context, 180)
    cta = str(seo_cfg.get("cta", "")).strip()

    lines = [
        f"{_opening_hook(meta, cfg, music_mood, language, clip_title, clip_description)} "
        + " ".join(first_line_tags)
    ]
    if context:
        lines.append(context)
    if cta:
        lines.append(cta)
    lines.append(" ".join(hashtags))

    return VideoMetadata(
        title=meta.title,
        description="\n".join(lines).strip(),
        tags=_expand_hidden_tags(meta, cfg, language, source_context, hashtags),
        category_id=meta.category_id,
        default_language=meta.default_language,
        default_audio_language=meta.default_audio_language,
        privacy_status=meta.privacy_status,
        self_declared_made_for_kids=meta.self_declared_made_for_kids,
        notify_subscribers=meta.notify_subscribers,
        publish_at=meta.publish_at,
    )


_GENERIC_GROUNDING_TERMS = {
    "сериал", "сериала", "серия", "сезон", "фильм", "фильма", "кино", "клип",
    "сцена", "момент", "год", "года", "актер", "актеры", "актёр", "актёры",
    "персонаж", "персонажи", "комедия", "драма", "школа", "класс",
    "series", "season", "movie", "film", "clip", "scene", "episode", "year",
    "actor", "actors", "character", "characters", "comedy", "drama",
}


def _terms(text: str) -> set[str]:
    spaced = re.sub(r"(?<=[a-zа-яё])(?=[A-ZА-ЯЁ])", " ", text)
    normalized = f"{text} {spaced}".lower().replace("ё", "е")
    raw_terms = set(re.findall(r"[a-zа-я0-9]{3,}", normalized, re.IGNORECASE))
    return {
        term for term in raw_terms
        if term not in _GENERIC_GROUNDING_TERMS
        and not re.fullmatch(r"\d+", term)
    }


def _metadata_text(data: dict[str, Any]) -> str:
    raw_tags = data.get("tags", [])
    if isinstance(raw_tags, list):
        tags_text = " ".join(str(tag) for tag in raw_tags)
    else:
        tags_text = str(raw_tags or "")
    return " ".join([
        str(data.get("title", "")),
        str(data.get("description", "")),
        tags_text,
    ])


def _has_placeholder_artifacts(text: str) -> bool:
    return bool(re.search(
        r"\?{3,}|\[[^\]]*(?:год|назван|сезон|title|year|season|name|titulo)[^\]]*\]",
        text,
        re.IGNORECASE,
    ))


def _metadata_is_grounded(
    data: dict[str, Any],
    source_context: str,
    clip_title: str,
    clip_description: str,
    clip_tags_hint: list[str],
) -> tuple[bool, str]:
    output_text = _metadata_text(data)
    if _has_placeholder_artifacts(output_text):
        return False, "содержит плейсхолдеры вместо фактов"

    output_terms = _terms(output_text)
    source_terms = _terms(source_context)
    if source_terms and not (output_terms & source_terms):
        return False, "не использует название/детали источника из входных данных"

    scene_terms = _terms(" ".join([clip_title, clip_description, *clip_tags_hint]))
    if scene_terms and not (output_terms & scene_terms) and not source_terms:
        return False, "не связан с описанием сцены из входных данных"

    return True, ""


def _build_metadata_if_grounded(
    data: dict[str, Any],
    music_mood: str,
    language: str,
    source_context: str,
    clip_title: str,
    clip_description: str,
    clip_tags_hint: list[str],
    provider_label: str,
    cfg: dict | None = None,
) -> VideoMetadata | None:
    is_grounded, reason = _metadata_is_grounded(
        data, source_context, clip_title, clip_description, clip_tags_hint
    )
    if not is_grounded:
        log.warning(f"{provider_label} SEO ответ отклонён: {reason}")
        return None
    meta = _build_metadata(data, music_mood, language)
    return _apply_metadata_strategy(
        meta, cfg or {}, music_mood, language, source_context,
        clip_title, clip_description, clip_tags_hint,
    )


def _build_metadata(
    data: dict[str, Any], music_mood: str, language: str
) -> VideoMetadata:
    title = str(data.get("title", "Untitled Short")).strip()
    description = str(data.get("description", "")).strip()
    raw_tags = data.get("tags", [])
    if isinstance(raw_tags, str):
        raw_tags = [t.strip() for t in re.split(r"[,;]", raw_tags) if t.strip()]
    tags_in = [str(t).strip().lstrip("#") for t in raw_tags if t]
    category_id = str(data.get("category_id", "24"))

    # 1. Strip banned hashtags из description
    description = _strip_banned_hashtags(description)

    # 2. Strip banned теги, удаляем дубликаты
    seen: set[str] = set()
    clean_tags: list[str] = []
    for t in tags_in:
        if _is_banned_tag(t):
            continue
        key = re.sub(r"\W", "", t.lower())
        if not key or key in seen:
            continue
        seen.add(key)
        clean_tags.append(t)
    # Если совсем пусто — берём дефолтные lang
    if not clean_tags:
        clean_tags = list(_DEFAULT_TAGS.get(language, _DEFAULT_TAGS["en"]))
    # Обрезаем до 8
    tags = clean_tags[:8]

    # 3. Title: эмодзи + clamp 60
    title = _ensure_emoji_in_title(title, music_mood)
    if len(title) > 60:
        title = title[:60].rstrip()

    # 4. Hashtags в description — гарантия наличия
    description = _ensure_hashtags(description, language, tags)

    return VideoMetadata(
        title=title,
        description=description,
        tags=tags,
        category_id=category_id,
        default_language=language,
        default_audio_language=language,
    )


def _fallback_seo(
    clip_title: str, clip_description: str, music_mood: str,
    language: str, source_context: str = "",
) -> VideoMetadata:
    """Локальный fallback если LLM недоступен. Без banned tokens.

    Простой шаблон: эмодзи + title, описание на основе clip_description,
    хэштеги из lang defaults, теги из source_context + defaults.
    """
    emoji = _MOOD_EMOJI.get(music_mood, _FALLBACK_EMOJI)
    base_title = clip_title.strip() or "Sцена"
    title = f"{emoji} {base_title}"[:60].rstrip()

    desc_parts: list[str] = []
    if clip_description:
        desc_parts.append(clip_description.strip()[:250])
    elif clip_title:
        desc_parts.append(clip_title.strip()[:250])
    if source_context:
        desc_parts.append(source_context.strip()[:150])

    hashtags = " ".join(_DEFAULT_HASHTAGS.get(language, _DEFAULT_HASHTAGS["en"])[:4])

    description = "\n".join(desc_parts) + "\n\n" + hashtags
    description = _strip_banned_hashtags(description)

    # Tags: из source_context + lang defaults
    tags: list[str] = []
    is_series = bool(re.search(r"\b(сериал|series|season|сезон)\b", source_context, re.IGNORECASE))
    if source_context:
        source_title = _extract_source_title(source_context)
        if source_title and not _is_banned_tag(source_title):
            tags.append(source_title[:30])
        # Берём 2 слова-ключа из source_context (название фильма)
        words = [w.strip() for w in re.split(r"[,—\-:|]", source_context) if w.strip()]
        for w in words[:2]:
            cleaned = w.strip()[:30]
            if cleaned and cleaned not in tags and not _is_banned_tag(cleaned):
                tags.append(cleaned)
    for t in _DEFAULT_TAGS.get(language, _DEFAULT_TAGS["en"]):
        if is_series and t.strip().lower().replace("ё", "е") in _FILM_ONLY_TAGS:
            continue
        if len(tags) >= 6:
            break
        if t not in tags:
            tags.append(t)

    return VideoMetadata(
        title=title or "YouTube Short",
        description=description,
        tags=tags[:8],
        category_id="24",  # Entertainment по умолч. для нарезок
        default_language=language,
        default_audio_language=language,
    )
