"""Анализ транскрипта через Kimi AI / Ollama: выбор лучших моментов."""

import json
import re
import time
from pathlib import Path

import ollama
from openai import OpenAI
from pydantic import BaseModel, Field, ValidationError

from .intro_outro_detector import (
    adjust_clip_borders,
    detect_intro_end,
    detect_outro_start,
)
from .utils import get_logger

log = get_logger("analyze")

_DIAGNOSTICS_KEY = "_analysis_diagnostics"

_SYSTEM_MSG = (
    "Ты — редактор YouTube Shorts. Отвечай ТОЛЬКО валидным JSON-объектом "
    "вида {\"clips\": [...]}. "
    "Каждый элемент массива clips — объект с числовыми полями start и end (секунды), "
    "а также строковыми полями title, hook, description, music_mood, reason и массивом tags. "
    "Никакого текста кроме JSON."
)


class Segment(BaseModel):
    start: float
    end: float
    title: str = Field(max_length=80)
    hook: str = ""
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    music_mood: str = "dramatic"
    reason: str = ""


def _sanitize_error(value: object) -> str:
    text = str(value)
    text = re.sub(r"sk-[A-Za-z0-9_-]+", "sk-***", text)
    text = re.sub(r"sk-or-v1-[A-Za-z0-9]+", "sk-or-v1-***", text)
    text = re.sub(r"user_[A-Za-z0-9]+", "user_***", text)
    text = re.sub(r"'user_id':\s*'[^']+'", "'user_id': '***'", text)
    return text


def _diag(cfg: dict, provider: str, model: str | None, message: str) -> None:
    entry = f"{provider}"
    if model:
        entry += f" {model}"
    entry += f": {message}"
    cfg.setdefault(_DIAGNOSTICS_KEY, []).append(entry)


def _debug_dir(cfg: dict) -> Path | None:
    raw = cfg.get("_debug_dir")
    if not raw:
        return None
    try:
        path = Path(raw)
        path.mkdir(parents=True, exist_ok=True)
        return path
    except OSError as e:
        log.warning(f"Не удалось создать debug-папку LLM: {_sanitize_error(e)}")
        return None


def _safe_slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")[:90] or "model"


def _save_raw_response(cfg: dict, provider: str, model: str, raw: str) -> None:
    directory = _debug_dir(cfg)
    if directory is None:
        return
    stamp = int(time.time() * 1000)
    file_name = f"llm_{stamp}_{_safe_slug(provider)}_{_safe_slug(model)}.json.txt"
    path = directory / file_name
    try:
        path.write_text(raw, encoding="utf-8")
        log.info(f"Сырой ответ LLM сохранён: {path}")
    except OSError as e:
        log.warning(f"Не удалось сохранить сырой ответ LLM: {_sanitize_error(e)}")


def describe_llm_chain(cfg: dict) -> str:
    labels: list[str] = []
    for key, label in (
        ("kimi", "Kimi"),
        ("gemini", "Gemini"),
        ("groq", "Groq"),
        ("openrouter", "OpenRouter"),
    ):
        layer = cfg.get(key, {})
        if layer.get("enabled") and layer.get("api_key"):
            labels.append(label)

    ollama_cfg = cfg.get("ollama", {})
    if ollama_cfg.get("primary_model") or ollama_cfg.get("fallback_model"):
        labels.append("Ollama")

    return " → ".join(labels) if labels else "LLM"


def _format_transcript(segments: list[dict]) -> tuple[str, str, int]:
    """Формат: [MM:SS] текст реплики. Возвращает (текст, длительность_строка, секунды)."""
    lines = []
    for s in segments:
        start = int(s["start"])
        lines.append(f"[{start}s] {s['text'].strip()}")
    total_sec = int(segments[-1]["end"]) if segments else 0
    total_mm, total_ss = divmod(total_sec, 60)
    duration_str = f"{total_mm} мин {total_ss} сек ({total_sec} секунд)"
    return "\n".join(lines), duration_str, total_sec


def _repair_kimi_mojibake(text: str) -> str:
    """Восстанавливает двойную mojibake-кодировку из Kimi response.

    Kimi-for-coding иногда возвращает текст где UTF-8 cyrillic был
    интерпретирован как Latin-1 и снова encoded в UTF-8. Признак:
    последовательности "Р?" / "Р+" в большом количестве.

    Возвращает исходную строку если mojibake не обнаружено или repair
    не сработал.
    """
    if not text:
        return text
    # Quick check: "РњР" / "РІ" / "РѕР" — типичные mojibake-маркеры.
    # Также нет смысла repair'ить ASCII-only.
    sample = text[:2000]
    if not any(s in sample for s in ("РњР", "РІ", "РѕР", "Р°", "Сѓ", "Р±", "С‚Р")):
        return text
    # Mojibake возникает когда UTF-8 cyrillic bytes интерпретируются как cp1251.
    # Recovery: encode("cp1251") → decode("utf-8") вернёт исходный текст.
    # Сначала cp1251 (типичный Windows mojibake), потом latin-1 как fallback.
    for enc in ("cp1251", "latin-1"):
        try:
            repaired = text.encode(enc, errors="strict").decode("utf-8", errors="strict")
        except (UnicodeDecodeError, UnicodeEncodeError, LookupError):
            continue
        # Sanity: repaired должен содержать реальную кириллицу
        if re.search(r"[А-Яа-яЁё]{3,}", repaired):
            return repaired
    return text


