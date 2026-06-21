"""Генерация ASS-субтитров с настраиваемой анимацией и стилем."""

import re
from pathlib import Path

import pysubs2

from .utils import get_logger, nvenc_video_args

log = get_logger("subtitle")

FONTS_DIR = Path(__file__).parent.parent / "fonts"

AVAILABLE_FONTS = {
    "Montserrat ExtraBold": "Montserrat-ExtraBold.ttf",
    "Montserrat Bold": "Montserrat-Bold.ttf",
    "Russo One": "RussoOne-Regular.ttf",
    "Impact": None,
    "Arial Black": None,
    "Arial": None,
    "Segoe UI": None,
    "Calibri": None,
    "Tahoma": None,
}

ANIMATION_STYLES = {
    "karaoke": "Плавная подсветка слова",
    "instant": "Мгновенная подсветка слова",
    "fade": "Плавное появление строки",
    "none": "Без анимации",
}

# Готовые пресеты — быстрая замена десятка слайдеров одним выбором
SUBTITLE_PRESETS: dict[str, dict] = {
    "current_karaoke": {
        "font_name": "Montserrat ExtraBold", "font_size": 72,
        "words_per_line": 3, "text_color": "#FFFFFF",
        "highlight_color": "#FFD700", "outline_color": "#000000",
        "outline_width": 6, "margin_v": 220, "uppercase": True,
        "animation": "karaoke", "background_box": False,
    },
    "tiktok_bold": {
        "font_name": "Montserrat ExtraBold", "font_size": 84,
        "words_per_line": 2, "text_color": "#FFFFFF",
        "highlight_color": "#FFEE00", "outline_color": "#000000",
        "outline_width": 10, "margin_v": 300, "uppercase": True,
        "animation": "instant", "background_box": False,
    },
    "mr_beast": {
        "font_name": "Russo One", "font_size": 96,
        "words_per_line": 2, "text_color": "#FFFFFF",
        "highlight_color": "#00FF7F", "outline_color": "#000000",
        "outline_width": 12, "margin_v": 250, "uppercase": True,
        "animation": "instant", "background_box": False,
    },
    "minimal": {
        "font_name": "Montserrat Bold", "font_size": 56,
        "words_per_line": 4, "text_color": "#FFFFFF",
        "highlight_color": "#FFFFFF", "outline_color": "#000000",
        "outline_width": 3, "margin_v": 180, "uppercase": False,
        "animation": "none", "background_box": True,
        "background_color": "#000000", "background_alpha": 180,
    },
    "story_book": {
        "font_name": "Montserrat Bold", "font_size": 64,
        "words_per_line": 3, "text_color": "#FFFFFF",
        "highlight_color": "#FF6B6B", "outline_color": "#1A1A1A",
        "outline_width": 4, "margin_v": 200, "uppercase": False,
        "animation": "fade", "background_box": True,
        "background_color": "#000000", "background_alpha": 140,
    },
}

PRESET_LABELS = {
    "current_karaoke": "🎤 Karaoke (текущий)",
    "tiktok_bold": "🔥 TikTok Bold",
    "mr_beast": "💚 Mr. Beast",
    "minimal": "✨ Minimal",
    "story_book": "📖 Story Book",
}

GOLDEN_STANDARD = {
    "font_name": "Montserrat ExtraBold",
    # Для 1080x1920 (вертикальные Shorts) большие каналы используют 70-90pt.
    # 28pt был слишком мелкий — на телефонном экране почти нечитаемо.
    "font_size": 72,
    "words_per_line": 3,
    "text_color": "#FFFFFF",
    "highlight_color": "#FFD700",
    "outline_color": "#000000",
    "outline_width": 6,
    "margin_v": 220,
    "margin_h": 60,
    "alignment": 2,
    "uppercase": True,
    "animation": "karaoke",
    "timing_offset_ms": 0,
}


def _words_from_segments(
    segments: list[dict], clip_start: float, clip_end: float
) -> list[dict]:
    words: list[dict] = []

    for seg in segments:
        seg_start = seg.get("start", 0)
        seg_end = seg.get("end", 0)

        # Сегмент должен значительно пересекаться с клипом (>0.5с)
        overlap_start = max(seg_start, clip_start)
        overlap_end = min(seg_end, clip_end)
        if overlap_end - overlap_start < 0.5:
            continue

        text = seg.get("text", "").strip()
        if not text:
            continue

        raw_words = [w for w in re.split(r"\s+", text) if w]
        if not raw_words:
            continue

        # Используем полные тайминги сегмента, не обрезаем по clip
        duration = seg_end - seg_start
        if duration <= 0:
            continue

        word_dur = duration / len(raw_words)
        for i, w in enumerate(raw_words):
            w_start = seg_start + i * word_dur
            w_end = seg_start + (i + 1) * word_dur
            # Пропускаем слова за пределами клипа
            if w_end < clip_start or w_start > clip_end:
                continue
            words.append(
                {"word": w, "start": round(w_start, 3), "end": round(w_end, 3)}
            )

    return words


