"""Детектор заставки/титров по плотности транскрипта.

Идея: заставка сериала = музыка + минимум речи. Основной эпизод = плотный диалог.
В окнах 10 сек считаем слова. Граница intro/outro = резкий скачок плотности.

Это soft-эвристика. Если детектор не нашёл (фильм без заставки) — возвращает
0/total_sec и блок-фильтр отключается, остаются только word-density проверки.

Используется в analyze.py:
1. detect_intro_end / detect_outro_start — границы для smart-skip
2. adjust_clip_borders — сдвигает start/end клипа за пределы заставки
"""

from __future__ import annotations

from .utils import get_logger

log = get_logger("intro_detect")


def _word_density_windows(
    transcript_segments: list[dict],
    window_start: float,
    window_end: float,
    window_sec: float = 10.0,
) -> list[tuple[float, int]]:
    """Считает слова в окнах [t, t+window_sec] на интервале [window_start, window_end].

    Возвращает [(window_start_sec, word_count), ...].
    Слово = токен длиной >= 2 символа (отсекает междометия "а", "о", "э").
    """
    out: list[tuple[float, int]] = []
    t = window_start
    while t < window_end:
        we = t + window_sec
        n = 0
        for s in transcript_segments:
            ss = float(s.get("start", 0))
            se = float(s.get("end", ss))
            if se >= t and ss <= we:
                txt = str(s.get("text", "")).strip()
                n += sum(1 for w in txt.split() if len(w) >= 2)
        out.append((t, n))
        t = we
    return out


def detect_intro_end(
    transcript_segments: list[dict],
    max_check_sec: float = 180.0,
    speech_threshold_words: int = 12,
    window_sec: float = 10.0,
) -> float:
    """Находит конец intro: первое окно с плотной речью.

    Возвращает 0.0 если intro нет (видео сразу начинается с диалога) или
    если детектор не уверен (первое окно уже плотное).

    speech_threshold_words=12 → ~1.2 слова/сек = нормальный темп диалога.
    Заставка обычно 0-3 слова на 10 сек (припев "Рик и Морти" 2-4 повтора).
    """
    if not transcript_segments:
        return 0.0

    windows = _word_density_windows(transcript_segments, 0, max_check_sec, window_sec)
    if not windows:
        return 0.0

    # Первое окно уже плотное — нет заставки, видео сразу с диалога
    if windows[0][1] >= speech_threshold_words:
        return 0.0

    # Ищем первое плотное окно после низкоплотных
    for window_start, words in windows:
        if words >= speech_threshold_words:
            log.info(
                f"📺 Детектор intro: заставка до {window_start:.0f}s "
                f"(первое плотное окно {words} слов >= {speech_threshold_words})"
            )
            return window_start

    log.info(
        f"📺 Детектор intro: плотного диалога не найдено в первых "
        f"{max_check_sec:.0f}s, intro_end=0 (без skip)"
    )
    return 0.0


def detect_outro_start(
    transcript_segments: list[dict],
    total_seconds: float,
    max_check_sec: float = 120.0,
    speech_threshold_words: int = 12,
    window_sec: float = 10.0,
) -> float:
    """Находит начало outro: конец последнего окна с плотной речью.

    Возвращает total_seconds если outro нет.
    """
    if not transcript_segments or total_seconds <= 0:
        return total_seconds

    start_check = max(0.0, total_seconds - max_check_sec)
    windows = _word_density_windows(
        transcript_segments, start_check, total_seconds, window_sec
    )
    if not windows:
        return total_seconds

    # С конца: последнее плотное окно
    for window_start, words in reversed(windows):
        if words >= speech_threshold_words:
            outro_start = window_start + window_sec
            log.info(
                f"📺 Детектор outro: outro начинается на {outro_start:.0f}s "
                f"(последнее плотное окно {words} слов)"
            )
            return min(outro_start, total_seconds)

    log.info(
        f"📺 Детектор outro: плотного диалога не найдено в последних "
        f"{max_check_sec:.0f}s, outro_start={total_seconds} (без skip)"
    )
    return total_seconds


def adjust_clip_borders(
    clip_start: float,
    clip_end: float,
    intro_end: float,
    outro_start: float,
    min_duration: float,
    target_duration: float,
    max_duration: float,
) -> tuple[float, float] | None:
    """Сдвигает границы клипа за пределы заставки/титров.

    Сценарии:
    - Клип ЦЕЛИКОМ в intro/outro → return None (отбраковать)
    - Клип ЧАСТИЧНО перекрывает intro/outro → сдвигаем границы
    - Если после сдвига короче min_duration — пробуем extend end до target
    - Если всё равно не вмещается — return None

    Возвращает (new_start, new_end) или None.
    """
    # 1) Клип целиком в intro
    if clip_end <= intro_end:
        return None
    # 2) Клип целиком в outro
    if clip_start >= outro_start:
        return None

    new_start = max(clip_start, intro_end)
    new_end = min(clip_end, outro_start)

    duration = new_end - new_start
    target_floor = max(min_duration, target_duration * 0.8)

    # После сдвига слишком короткий — расширим end (если есть запас до outro)
    if duration < target_floor:
        wanted_end = new_start + target_duration
        if wanted_end <= outro_start:
            new_end = wanted_end
            duration = new_end - new_start
        else:
            # outro мешает — пробуем total как можно больше
            new_end = outro_start
            duration = new_end - new_start
            if duration < target_floor:
                return None  # не помещается

    # Слишком длинный → обрезаем до max
    if duration > max_duration:
        new_end = new_start + max_duration

    return (new_start, new_end)