def _analyze_kimi(prompt: str, cfg: dict, desired_count: int) -> list[Segment] | None:
    """Анализ через Kimi AI (OpenAI-совместимый API).

    Поддерживает как обычный Moonshot API, так и Kimi For Coding (по подписке) —
    последний требует кастомный User-Agent кодинг-агента, иначе 403.
    """
    kimi_cfg = cfg.get("kimi", {})
    if not kimi_cfg.get("enabled") or not kimi_cfg.get("api_key"):
        return None

    default_headers = {}
    user_agent = kimi_cfg.get("user_agent")
    if user_agent:
        default_headers["User-Agent"] = user_agent

    client = OpenAI(
        api_key=kimi_cfg["api_key"],
        base_url=kimi_cfg.get("base_url", "https://api.moonshot.cn/v1"),
        default_headers=default_headers or None,
    )
    model = kimi_cfg.get("model", "moonshot-v1-32k")

    # Некоторые Kimi-модели (например kimi-for-coding, kimi-k2-*) принимают
    # только temperature=1. Детектим по имени и не передаём temperature вообще
    # либо передаём 1.0, чтобы избежать "invalid temperature: only 1 is allowed".
    _kimi_temp_locked = (
        model.startswith("kimi-for-")
        or model.startswith("kimi-k2")
        or model.startswith("kimi-k3")
        or "for-coding" in model
    )

    for attempt in range(2):
        log.info(f"Анализ через Kimi {model} (попытка {attempt + 1})...")
        try:
            # max_tokens: reasoning-модели (K2.7 Code) тратят бюджет на
            # размышления; кфг override позволяет поднять до 32k-65k.
            max_toks = int(kimi_cfg.get("max_tokens", 16384))
            kwargs = {
                "model": model,
                "messages": [
                    {"role": "system", "content": _SYSTEM_MSG},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": max_toks,
                "response_format": {"type": "json_object"},
            }
            if _kimi_temp_locked:
                kwargs["temperature"] = 1.0
            else:
                kwargs["temperature"] = 0.3 + attempt * 0.15
            resp = client.chat.completions.create(**kwargs)
            msg = resp.choices[0].message
            raw_orig = msg.content or ""
            # Debug: если content пустой, посмотрим reasoning_content
            # (kimi-k2 reasoning модели). Логируем его длину чтобы понять
            # куда ушёл бюджет.
            reasoning_text = None
            for attr in ("reasoning_content", "reasoning", "thinking"):
                val = getattr(msg, attr, None)
                if val:
                    reasoning_text = str(val)
                    break
            if not raw_orig and reasoning_text:
                log.warning(
                    f"Kimi {model}: content=0, reasoning_content={len(reasoning_text)} симв "
                    f"(max_tokens={max_toks} мало). Первые 300: {reasoning_text[:300]}"
                )
                # Попробуем извлечь JSON из reasoning (модель могла туда положить ответ)
                from_reasoning = _extract_balanced_json(reasoning_text)
                if from_reasoning:
                    log.info("Kimi: JSON найден в reasoning_content, использую его")
                    raw_orig = from_reasoning
            # Kimi-Coding иногда возвращает текст с двойной mojibake:
            # UTF-8 cyrillic → latin-1 интерпретация → re-encode в UTF-8.
            # Маркер: содержит много "Р?" последовательностей.
            # Recover: encode("latin-1").decode("utf-8") вернёт исходный
            # cyrillic. Делаем БЕЗ записи переменной resp (resp ниже не используется).
            repaired = _repair_kimi_mojibake(raw_orig)
            if repaired is not raw_orig:
                log.info("Kimi response mojibake detected, repaired")
                # Подменяем content внутри resp вручную нельзя — но дальше
                # код использует resp.choices[0].message.content, перепишем
                # через прямое присвоение.
                try:
                    resp.choices[0].message.content = repaired
                except Exception:
                    pass
            raw = resp.choices[0].message.content or ""
            _save_raw_response(cfg, "Kimi", model, raw)
            # Usage debug: куда ушёл бюджет (prompt/completion/reasoning)
            try:
                u = resp.usage
                if u is not None:
                    completion_det = getattr(u, "completion_tokens_details", None)
                    reasoning_tok = (
                        getattr(completion_det, "reasoning_tokens", 0)
                        if completion_det else 0
                    )
                    log.info(
                        f"Kimi usage: prompt={u.prompt_tokens} "
                        f"completion={u.completion_tokens} reasoning={reasoning_tok} "
                        f"total={u.total_tokens}"
                    )
            except Exception:
                pass
            log.info(f"Ответ Kimi: {len(raw)} символов")
            log.info(f"Первые 500 символов: {raw[:500]}")

            target_dur = cfg["shorts"]["target_duration_sec"]
            min_dur = cfg["shorts"]["min_duration_sec"]
            segments = _parse_response(raw, target_dur, min_dur)
            if segments:
                log.info(f"Получено {len(segments)} сегментов от Kimi")
                if len(segments) >= desired_count or attempt > 0:
                    return segments[:desired_count]
                log.warning(f"Мало сегментов ({len(segments)}), повторяю")
                continue
            log.warning("Kimi вернул невалидный ответ")
            _diag(cfg, "Kimi", model, "invalid JSON or no clip objects")
        except Exception as e:
            err = _sanitize_error(e)
            err_lower = err.lower()
            # Авто-retry для temperature lock-in: Kimi может поменять policy
            # и эта модель больше не принимает кастомный temperature.
            if (
                "invalid temperature" in err_lower
                or "only 1 is allowed" in err_lower
            ) and not _kimi_temp_locked:
                log.warning(
                    f"Kimi {model} требует temperature=1, ретраю с temperature=1.0"
                )
                _kimi_temp_locked = True
                try:
                    resp = client.chat.completions.create(
                        model=model,
                        messages=[
                            {"role": "system", "content": _SYSTEM_MSG},
                            {"role": "user", "content": prompt},
                        ],
                        temperature=1.0,
                        max_tokens=16384,
                        response_format={"type": "json_object"},
                    )
                    raw = resp.choices[0].message.content or ""
                    _save_raw_response(cfg, "Kimi", model, raw)
                    target_dur = cfg["shorts"]["target_duration_sec"]
                    min_dur = cfg["shorts"]["min_duration_sec"]
                    segments = _parse_response(raw, target_dur, min_dur)
                    if segments:
                        log.info(
                            f"Kimi retry OK: {len(segments)} сегментов (temperature=1)"
                        )
                        return segments[:desired_count]
                except Exception as e2:
                    log.warning(f"Kimi retry с temperature=1 тоже упал: {e2}")
            log.warning(f"Ошибка Kimi: {err}")
            _diag(cfg, "Kimi", model, err)
            break
    return None


def _analyze_openai_compat(
    prompt: str,
    cfg: dict,
    desired_count: int,
    provider_key: str,
    provider_label: str,
) -> list[Segment] | None:
    """Универсальный вызов OpenAI-совместимого API (Gemini/Groq/etc).

    Читает cfg[provider_key] с полями: enabled, api_key, base_url, model,
    fallback_model (опционально).
    """
    p_cfg = cfg.get(provider_key, {})
    if not p_cfg.get("enabled") or not p_cfg.get("api_key"):
        return None

    client = OpenAI(
        api_key=p_cfg["api_key"],
        base_url=p_cfg["base_url"],
    )
    models = [p_cfg["model"]]
    if p_cfg.get("fallback_model") and p_cfg["fallback_model"] not in models:
        models.append(p_cfg["fallback_model"])

    min_ok = max(3, (desired_count * 2) // 3)
    best_segments: list[Segment] = []

    for model in models:
        log.info(f"Анализ через {provider_label} {model}...")
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _SYSTEM_MSG},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=16384,
                response_format={"type": "json_object"},
            )
            raw = resp.choices[0].message.content or ""
            _save_raw_response(cfg, provider_label, model, raw)
            log.info(f"Ответ {provider_label} ({model}): {len(raw)} символов")
            log.info(f"Первые 500 символов: {raw[:500]}")

            target_dur = cfg["shorts"]["target_duration_sec"]
            min_dur = cfg["shorts"]["min_duration_sec"]
            segments = _parse_response(raw, target_dur, min_dur)
            if not segments:
                if _looks_like_garbage(raw):
                    msg = "looped/garbage response"
                    log.warning(f"{provider_label} {model}: мусор, пропускаю")
                else:
                    msg = "invalid JSON or no clip objects"
                    log.warning(f"{provider_label} {model}: невалидный JSON")
                _diag(cfg, provider_label, model, msg)
                continue

            log.info(f"Получено {len(segments)} сегментов от {provider_label} {model}")
            if len(segments) > len(best_segments):
                best_segments = segments
            if len(segments) >= min_ok:
                return segments[:desired_count]
        except Exception as e:
            err = _sanitize_error(e)
            log.warning(f"Ошибка {provider_label} {model}: {err}")
            _diag(cfg, provider_label, model, err)
            continue

    return best_segments[:desired_count] if best_segments else None


def _analyze_gemini(prompt: str, cfg: dict, desired_count: int) -> list[Segment] | None:
    return _analyze_openai_compat(prompt, cfg, desired_count, "gemini", "Gemini")