def _color_hex_to_ass(hex_color: str) -> str:
    """Конвертирует #RRGGBB или #AARRGGBB в ASS-формат &HAABBGGRR."""
    h = hex_color.lstrip("#")
    if len(h) == 6:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return f"&H00{b:02X}{g:02X}{r:02X}"
    if len(h) == 8:
        a, r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), int(h[6:8], 16)
        return f"&H{a:02X}{b:02X}{g:02X}{r:02X}"
    return "&H00FFFFFF"


def _parse_color(val: str) -> pysubs2.Color:
    """Парсит цвет из HEX (#RRGGBB) или ASS (&HAABBGGRR)."""
    val = val.strip().strip('"').strip("'")
    if val.startswith("#"):
        h = val.lstrip("#")
        if len(h) == 6:
            r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
            return pysubs2.Color(r, g, b, 0)
        if len(h) == 8:
            a, r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), int(h[6:8], 16)
            return pysubs2.Color(r, g, b, a)
    if val.startswith("&H"):
        hex_str = val[2:].ljust(8, "0")
        a = int(hex_str[0:2], 16)
        b = int(hex_str[2:4], 16)
        g = int(hex_str[4:6], 16)
        r = int(hex_str[6:8], 16)
        return pysubs2.Color(r, g, b, a)
    return pysubs2.Color(255, 255, 255, 0)


