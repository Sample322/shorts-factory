"""Auto-thumbnail: вырезаем кадр с максимальной face confidence + overlay title.

Использует MediaPipe для поиска лучшего кадра + PIL для text overlay.
Возвращает путь к JPG. Используется в youtube_upload как кастомный thumbnail.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from .utils import get_logger

log = get_logger("thumbnail")


def _find_best_face_frame(video_path: Path, sample_count: int = 24) -> float:
    """Сэмплируем sample_count кадров, находим кадр с лучшим face conf.

    Возвращает timestamp в секундах. Если лиц нет — середина видео.
    """
    try:
        import cv2
        import mediapipe as mp
    except ImportError:
        log.warning("mediapipe не установлен, thumbnail = середина клипа")
        return _video_duration(video_path) / 2

    duration = _video_duration(video_path)
    if duration <= 0:
        return 0.5

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return duration / 2

    face_det = mp.solutions.face_detection.FaceDetection(
        model_selection=1, min_detection_confidence=0.3
    )
    best_ts = duration / 2
    best_score = -1.0
    try:
        for i in range(sample_count):
            ts = (i + 1) * duration / (sample_count + 1)
            cap.set(cv2.CAP_PROP_POS_MSEC, ts * 1000)
            ret, frame = cap.read()
            if not ret:
                continue
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            results = face_det.process(rgb)
            if not results.detections:
                continue
            # Берём max face confidence в кадре
            max_conf = max(d.score[0] for d in results.detections)
            if max_conf > best_score:
                best_score = max_conf
                best_ts = ts
    finally:
        cap.release()
        face_det.close()

    log.info(
        f"Thumbnail: лучший кадр {best_ts:.1f}s "
        f"(face conf {best_score:.2f})" if best_score > 0
        else f"Thumbnail: лица не найдены, использую середину {best_ts:.1f}s"
    )
    return best_ts


def _video_duration(video_path: Path) -> float:
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            capture_output=True, text=True, timeout=10,
        )
        return float(r.stdout.strip() or 0)
    except (subprocess.SubprocessError, ValueError):
        return 0.0


def _extract_frame(video_path: Path, timestamp: float, out_path: Path) -> Path:
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", str(timestamp), "-i", str(video_path),
        "-frames:v", "1", "-q:v", "2",
        str(out_path),
    ]
    subprocess.run(cmd, check=True)
    return out_path


def _overlay_text(
    image_path: Path,
    title: str,
    out_path: Path,
    font_size: int = 120,
    text_color: tuple = (255, 255, 255),
    stroke_color: tuple = (0, 0, 0),
    stroke_width: int = 8,
) -> Path:
    """Накладывает title overlay на кадр (нижняя треть, центрированный)."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        import shutil
        shutil.copy(image_path, out_path)
        return out_path

    img = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(img)

    # Try Montserrat ExtraBold if available, else default
    font_paths = [
        Path("fonts/Montserrat-ExtraBold.ttf"),
        Path("fonts/Montserrat-Bold.ttf"),
        Path("fonts/RussoOne-Regular.ttf"),
    ]
    font = None
    for fp in font_paths:
        if fp.exists():
            try:
                font = ImageFont.truetype(str(fp), font_size)
                break
            except Exception:
                continue
    if font is None:
        font = ImageFont.load_default()

    # Перенос title по ширине
    title_upper = title.upper()
    max_width = img.width * 0.9
    lines: list[str] = []
    words = title_upper.split()
    current = ""
    for w in words:
        test = (current + " " + w).strip()
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] - bbox[0] > max_width and current:
            lines.append(current)
            current = w
        else:
            current = test
    if current:
        lines.append(current)

    # Lines в нижней трети, centered
    line_h = font_size + 10
    total_h = line_h * len(lines)
    start_y = int(img.height * 0.65) - total_h // 2

    for i, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=font)
        w = bbox[2] - bbox[0]
        x = (img.width - w) // 2
        y = start_y + i * line_h
        draw.text(
            (x, y), line, font=font,
            fill=text_color, stroke_width=stroke_width, stroke_fill=stroke_color,
        )

    img.save(out_path, "JPEG", quality=92)
    return out_path


def generate_thumbnail(
    clip_video: Path,
    title: str,
    out_dir: Path,
    name: str = "thumbnail.jpg",
) -> Path | None:
    """Полный pipeline: найти лучший кадр + overlay title.

    Возвращает путь к финальному JPG или None при ошибке.
    """
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = _find_best_face_frame(clip_video)
        with tempfile.NamedTemporaryFile(
            suffix=".jpg", delete=False, dir=str(out_dir)
        ) as tmp:
            tmp_path = Path(tmp.name)
        try:
            _extract_frame(clip_video, ts, tmp_path)
            final = out_dir / name
            _overlay_text(tmp_path, title, final)
            return final
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
    except Exception as e:
        log.warning(f"Thumbnail generation упал: {e}")
        return None