def _analyze_groq(prompt: str, cfg: dict, desired_count: int) -> list[Segment] | None:
    return _analyze_openai_compat(prompt, cfg, desired_count, "groq", "Groq")


def _analyze_openrouter(prompt: str, cfg: dict, desired_count: int) -> list[Segment] | None:
    """Анализ через OpenRouter (цепочка настроенных free/paid моделей).

    Между Kimi/Gemini/Groq и Ollama. В текущем локальном режиме выключен конфигом.
    """
    or_cfg = cfg.get("openrouter", {})
    if not or_cfg.get("enabled") or not or_cfg.get("api_key"):
        return None

    default_headers = {
        "HTTP-Referer": or_cfg.get("http_referer", "https://localhost"),
        "X-Title": or_cfg.get("app_title", "ShortsFactory"),
    }

    client = OpenAI(
        api_key=or_cfg["api_key"],
        base_url=or_cfg.get("base_url", "https://openrouter.ai/api/v1"),
        default_headers=default_headers,
    )

    # Список моделей: новый формат models[], старый model+fallback_model для совместимости
    models = list(or_cfg.get("models") or [])
    if not models:
        primary = or_cfg.get("model")
        fallback = or_cfg.get("fallback_model")
        if primary:
            models.append(primary)
        if fallback and fallback not in models:
            models.append(fallback)
    if not models:
        log.warning("OpenRouter: список моделей пуст")
        return None

    # min_ok — если получили хотя бы столько сегментов, считаем результат
    # "достаточно хорошим" и возвращаем сразу. Иначе пробуем следующую модель,
    # но запоминаем лучший результат (max сегментов) на случай если все слабые.
    min_ok = max(3, (desired_count * 2) // 3)
    best_segments: list[Segment] = []

    for model in models:
        log.info(f"Анализ через OpenRouter {model}...")
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _SYSTEM_MSG},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=16384,
                response_format={"type": "json_object"},
            )
            raw = resp.choices[0].message.content or ""
            _save_raw_response(cfg, "OpenRouter", model, raw)
            log.info(f"Ответ OpenRouter ({model}): {len(raw)} символов")
            log.info(f"Первые 500 символов: {raw[:500]}")

            target_dur = cfg["shorts"]["target_duration_sec"]
            min_dur = cfg["shorts"]["min_duration_sec"]
            segments = _parse_response(raw, target_dur, min_dur)
            if not segments:
                if _looks_like_garbage(raw):
                    msg = "looped/garbage response"
                    log.warning(f"OpenRouter {model} вернул мусор, перехожу к следующей")
                else:
                    msg = "invalid JSON or no clip objects"
                    log.warning(f"OpenRouter {model} вернул невалидный JSON, перехожу к следующей")
                _diag(cfg, "OpenRouter", model, msg)
                continue

            log.info(f"Получено {len(segments)} сегментов от OpenRouter {model}")
            if len(segments) > len(best_segments):
                best_segments = segments
            if len(segments) >= min_ok:
                return segments[:desired_count]
            log.warning(
                f"Мало сегментов ({len(segments)} < {min_ok}), пробую следующую модель"
            )
        except Exception as e:
            # 402/429 → лимиты, 401 → битый ключ, любая ошибка — следующая модель
            err = _sanitize_error(e)
            log.warning(f"Ошибка OpenRouter {model}: {err}")
            _diag(cfg, "OpenRouter", model, err)
            continue

    if best_segments:
        log.info(
            f"OpenRouter: лучший результат {len(best_segments)} сегментов "
            f"(хотели {desired_count}), возвращаю"
        )
        return best_segments[:desired_count]
    return None


def _looks_like_garbage(raw: str) -> bool:
    """Детектит галлюцинации с повторяющимся текстом.

    LLM на длинном контексте иногда зацикливается: повторяет одну фразу
    десятки раз. Нет смысла парсить такой ответ — сразу скипаем модель.
    """
    if not raw or len(raw) < 50:
        return True

    # A valid JSON object can legitimately repeat keys like start/end/title.
    # Keep this detector conservative; callers parse first and ask this only
    # when parsing failed or produced no clips.
    cleaned = re.sub(r"\s+", "", raw)
    balanced = _extract_balanced_json(cleaned)
    if balanced:
        try:
            json.loads(balanced)
            return False
        except json.JSONDecodeError:
            pass

    for chunk_len in range(4, 81):
        max_i = max(0, len(cleaned) - chunk_len * 4)
        for i in range(max_i):
            chunk = cleaned[i : i + chunk_len]
            if len(set(chunk)) < 3:
                continue
            repeats = 1
            pos = i + chunk_len
            while cleaned[pos : pos + chunk_len] == chunk:
                repeats += 1
                pos += chunk_len
                if repeats >= (8 if chunk_len <= 6 else 4):
                    return True

    suspicious_tokens = ('"}"]}', '"]"}', '"":""]')
    if any(cleaned.count(token) >= 12 for token in suspicious_tokens):
        return True
    return False


def _installed_ollama_models(cli: ollama.Client, cfg: dict) -> set[str] | None:
    try:
        resp = cli.list()
    except Exception as e:
        err = _sanitize_error(e)
        log.warning(f"Не удалось получить список моделей Ollama: {err}")
        _diag(cfg, "Ollama", None, f"cannot list local models: {err}")
        return None

    raw_models = resp.get("models", []) if isinstance(resp, dict) else getattr(resp, "models", [])
    names: set[str] = set()
    for item in raw_models:
        if isinstance(item, dict):
            name = item.get("name") or item.get("model")
        else:
            name = getattr(item, "name", None) or getattr(item, "model", None)
        if name:
            names.add(str(name))
    return names


def _ollama_analysis_option_attempts(cfg: dict) -> list[dict]:
    o_cfg = cfg.get("ollama", {})
    base_ctx = int(o_cfg.get("num_ctx", 8192))
    base_predict = int(o_cfg.get("num_predict", 2048))
    retry_predict = int(o_cfg.get("analysis_retry_num_predict", min(base_predict, 1024)))
    raw_retry_contexts = o_cfg.get("analysis_retry_num_ctx", [4096])
    if isinstance(raw_retry_contexts, int):
        retry_contexts = [raw_retry_contexts]
    else:
        retry_contexts = [int(v) for v in (raw_retry_contexts or [])]

    contexts: list[int] = []
    for ctx in [base_ctx, *retry_contexts]:
        if ctx > 0 and ctx not in contexts:
            contexts.append(ctx)

    attempts: list[dict] = []
    for i, ctx in enumerate(contexts):
        attempts.append({
            "temperature": float(o_cfg.get("temperature", 0.25)),
            "num_predict": base_predict if i == 0 else min(base_predict, retry_predict),
            "num_ctx": ctx,
        })
    return attempts


def _is_ollama_resource_error(message: str) -> bool:
    lowered = message.lower()
    needles = (
        "cuda",
        "out of memory",
        "not enough memory",
        "llama-server process has terminated",
        "failed to load model",
        "status code: 500",
    )
    return any(needle in lowered for needle in needles)