def build_ass(
    word_segments: list[dict],
    output_ass: Path,
    cfg: dict,
    clip_start: float,
    clip_end: float,
    transcript_segments: list[dict] | None = None,
) -> None:
    subs = pysubs2.SSAFile()
    sub_cfg = cfg.get("subtitle", {})

    out_w = cfg["video"]["output_width"]
    out_h = cfg["video"]["output_height"]

    # Привязка координатной системы к разрешению видео
    subs.info["PlayResX"] = str(out_w)
    subs.info["PlayResY"] = str(out_h)
    subs.info["WrapStyle"] = "0"
    subs.info["ScaledBorderAndShadow"] = "yes"

    font_name = sub_cfg.get("font_name", "Montserrat ExtraBold")
    font_size = sub_cfg.get("font_size", 72)
    # Для 1080x1920 (PlayResY=1920) 1 единица = 1 пиксель.
    # Большие YouTube Shorts каналы используют 60-100pt. Допускаем 20-140.
    font_size = max(20, min(140, font_size))

    animation = sub_cfg.get("animation", "karaoke")
    use_uppercase = sub_cfg.get("uppercase", True)
    words_per_line = sub_cfg.get("words_per_line", 3)
    timing_offset = sub_cfg.get("timing_offset_ms", -200)

    margin_v = sub_cfg.get("margin_v", 100)
    margin_h = sub_cfg.get("margin_h", 80)

    text_color = _parse_color(sub_cfg.get("text_color", "#FFFFFF"))
    highlight_color = _parse_color(sub_cfg.get("highlight_color", "#FFD700"))
    outline_color = _parse_color(sub_cfg.get("outline_color", "#000000"))
    outline_w = sub_cfg.get("outline_width", 6)
    # На крупном шрифте 70-100pt тонкий контур невидим на светлом фоне.
    # Поднимаем потолок до 14.
    outline_w = max(1, min(14, outline_w))

    # Background box опция: BorderStyle=3 рисует непрозрачный/полупрозрачный
    # box за текстом вместо stroke+shadow. Улучшает читаемость на ярких
    # сценах с переменным фоном. Альфа 0=прозрачный, 255=непрозрачный.
    bg_box = bool(sub_cfg.get("background_box", False))
    bg_alpha = int(sub_cfg.get("background_alpha", 160))  # 160 ≈ 63% прозрачности
    bg_color_hex = sub_cfg.get("background_color", "#000000")
    bg_rgba = _parse_color(bg_color_hex)
    # pysubs2.Color имеет (r,g,b,a); _parse_color возвращает Color объект
    bg_color = pysubs2.Color(bg_rgba.r, bg_rgba.g, bg_rgba.b, 255 - bg_alpha)

    style = subs.styles["Default"]
    style.fontname = font_name
    style.fontsize = font_size
    style.primarycolor = text_color
    style.secondarycolor = highlight_color
    style.outlinecolor = outline_color
    style.backcolor = bg_color if bg_box else pysubs2.Color(0, 0, 0, 128)
    style.outline = 0 if bg_box else outline_w
    style.shadow = 0 if bg_box else 1
    style.borderstyle = 3 if bg_box else 1  # 3 = opaque box; 1 = outline+shadow
    style.alignment = 2
    style.marginv = margin_v
    style.marginl = margin_h
    style.marginr = margin_h
    style.bold = True

    # --- Получаем слова ---
    # Берём слова, у которых ЛЮБАЯ часть пересекается с клипом
    # (не только start) — иначе слово на границе теряется.
    def _overlaps(w: dict, lo: float, hi: float) -> bool:
        if "start" not in w:
            return False
        w_start = w["start"]
        w_end = w.get("end", w_start + 0.3)
        return w_end >= lo and w_start <= hi

    words_in_clip = [w for w in word_segments if _overlaps(w, clip_start, clip_end)]

    if not words_in_clip and transcript_segments:
        words_in_clip = _words_from_segments(
            transcript_segments, clip_start, clip_end
        )
        if words_in_clip:
            log.info(
                f"Сгенерировано {len(words_in_clip)} слов из сегментов транскрипта"
            )

    # Последний fallback: расширяем диапазон поиска на 2 секунды
    if not words_in_clip and word_segments:
        words_in_clip = [
            w for w in word_segments if _overlaps(w, clip_start - 2, clip_end + 2)
        ]
        if words_in_clip:
            log.info(f"Найдено {len(words_in_clip)} слов с расширенным диапазоном")

    if not words_in_clip:
        log.warning(
            f"Нет слов для субтитров в диапазоне [{clip_start:.1f}-{clip_end:.1f}]"
        )
        subs.save(str(output_ass))
        return

    # --- Группируем слова умно: режем по паузам, не только по words_per_line ---
    # Большая пауза между словами (>0.6с) = новая строка. Иначе при паузе
    # текущая строка остаётся на экране в тишине, а потом следующая внезапно
    # появляется — выглядит как "субтитры отстают".
    PAUSE_BREAK_SEC = 0.6
    groups: list[list[dict]] = []
    current: list[dict] = []
    last_end = None
    for w in words_in_clip:
        if current and (
            len(current) >= words_per_line
            or (last_end is not None and w["start"] - last_end > PAUSE_BREAK_SEC)
        ):
            groups.append(current)
            current = []
        current.append(w)
        last_end = w.get("end", w["start"] + 0.3)
    if current:
        groups.append(current)

    for gi, group in enumerate(groups):
        g_start_abs = group[0]["start"]
        g_end_abs = group[-1].get("end", group[-1]["start"] + 0.5)

        # Держим строку на экране ДО появления следующей группы — это убирает
        # моргания и "пустые" моменты. Минимум — собственный end.
        if gi + 1 < len(groups):
            hold_until = groups[gi + 1][0]["start"] - 0.05  # 50мс перед следующей
            g_end_abs = max(g_end_abs, hold_until)

        g_start = (g_start_abs - clip_start) * 1000 + timing_offset
        g_end = (g_end_abs - clip_start) * 1000 + timing_offset
        g_start = max(0, g_start)
        g_end = max(g_start + 100, g_end)

        if animation in ("karaoke", "instant"):
            parts: list[str] = []
            tag = "kf" if animation == "karaoke" else "k"
            for j, w in enumerate(group):
                w_start = w["start"]
                w_end = w.get("end", w_start + 0.3)
                # Учитываем паузу ДО этого слова — она тоже должна "отыграть"
                # перед подсветкой, иначе текущее слово подсветится моментально.
                if j > 0:
                    prev_end = group[j - 1].get("end", group[j - 1]["start"] + 0.3)
                    gap = max(0.0, w_start - prev_end)
                    if gap > 0.02:
                        parts.append(f"{{\\{tag}{int(gap * 100)}}}")
                w_dur = max(0.05, w_end - w_start)
                dur_cs = max(5, int(w_dur * 100))
                word_text = w["word"].strip()
                if use_uppercase:
                    word_text = word_text.upper()
                # Без пробела внутри ASS-тега. Пробелы между словами идут
                # отдельным span'ом без подсветки.
                sep = " " if j > 0 else ""
                parts.append(f"{sep}{{\\{tag}{dur_cs}}}{word_text}")
            text = "".join(parts)
        elif animation == "fade":
            word_texts = [w["word"].strip() for w in group]
            if use_uppercase:
                word_texts = [wt.upper() for wt in word_texts]
            text = "{\\fad(150,100)}" + " ".join(word_texts)
        else:
            word_texts = [w["word"].strip() for w in group]
            if use_uppercase:
                word_texts = [wt.upper() for wt in word_texts]
            text = " ".join(word_texts)

        line = pysubs2.SSAEvent(
            start=int(g_start),
            end=int(g_end),
            text=text,
            style="Default",
        )
        subs.events.append(line)

    subs.save(str(output_ass))
    log.info(f"Субтитры: {len(subs.events)} строк для клипа")


def burn_subtitles(
    video_in: Path, ass_file: Path, video_out: Path, cfg: dict
) -> Path:
    import os
    import subprocess

    # FFmpeg ASS-фильтр не любит Windows-абсолютные пути (: = разделитель опций).
    # Используем относительные пути от CWD.
    cwd = Path.cwd()
    try:
        ass_rel = ass_file.resolve().relative_to(cwd).as_posix()
    except ValueError:
        ass_rel = str(ass_file).replace("\\", "/").replace(":", r"\:")

    try:
        fonts_rel = FONTS_DIR.resolve().relative_to(cwd).as_posix()
    except ValueError:
        fonts_rel = str(FONTS_DIR).replace("\\", "/").replace(":", r"\:")

    vf = f"ass={ass_rel}:fontsdir={fonts_rel}"

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(video_in),
        "-vf", vf,
        *nvenc_video_args(cfg),
        "-c:a", "copy",
        str(video_out),
    ]
    subprocess.run(cmd, check=True)
    return video_out