def _is_ollama_mojibake_path_error(message: str) -> bool:
    """Detect Ollama startup with broken (non-ASCII) OLLAMA_MODELS path.

    Symptom: error contains user-profile-style path with mojibake chars
    (cp1251 cyrillic interpreted as utf-8). Example:
        failed to load model from C:\\Users\\������ ����\\.ollama\\...
    Means ollama serve was started WITHOUT proper OLLAMA_MODELS env and
    is reading from the broken default user profile path.
    """
    # cp1251 кириллица как UTF-8 даёт "?" символы или characters � (U+FFFD)
    # Также реальный backslash-Users без mojibake signal'а не интересен
    if "failed to load model" not in message.lower():
        return False
    if "\\users\\" not in message.lower():
        return False
    # Маркеры mojibake: replacement char, череда "?" в пути, нестандартные
    # байты (вне ASCII печатных + ascii cyrillic)
    if "�" in message:
        return True
    if "?????" in message:
        return True
    # Бинарные байты-как-знаки-?-в-пути
    # cp1251 после reinterpret как latin-1 даёт "Ð" "Â" "ï¿½"
    suspicious = ("ï¿½", "Ð", "Â", "ÿ", "Ô")
    if any(s in message for s in suspicious):
        return True
    return False


def _try_restart_ollama_via_script() -> bool:
    """Пробует автоматически перезапустить ollama с правильным OLLAMA_MODELS.

    Запускает scripts/start_ollama_for_factory.ps1 в subprocess. Returns
    True если скрипт отработал без ошибок (это НЕ гарантирует что ollama
    стартовал — просто что наш PowerShell helper не упал).
    """
    import subprocess as _sp
    from pathlib import Path as _P
    script = _P(__file__).resolve().parents[1] / "scripts" / "start_ollama_for_factory.ps1"
    if not script.exists():
        log.warning(f"start_ollama_for_factory.ps1 не найден ({script}), recovery skip")
        return False
    try:
        log.info(f"🔁 Автоматически перезапускаю Ollama через {script.name}...")
        r = _sp.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(script)],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode == 0:
            log.info("✅ Ollama restart скрипт отработал OK")
            return True
        log.warning(f"Ollama restart скрипт упал: {r.stderr[:200]}")
        return False
    except (_sp.SubprocessError, OSError) as e:
        log.warning(f"Не удалось запустить ollama restart скрипт: {e}")
        return False


def _ollama_preflight(cli: ollama.Client, model: str) -> tuple[bool, str | None]:
    """Pre-flight: проверка что Ollama сервер видит модель.

    Returns (ok, error_message). Если ok=False с mojibake-сообщением,
    можно попробовать auto-restart перед chat.
    """
    try:
        cli.show(model)
        return True, None
    except Exception as e:
        err = _sanitize_error(e)
        return False, err


def _analyze_ollama(prompt: str, cfg: dict, desired_count: int) -> list[Segment] | None:
    """Analyze through local Ollama and retry with lighter options on GPU errors."""
    cli = ollama.Client(host=cfg["ollama"]["host"])
    raw_chat = cli.chat

    def chat_with_retries(**kwargs):
        base_options = dict(kwargs.get("options") or {})
        attempts = [base_options]
        for retry_options in _ollama_analysis_option_attempts(cfg)[1:]:
            if retry_options not in attempts:
                attempts.append(dict(retry_options))
        last_error: Exception | None = None
        for i, attempt_options in enumerate(attempts):
            if i:
                log.warning(
                    f"Ollama {kwargs.get('model')}: retry num_ctx={attempt_options['num_ctx']} "
                    f"num_predict={attempt_options['num_predict']}"
                )
            kwargs["options"] = attempt_options
            try:
                return raw_chat(**kwargs)
            except Exception as exc:
                last_error = exc
                err = _sanitize_error(exc)
                has_retry = i + 1 < len(attempts)
                if has_retry and _is_ollama_resource_error(err):
                    next_options = attempts[i + 1]
                    _diag(
                        cfg,
                        "Ollama",
                        str(kwargs.get("model") or ""),
                        (
                            f"{err}; retrying with num_ctx={next_options['num_ctx']} "
                            f"num_predict={next_options['num_predict']}"
                        ),
                    )
                    continue
                raise
        if last_error:
            raise last_error
        raise RuntimeError("Ollama did not run any analysis attempts")

    cli.chat = chat_with_retries
    models = [cfg["ollama"]["primary_model"]]
    if cfg["ollama"].get("analysis_use_fallback_model", False) and cfg["ollama"].get("fallback_model"):
        models.append(cfg["ollama"]["fallback_model"])
    installed_models = _installed_ollama_models(cli, cfg)
    min_ok = max(3, (desired_count * 2) // 3)
    best_segments: list[Segment] = []

    # Pre-flight: проверяем что сервер видит хотя бы первую модель.
    # Если падает с mojibake — один раз пробуем авто-restart через скрипт.
    mojibake_recovered = False
    for model in models:
        if installed_models is not None and model not in installed_models:
            msg = f"model is not installed locally; run `ollama pull {model}`"
            log.warning(f"Ollama {model}: {msg}")
            _diag(cfg, "Ollama", model, msg)
            continue

        preflight_ok, preflight_err = _ollama_preflight(cli, model)
        if not preflight_ok and _is_ollama_mojibake_path_error(preflight_err or ""):
            if not mojibake_recovered:
                log.warning(
                    "❌ Ollama serve запущен с битым OLLAMA_MODELS (mojibake path), "
                    "пробую автоматический перезапуск..."
                )
                mojibake_recovered = True
                if _try_restart_ollama_via_script():
                    # После перезапуска — снова проверяем
                    cli = ollama.Client(host=cfg["ollama"]["host"])
                    cli.chat = chat_with_retries
                    installed_models = _installed_ollama_models(cli, cfg)
                    preflight_ok, preflight_err = _ollama_preflight(cli, model)

            if not preflight_ok:
                actionable = (
                    "Ollama serve запущен БЕЗ правильного OLLAMA_MODELS env "
                    "(пути с кириллицей не читаются). "
                    "Закрой ollama app.exe (трей) и запусти: "
                    "powershell -ExecutionPolicy Bypass -File "
                    "C:\\shorts-factory\\scripts\\start_ollama_for_factory.ps1"
                )
                log.warning(f"❌ {actionable}")
                _diag(cfg, "Ollama", model, actionable)
                break  # дальше Ollama не имеет смысла

        log.info(f"Анализ через {model}...")
        try:
            options = {
                "temperature": float(cfg["ollama"].get("temperature", 0.25)),
                "num_predict": int(cfg["ollama"].get("num_predict", 8192)),
                "num_ctx": int(cfg["ollama"].get("num_ctx", 32768)),
            }
            resp = cli.chat(
                model=model,
                messages=[
                    {"role": "system", "content": _SYSTEM_MSG},
                    {"role": "user", "content": prompt},
                ],
                options=options,
                format="json",  # форсим JSON-режим Ollama
                keep_alive=cfg["ollama"].get("keep_alive", "2m"),
            )
            raw = resp["message"]["content"]
            _save_raw_response(cfg, "Ollama", model, raw)

            log.info(f"Ответ {model}: {len(raw)} символов")
            log.info(f"Первые 500 символов: {raw[:500]}")

            target_dur = cfg["shorts"]["target_duration_sec"]
            min_dur = cfg["shorts"]["min_duration_sec"]
            segments = _parse_response(raw, target_dur, min_dur)
            if not segments:
                if _looks_like_garbage(raw):
                    msg = "looped/garbage response"
                    log.warning(f"{model} вернул зацикленный/мусорный ответ, скипаю модель")
                else:
                    msg = "invalid JSON or no clip objects"
                    log.warning(f"{model} вернул пустой/невалидный JSON, fallback")
                _diag(cfg, "Ollama", model, msg)
                continue

            log.info(
                f"Получено {len(segments)} сегментов от {model} "
                f"(запрошено {desired_count})"
            )
            if len(segments) > len(best_segments):
                best_segments = segments
            if len(segments) >= min_ok:
                return segments[:desired_count]
            log.warning(
                f"{model} дал мало ({len(segments)} < {min_ok}), пробую fallback"
            )
        except Exception as e:
            err = _sanitize_error(e)
            # Mojibake path detection — пробуем auto-restart ВНУТРИ chat
            # exception (preflight мог пройти если cli.show работал, а chat — нет).
            if _is_ollama_mojibake_path_error(err):
                if not mojibake_recovered:
                    log.warning(
                        "❌ Ollama mojibake path внутри chat. "
                        "Пробую автоматический перезапуск..."
                    )
                    mojibake_recovered = True
                    if _try_restart_ollama_via_script():
                        # Пересоздаём cli с новым сервером
                        cli = ollama.Client(host=cfg["ollama"]["host"])
                        cli.chat = chat_with_retries
                        installed_models = _installed_ollama_models(cli, cfg)
                        # Повторяем chat ещё один раз
                        try:
                            log.info(
                                f"🔁 Повторяю chat после restart Ollama для {model}..."
                            )
                            resp = cli.chat(
                                model=model,
                                messages=[
                                    {"role": "system", "content": _SYSTEM_MSG},
                                    {"role": "user", "content": prompt},
                                ],
                                options=options,
                                format="json",
                                keep_alive=cfg["ollama"].get("keep_alive", "2m"),
                            )
                            raw = resp["message"]["content"]
                            _save_raw_response(cfg, "Ollama", model, raw)
                            log.info(f"Ответ после restart {model}: {len(raw)} символов")
                            target_dur = cfg["shorts"]["target_duration_sec"]
                            min_dur = cfg["shorts"]["min_duration_sec"]
                            segments = _parse_response(raw, target_dur, min_dur)
                            if segments:
                                if len(segments) > len(best_segments):
                                    best_segments = segments
                                if len(segments) >= min_ok:
                                    return segments[:desired_count]
                            continue
                        except Exception as e_retry:
                            err_retry = _sanitize_error(e_retry)
                            log.warning(
                                f"После restart Ollama chat снова упал: {err_retry}"
                            )
                            err = err_retry  # fall through к actionable error
                # Auto-restart не помог или уже пробовали
                actionable = (
                    f"Ollama serve запущен БЕЗ правильного OLLAMA_MODELS env. "
                    f"Auto-restart не помог. Скорее всего ollama app.exe "
                    f"(трей-иконка) держит старый процесс. "
                    f"ВРУЧНУЮ: закрой трей-иконку Ollama и запусти: "
                    f"powershell -ExecutionPolicy Bypass -File "
                    f"C:\\shorts-factory\\scripts\\start_ollama_for_factory.ps1"
                )
                log.warning(f"❌ Ollama mojibake path: {actionable}")
                _diag(cfg, "Ollama", model, actionable)
                break
            log.warning(f"Ошибка {model}: {err}")
            _diag(cfg, "Ollama", model, err)
            continue

    if best_segments:
        log.info(
            f"Ollama: лучший результат {len(best_segments)} сегментов, возвращаю"
        )
        return best_segments[:desired_count]
    return None


def _count_words_in_range(
    transcript_segments: list[dict], start: float, end: float
) -> int:
    """Считает слова в транскрипте в диапазоне [start, end]."""
    total = 0
    for s in transcript_segments:
        s_start = float(s.get("start", 0))
        s_end = float(s.get("end", s_start))
        # Сегмент пересекается с [start, end]
        if s_end >= start and s_start <= end:
            text = str(s.get("text", "")).strip()
            # Считаем "слова" грубо: токены длиной >= 2 буквы
            total += sum(1 for w in text.split() if len(w) >= 2)
    return total


def _hook_overlap_score(
    seg: "Segment",
    transcript_segments: list[dict],
    window_before: float = 0.5,
    window_after: float = 2.5,
) -> float | None:
    """Считает долю слов hook'а присутствующих в transcript в первые сек клипа.

    None если hook пустой. >= 0.35 норма. < 0.35 = LLM придумал hook.
    """
    hook = (getattr(seg, "hook", "") or "").lower().strip()
    if not hook or len(hook) < 4:
        return None
    hook_words = {w for w in re.split(r"[^\wа-яА-ЯёЁ]+", hook) if len(w) >= 3}
    if not hook_words:
        return None
    lo = seg.start - window_before
    hi = seg.start + window_after
    transcript_text = " ".join(
        str(s.get("text", "")).lower()
        for s in transcript_segments
        if s.get("end", 0) >= lo and s.get("start", 0) <= hi
    )
    found = sum(1 for w in hook_words if w in transcript_text)
    return found / len(hook_words)


def _filter_and_adjust_segments(
    segments: list[Segment],
    transcript_segments: list[dict],
    total_seconds: int,
    intro_end: float,
    outro_start: float,
    cfg: dict,
) -> list[Segment]:
    """Сдвигает границы клипов за пределы заставки и отбраковывает silence-зоны.

    Стратегия:
    - Клип целиком в intro/outro → отбраковать
    - Клип частично перекрывает intro/outro → сдвинуть start/end
    - Клип без диалога (Content ID match риск) → отбраковать
    - Клип с пустыми первыми 3 сек → отбраковать (хук слабый + intro-fill риск)
    """
    safety = cfg.get("safety", {})
    shorts = cfg.get("shorts", {})
    min_words = int(safety.get("min_dialogue_words", 8))
    min_first_3 = int(safety.get("min_first_3sec_words", 2))
    min_dur = float(shorts.get("min_duration_sec", 15))
    target_dur = float(shorts.get("target_duration_sec", 35))
    max_dur = float(shorts.get("max_duration_sec", 60))

    safe: list[Segment] = []
    rejected: list[tuple[Segment, str]] = []

    for seg in segments:
        # 1) Adjust borders — сдвигаем за пределы intro/outro если перекрытие
        adjusted = adjust_clip_borders(
            seg.start, seg.end,
            intro_end, outro_start,
            min_dur, target_dur, max_dur,
        )
        if adjusted is None:
            rejected.append((
                seg,
                f"целиком в заставке/титрах (intro≤{intro_end:.0f}s, outro≥{outro_start:.0f}s)"
            ))
            continue
        new_start, new_end = adjusted
        if (new_start, new_end) != (seg.start, seg.end):
            log.info(
                f"✂️  Клип '{seg.title[:40]}' сдвинут: "
                f"[{seg.start:.0f}s..{seg.end:.0f}s] → [{new_start:.0f}s..{new_end:.0f}s] "
                "(избегаем заставку)"
            )
            # Создаём новый Segment с обновлёнными границами
            seg = Segment(**{**seg.model_dump(), "start": new_start, "end": new_end})

        # 2) Диалог пустой/слабый — Content ID будет матчить оригинальный score
        total_words = _count_words_in_range(transcript_segments, seg.start, seg.end)
        if total_words < min_words:
            rejected.append((seg, f"мало слов ({total_words} < {min_words}) — риск Content ID на оригинальный score"))
            continue

        # 3) Первые 3 сек без диалога (хук слабый + intro-fill риск)
        first_words = _count_words_in_range(transcript_segments, seg.start, seg.start + 3.0)
        if first_words < min_first_3:
            rejected.append((seg, f"первые 3 сек без речи ({first_words} < {min_first_3})"))
            continue

        # 4) Hook fact-check: hook должен реально звучать в первые 2.5с клипа
        if safety.get("hook_verify_enabled", True):
            hook_overlap = _hook_overlap_score(
                seg, transcript_segments,
                window_before=0.5, window_after=2.5,
            )
            min_hook_overlap = float(safety.get("hook_min_overlap", 0.35))
            if hook_overlap is not None and hook_overlap < min_hook_overlap:
                rejected.append((
                    seg,
                    f"hook не звучит в первые сек ({hook_overlap:.0%} overlap < "
                    f"{min_hook_overlap:.0%}) — LLM придумал hook"
                ))
                continue

        safe.append(seg)

    for seg, reason in rejected:
        log.warning(
            f"⛔ Клип [start={seg.start:.0f}s, end={seg.end:.0f}s] '{seg.title[:40]}' "
            f"отбракован: {reason}"
        )
    if safe:
        log.info(f"✅ Safety-фильтр: {len(safe)}/{len(segments)} клипов прошли")
        # Диагностика распределения по видео — поможем заметить кучность
        if len(safe) >= 2 and total_seconds > 0:
            sorted_safe = sorted(safe, key=lambda s: s.start)
            first_start = sorted_safe[0].start
            last_start = sorted_safe[-1].start
            spread_pct = (last_start - first_start) / max(total_seconds, 1) * 100
            avg_gap = (
                (last_start - first_start) / (len(sorted_safe) - 1)
                if len(sorted_safe) > 1 else 0
            )
            log.info(
                f"📊 Распределение клипов: spread {spread_pct:.0f}% от длины видео, "
                f"средний gap {avg_gap:.0f}s, первый={first_start:.0f}s, "
                f"последний={last_start:.0f}s, видео={total_seconds}s"
            )
            if spread_pct < 50:
                log.warning(
                    "⚠️  Кучность: клипы покрывают <50% видео. "
                    "LLM возможно взял подряд из одной зоны — рассмотри ретрай "
                    "промпта или укажи в analyze_segments.md что нужно распределение."
                )
    return safe


def _clip_time_bounds(transcript_segments: list[dict]) -> tuple[float, float]:
    if not transcript_segments:
        return 0.0, 0.0
    start = min(float(s.get("start", 0.0)) for s in transcript_segments)
    end = max(float(s.get("end", s.get("start", 0.0))) for s in transcript_segments)
    return start, end


def _offset_transcript_segments(
    transcript_segments: list[dict], time_offset: float
) -> list[dict]:
    if abs(time_offset) < 0.001:
        return transcript_segments

    shifted: list[dict] = []
    for seg in transcript_segments:
        item = dict(seg)
        start = max(0.0, float(item.get("start", 0.0)) - time_offset)
        end = max(start, float(item.get("end", start)) - time_offset)
        item["start"] = start
        item["end"] = end
        shifted.append(item)
    return shifted


def _apply_time_offset(segments: list[Segment], time_offset: float) -> list[Segment]:
    if abs(time_offset) < 0.001:
        return segments
    return [
        Segment(
            **{
                **seg.model_dump(),
                "start": seg.start + time_offset,
                "end": seg.end + time_offset,
            }
        )
        for seg in segments
    ]


def _build_analysis_prompt(
    prompt_template: str,
    transcript_segments: list[dict],
    total_seconds: int,
    cfg: dict,
    clips_count: int,
    time_offset: float = 0.0,
) -> str:
    shorts_cfg = cfg["shorts"]
    prompt_segments = _offset_transcript_segments(transcript_segments, time_offset)
    transcript_text, total_duration, chunk_total_seconds = _format_transcript(
        prompt_segments
    )
    target_sec = float(shorts_cfg["target_duration_sec"])
    min_sec = float(shorts_cfg["min_duration_sec"])
    max_sec = float(shorts_cfg["max_duration_sec"])
    target_min_ratio = float(shorts_cfg.get("target_min_ratio", 0.8))
    target_max_ratio = float(shorts_cfg.get("target_max_ratio", 1.25))
    target_floor = max(min_sec, target_sec * target_min_ratio)
    target_ceiling = min(max_sec, target_sec * target_max_ratio)
    time_rules = (
        "IMPORTANT TIME/DURATION OVERRIDE:\n"
        "- Transcript timestamps are shown as [123s]. Return plain seconds, not MMSS or HHMMSS.\n"
        f"- Return exactly {clips_count} clips.\n"
        f"- Clip duration must stay close to target {target_sec:.0f}s: "
        f"accepted range {target_floor:.0f}-{target_ceiling:.0f}s. "
        f"Do not choose {min_sec:.0f}s just because it is the minimum.\n"
    )
    if abs(time_offset) >= 0.001:
        time_rules += (
            "- This is one local chunk. Transcript seconds start at 0 for this chunk.\n"
            f"- Return LOCAL chunk seconds from 0 to {max(total_seconds, chunk_total_seconds)}. "
            f"The pipeline will add global offset {time_offset:.3f}s after parsing.\n"
        )
    return time_rules + "\n" + prompt_template.format(
        clips_count=clips_count,
        min_sec=shorts_cfg["min_duration_sec"],
        max_sec=shorts_cfg["max_duration_sec"],
        target_sec=shorts_cfg["target_duration_sec"],
        transcript=transcript_text,
        total_duration=total_duration,
        total_seconds=max(total_seconds, chunk_total_seconds),
    )


def _remote_llm_enabled(cfg: dict) -> bool:
    for key in ("kimi", "gemini", "groq", "openrouter"):
        layer = cfg.get(key, {})
        if layer.get("enabled") and layer.get("api_key"):
            return True
    return False


def _split_local_analysis_chunks(transcript: dict, cfg: dict) -> list[list[dict]]:
    segments = transcript.get("segments", [])
    if not segments:
        return []

    boundaries = transcript.get("video_boundaries") or []
    if len(boundaries) > 1:
        chunks: list[list[dict]] = []
        for boundary in boundaries:
            path = str(boundary.get("path", ""))
            offset = float(boundary.get("offset", 0.0))
            end = offset + float(boundary.get("duration", 0.0))
            chunk = [
                s for s in segments
                if str(s.get("source_path", "")) == path
                or (offset <= float(s.get("start", 0.0)) < end)
            ]
            if chunk:
                chunks.append(chunk)
        if len(chunks) > 1:
            return chunks

    max_chars = int(cfg.get("ollama", {}).get("analysis_chunk_max_chars", 22000))
    full_text, _, _ = _format_transcript(segments)
    if len(full_text) <= max_chars:
        return [segments]

    chunks = []
    current: list[dict] = []
    current_chars = 0
    for seg in segments:
        line_len = len(str(seg.get("text", ""))) + 16
        if current and current_chars + line_len > max_chars:
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(seg)
        current_chars += line_len
    if current:
        chunks.append(current)
    return chunks


def _fits_without_overlap(
    segment: Segment, selected: list[Segment], min_gap_sec: float = 0.0
) -> bool:
    for kept in selected:
        if (
            segment.start < kept.end + min_gap_sec
            and segment.end + min_gap_sec > kept.start
        ):
            return False
    return True


def _non_overlapping_segments(
    segments: list[Segment], min_gap_sec: float = 0.0
) -> list[Segment]:
    selected: list[Segment] = []
    for seg in segments:
        if _fits_without_overlap(seg, selected, min_gap_sec):
            selected.append(seg)
    return selected


def _select_evenly(
    chunk_results: list[list[Segment]],
    desired_count: int,
    min_gap_sec: float = 0.0,
) -> list[Segment]:
    selected: list[Segment] = []
    idx = 0
    while len(selected) < desired_count:
        progressed = False
        for chunk in chunk_results:
            if idx >= len(chunk):
                continue
            progressed = True
            if not _fits_without_overlap(chunk[idx], selected, min_gap_sec):
                continue
            selected.append(chunk[idx])
            if len(selected) >= desired_count:
                break
        if not progressed:
            break
        idx += 1
    return selected


def _run_provider_chain(
    prompt: str,
    cfg: dict,
    asked_count: int,
    desired_count: int,
    transcript_segments: list[dict],
    total_seconds: int,
    time_offset: float = 0.0,
) -> list[Segment] | None:
    intro_end = detect_intro_end(transcript_segments)
    outro_start = detect_outro_start(transcript_segments, total_seconds)

    result = None
    provider_labels = {
        "_analyze_kimi": "Kimi",
        "_analyze_gemini": "Gemini",
        "_analyze_groq": "Groq",
        "_analyze_openrouter": "OpenRouter",
        "_analyze_ollama": "Ollama",
    }
    for fn in (
        _analyze_kimi,
        _analyze_gemini,
        _analyze_groq,
        _analyze_openrouter,
        _analyze_ollama,
    ):
        provider_label = provider_labels.get(fn.__name__, fn.__name__)
        candidates = fn(prompt, cfg, asked_count)
        if not candidates:
            continue
        candidates = _apply_time_offset(candidates, time_offset)
        filtered = _filter_and_adjust_segments(
            candidates, transcript_segments, total_seconds,
            intro_end, outro_start, cfg,
        )
        min_gap_sec = float(cfg.get("shorts", {}).get("min_gap_sec", 10.0))
        before_overlap_filter = len(filtered)
        filtered = _non_overlapping_segments(filtered, min_gap_sec)
        if before_overlap_filter and len(filtered) < before_overlap_filter:
            log.warning(
                f"Overlap-filter: {len(filtered)}/{before_overlap_filter} clips kept "
                f"with {min_gap_sec:.0f}s gap"
            )
        if not filtered:
            _diag(
                cfg,
                provider_label,
                None,
                f"{len(candidates)} parsed clips, 0 passed safety filter",
            )
        if len(filtered) >= desired_count:
            log.info(
                f"✅ Получено ровно {desired_count} клипов после safety-фильтра"
            )
            return filtered[:desired_count]
        if filtered and (result is None or len(filtered) > len(result)):
            result = filtered
            log.warning(
                f"После safety-фильтра {len(filtered)}/{desired_count} клипов, "
                "пробую следующую LLM"
            )
    return result


def _analyze_cache_key(transcript: dict, cfg: dict) -> str:
    """Stable hash of inputs that affect analyze() output."""
    import hashlib
    shorts = cfg.get("shorts", {})
    parts = [
        str(shorts.get("clips_per_video", 6)),
        str(shorts.get("target_duration_sec", 35)),
        str(shorts.get("min_duration_sec", 15)),
        str(shorts.get("max_duration_sec", 60)),
        str(len(transcript.get("segments", []))),
        # First+last segment text as fingerprint
        str(transcript.get("segments", [{}])[0].get("text", ""))[:200],
        str(transcript.get("segments", [{}])[-1].get("text", ""))[:200],
    ]
    return hashlib.sha1("||".join(parts).encode("utf-8")).hexdigest()[:16]


def _try_load_analyze_cache(transcript: dict, cfg: dict) -> list[Segment] | None:
    """Resume: если такой transcript+settings уже анализировали — берём из кеша."""
    if not cfg.get("analyze_cache", {}).get("enabled", True):
        return None
    cache_dir = Path(cfg.get("paths", {}).get("cache", "cache")) / "analyze_results"
    key = _analyze_cache_key(transcript, cfg)
    cache_file = cache_dir / f"{key}.json"
    if not cache_file.exists():
        return None
    try:
        import time as _t
        age_days = (_t.time() - cache_file.stat().st_mtime) / 86400
        max_age = float(cfg.get("analyze_cache", {}).get("max_age_days", 7))
        if age_days > max_age:
            log.info(f"Analyze cache устарел ({age_days:.1f}d > {max_age}d), пропускаю")
            return None
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        segments = [Segment(**item) for item in data.get("segments", [])]
        if segments:
            log.info(
                f"✅ Resume: analyze результат загружен из кеша "
                f"{cache_file.name} ({len(segments)} клипов, {age_days:.1f}d old)"
            )
            return segments
    except Exception as e:
        log.warning(f"Analyze cache битый ({e}), пересоздам")
    return None


def _save_analyze_cache(
    transcript: dict, cfg: dict, segments: list[Segment]
) -> None:
    """Сохраняем результат для будущего resume."""
    if not cfg.get("analyze_cache", {}).get("enabled", True):
        return
    cache_dir = Path(cfg.get("paths", {}).get("cache", "cache")) / "analyze_results"
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = _analyze_cache_key(transcript, cfg)
    cache_file = cache_dir / f"{key}.json"
    try:
        payload = {
            "segments": [s.model_dump() for s in segments],
            "cached_at": _iso_now() if "_iso_now" in globals() else None,
        }
        cache_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log.info(f"💾 Analyze результат сохранён в кеш: {cache_file.name}")
    except Exception as e:
        log.warning(f"Не смог сохранить analyze cache: {e}")


def analyze(transcript: dict, cfg: dict) -> list[Segment]:
    # Resume from checkpoint — если такой же transcript+config уже анализировали
    cached = _try_load_analyze_cache(transcript, cfg)
    if cached:
        return cached

    cfg[_DIAGNOSTICS_KEY] = []
    shorts_cfg = cfg["shorts"]

    prompt_template = Path("prompts/analyze_segments.md").read_text(encoding="utf-8")
    transcript_segments = transcript["segments"]
    _, _, total_seconds = _format_transcript(transcript_segments)
    desired_count = shorts_cfg["clips_per_video"]

    # Просим запас клипов: после safety-фильтра (intro/silence/outro)
    # может отвалиться часть. asked_count → в промпт чтобы LLM реально
    # вернула больше, а не ровно desired_count.
    asked_count = max(desired_count, int(desired_count * 1.5))

    # Two-pass анализ: если видео длинное (>= 25 мин), делаем chunked
    # независимо от провайдера. Большие промпты ломают и Kimi (0 байт content),
    # и Ollama (OOM). Chunked = LLM фокусируется на 10-мин кусках по отдельности.
    chunks = _split_local_analysis_chunks(transcript, cfg)
    force_chunked = total_seconds > float(
        cfg.get("analyze", {}).get("force_chunk_threshold_sec", 1500)
    )
    if (
        chunks
        and len(chunks) > 1
        and (
            force_chunked
            or (
                not _remote_llm_enabled(cfg)
                and cfg.get("ollama", {}).get("primary_model")
            )
        )
    ):
        log.info(
            f"Локальный LLM-анализ чанками: {len(chunks)} частей вместо одного "
            f"prompt'а на {len(transcript_segments)} сегментов"
        )
        per_chunk_asked = asked_count
        per_chunk_desired = min(per_chunk_asked, desired_count)
        chunk_results: list[list[Segment]] = []
        for i, chunk_segments in enumerate(chunks, 1):
            chunk_start, chunk_end = _clip_time_bounds(chunk_segments)
            chunk_duration = max(1, int(chunk_end - chunk_start))
            log.info(
                f"LLM-анализ чанка {i}/{len(chunks)}: "
                f"{len(chunk_segments)} сегментов транскрипта"
            )
            chunk_prompt = _build_analysis_prompt(
                prompt_template,
                chunk_segments,
                chunk_duration,
                cfg,
                per_chunk_asked,
                time_offset=chunk_start,
            )
            chunk_result = _run_provider_chain(
                chunk_prompt, cfg, per_chunk_asked, per_chunk_desired,
                chunk_segments, total_seconds, time_offset=chunk_start,
            )
            if chunk_result:
                chunk_results.append(chunk_result)
        result = _select_evenly(
            chunk_results,
            desired_count,
            float(shorts_cfg.get("min_gap_sec", 10.0)),
        )
    else:
        prompt = _build_analysis_prompt(
            prompt_template, transcript_segments, total_seconds, cfg, asked_count
        )
        result = _run_provider_chain(
            prompt, cfg, asked_count, desired_count, transcript_segments, total_seconds
        )

    if result and (
        len(result) >= desired_count or shorts_cfg.get("allow_fewer_clips", False)
    ):
        final = result[:desired_count]
        _save_analyze_cache(transcript, cfg, final)
        return final
    if result:
        _diag(
            cfg,
            "post-filter",
            None,
            f"only {len(result)}/{desired_count} clips passed safety filter",
        )

    diagnostics = cfg.get(_DIAGNOSTICS_KEY, [])
    if diagnostics:
        seen: set[str] = set()
        compact: list[str] = []
        for item in diagnostics:
            if item not in seen:
                compact.append(item)
                seen.add(item)
        details = "; ".join(compact[-12:])
    else:
        details = "нет детальной диагностики от провайдеров"

    raise RuntimeError(
        "LLM-анализ не получил пригодные клипы. Причины: "
        f"{details}. Проверь, что Ollama запущена, локальная модель установлена "
        "и в логах нет rejection по заставке/титрам/silence-зонам."
    )


def _parse_time(val: str | float | int) -> float:
    """Парсит время из разных форматов: 'MM:SS', 'HH:MM:SS', секунды."""
    if isinstance(val, (int, float)):
        return float(val)
    val = str(val).strip()
    parts = val.split(":")
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    return float(val)


def _extract_balanced_json(raw: str) -> str | None:
    """Ищет первый сбалансированный JSON-объект/массив через подсчёт скобок.

    Защищает от broken JSON: некоторые модели возвращают валидный объект,
    а потом зацикливаются ("...{\"start\":5,\"end..." + пустые строки).
    Жадный regex захватил бы весь мусор. Balance-counter находит конец
    первого закрытого объекта и стопится там.
    """
    # Игнорируем braces внутри строк
    start_idx = None
    open_char = None
    close_char = None
    depth = 0
    in_string = False
    escape = False

    for i, ch in enumerate(raw):
        if start_idx is None:
            if ch in "{[":
                start_idx = i
                open_char = ch
                close_char = "}" if ch == "{" else "]"
                depth = 1
            continue

        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue

        if ch == open_char:
            depth += 1
        elif ch == close_char:
            depth -= 1
            if depth == 0:
                return raw[start_idx : i + 1]
    return None


def _parse_response(
    raw: str, target_duration: float = 35.0, min_duration: float = 15.0
) -> list[Segment]:
    # Убираем <think>...</think> блоки (qwen3)
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    # Убираем markdown code blocks
    raw = re.sub(r"```json\s*\n?", "", raw)
    raw = re.sub(r"\n?```\s*$", "", raw)
    raw = raw.strip()

    # Пробуем как чистый JSON
    data = None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Balance-counter находит первый закрытый объект/массив.
        # Защита от broken JSON с зацикливанием (gpt-oss-120b и т.п.).
        balanced = _extract_balanced_json(raw)
        if balanced:
            try:
                data = json.loads(balanced)
            except json.JSONDecodeError:
                pass

    if data is None:
        return []

    if isinstance(data, dict):
        for key in ("segments", "clips", "highlights", "results", "moments", "data"):
            if key in data and isinstance(data[key], list):
                data = data[key]
                break
        if isinstance(data, dict) and ("start" in data or "time" in data):
            data = [data]

    if not isinstance(data, list):
        return []

    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        # Пропускаем элементы без start/end (не сегменты)
        has_time = any(
            k in item
            for k in ("start", "end", "start_time", "end_time", "begin", "time")
        )
        if not has_time:
            continue

        try:
            for src_key in ("start_time", "begin", "from"):
                if src_key in item and "start" not in item:
                    item["start"] = item.pop(src_key)
            for src_key in ("end_time", "finish", "to"):
                if src_key in item and "end" not in item:
                    item["end"] = item.pop(src_key)
            if "mood" in item and "music_mood" not in item:
                item["music_mood"] = item.pop("mood")

            if "time" in item and "start" not in item:
                t = _parse_time(item.pop("time"))
                item["start"] = t
                item["end"] = t + target_duration

            item["start"] = _parse_time(item.get("start", 0))
            item["end"] = _parse_time(item.get("end", 0))

            if item["end"] <= item["start"]:
                item["end"] = item["start"] + target_duration

            duration = item["end"] - item["start"]
            target_floor = max(min_duration, target_duration * 0.8)
            target_ceiling = target_duration * 1.25
            if duration < target_floor:
                log.warning(
                    f"Сегмент короче целевого окна ({duration:.0f}с < {target_floor:.0f}с), "
                    f"расширяю до {target_duration:.0f}с"
                )
                item["end"] = item["start"] + target_duration
            elif duration > target_ceiling:
                log.warning(
                    f"Сегмент длиннее целевого окна ({duration:.0f}с > {target_ceiling:.0f}с), "
                    f"обрезаю до {target_duration:.0f}с"
                )
                item["end"] = item["start"] + target_duration

            item.setdefault("title", item.get("description", "Clip")[:80])
            item.setdefault("hook", "")
            item.setdefault("description", "")
            item.setdefault("tags", [])
            item.setdefault("music_mood", "dramatic")

            out.append(Segment(**item))
        except (ValidationError, ValueError, TypeError) as e:
            log.warning(f"Невалидный сегмент пропущен: {e}")
    return out
