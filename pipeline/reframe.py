"""Три режима вертикализации: center-crop, blur-background, smart-tracking."""

import subprocess
from pathlib import Path

import cv2
import numpy as np

from .utils import get_logger, has_audio_stream, nvenc_video_args

log = get_logger("reframe")

_FACE_MODEL = Path(__file__).parent.parent / "models" / "blaze_face_short_range.tflite"


def reframe(input_path: Path, output_path: Path, mode: str, cfg: dict) -> Path:
    if mode == "center":
        return _center_crop(input_path, output_path, cfg)
    if mode == "blur":
        return _blur_bg(input_path, output_path, cfg)
    if mode == "smart":
        return _smart_track(input_path, output_path, cfg)
    raise ValueError(f"Неизвестный режим reframe: {mode}")


def _audio_args(inp: Path) -> list[str]:
    """Возвращает ffmpeg-аргументы для аудио: copy если есть, иначе ничего."""
    return ["-c:a", "copy"] if has_audio_stream(inp) else ["-an"]


def _center_crop(inp: Path, out: Path, cfg: dict) -> Path:
    w, h = cfg["video"]["output_width"], cfg["video"]["output_height"]
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(inp),
        "-vf", f"crop=ih*{w}/{h}:ih,scale={w}:{h}",
        *nvenc_video_args(cfg),
        *_audio_args(inp), str(out),
    ]
    subprocess.run(cmd, check=True)
    return out


def _blur_bg(inp: Path, out: Path, cfg: dict) -> Path:
    """Размытый фон: исходное 16:9 видео по ширине 9:16, блюр сверху/снизу."""
    w, h = cfg["video"]["output_width"], cfg["video"]["output_height"]
    # 1) Фон: масштабируем по высоте чтобы покрыть 1080x1920, кропаем, блюрим
    # 2) Передний план: масштабируем по ширине до 1080, сохраняя пропорции
    # 3) Накладываем по центру
    fc = (
        f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},gblur=sigma=40[bg];"
        f"[0:v]scale={w}:-2:force_original_aspect_ratio=decrease[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2"
    )
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(inp),
        "-filter_complex", fc,
        *nvenc_video_args(cfg),
        *_audio_args(inp), str(out),
    ]
    subprocess.run(cmd, check=True)
    return out


def _smart_track(inp: Path, out: Path, cfg: dict) -> Path:
    """Детектит лицо через MediaPipe и кропает с блюр-фоном.

    zoom_out контролирует, сколько контекста вокруг лица показывать:
    0.0 = жёсткий 9:16 кроп (очень близко)
    0.5 = ~половина ширины кадра + блюр-бары сверху/снизу
    1.0 = полная ширина (= blur mode)
    """
    import mediapipe as mp
    from mediapipe.tasks import python as mp_python
    from mediapipe.tasks.python import vision

    if not _FACE_MODEL.exists():
        log.warning("Модель face detection не найдена, fallback на blur")
        return _blur_bg(inp, out, cfg)

    reframe_cfg = cfg.get("reframe", {})
    detect_interval = reframe_cfg.get("smart_detect_interval", 3)
    smooth_alpha = reframe_cfg.get("smooth_alpha", 0.04)
    min_confidence = reframe_cfg.get("min_face_confidence", 0.45)
    no_face_fallback = reframe_cfg.get("no_face_fallback", "hold")
    zoom_out = reframe_cfg.get("smart_zoom_out", 0.45)

    cap = cv2.VideoCapture(str(inp))
    fps = cap.get(cv2.CAP_PROP_FPS)
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    target_w = cfg["video"]["output_width"]
    target_h = cfg["video"]["output_height"]

    # Базовый 9:16 кроп + расширение через zoom_out
    base_crop_w = int(src_h * target_w / target_h)
    base_crop_w = min(base_crop_w, src_w)
    crop_w = int(base_crop_w + (src_w - base_crop_w) * zoom_out)
    crop_w = min(crop_w, src_w)

    log.info(
        f"Smart track: src={src_w}x{src_h}, "
        f"crop_w={crop_w} (base_9:16={base_crop_w}, zoom_out={zoom_out})"
    )

    # Детектор лиц
    base_options = mp_python.BaseOptions(model_asset_path=str(_FACE_MODEL))
    options = vision.FaceDetectorOptions(
        base_options=base_options,
        min_detection_confidence=min_confidence,
    )
    detector = vision.FaceDetector.create_from_options(options)

    centers_x: list[tuple[int, int]] = []
    frame_idx = 0
    last_detected_x = src_w // 2
    detection_count = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if frame_idx % detect_interval == 0:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            result = detector.detect(mp_image)

            if result.detections:
                best = _pick_best_face(result.detections, src_w)
                bb = best.bounding_box
                last_detected_x = bb.origin_x + bb.width // 2
                detection_count += 1
            else:
                if no_face_fallback == "center":
                    last_detected_x = src_w // 2

            centers_x.append((frame_idx, last_detected_x))
        frame_idx += 1

    cap.release()
    detector.close()

    if not centers_x:
        log.warning("Кадры не обработаны, fallback на blur")
        return _blur_bg(inp, out, cfg)

    face_ratio = detection_count / max(len(centers_x), 1) * 100
    log.info(
        f"Smart tracking: {detection_count} детекций лиц "
        f"из {len(centers_x)} проверенных ({face_ratio:.0f}%)"
    )

    # Сглаживание
    smoothed = _smooth_trajectory(centers_x, n_frames, alpha=smooth_alpha)

    half = crop_w // 2
    smoothed = np.clip(smoothed, half, src_w - half)

    # Рендер покадрово через FFmpeg pipe (NVENC + аудио за один проход)
    need_blur_bars = crop_w > base_crop_w
    fg_h = int(target_w * src_h / crop_w) if need_blur_bars else target_h
    fg_h = min(fg_h, target_h)
    # Гарантируем чётные размеры
    fg_h = fg_h - (fg_h % 2)

    y_offset = (target_h - fg_h) // 2

    audio_available = has_audio_stream(inp)
    ffmpeg_cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        # Raw video из pipe
        "-f", "rawvideo", "-pix_fmt", "bgr24",
        "-s", f"{target_w}x{target_h}",
        "-r", str(fps),
        "-i", "pipe:0",
    ]
    if audio_available:
        ffmpeg_cmd += [
            "-i", str(inp),
            "-map", "0:v", "-map", "1:a",
            *nvenc_video_args(cfg),
            "-c:a", "copy", "-shortest",
        ]
    else:
        ffmpeg_cmd += [
            "-map", "0:v",
            *nvenc_video_args(cfg),
            "-an",
        ]
    ffmpeg_cmd.append(str(out))
    ffproc = subprocess.Popen(ffmpeg_cmd, stdin=subprocess.PIPE)

    cap = cv2.VideoCapture(str(inp))
    idx = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        cx = int(smoothed[idx]) if idx < len(smoothed) else int(smoothed[-1])
        x0 = max(0, cx - half)
        x1 = min(src_w, x0 + crop_w)

        cropped = frame[:, x0:x1]

        if need_blur_bars:
            small = cv2.resize(cropped, (270, 480))
            small_blur = cv2.GaussianBlur(small, (0, 0), sigmaX=15, sigmaY=15)
            bg = cv2.resize(small_blur, (target_w, target_h))

            fg = cv2.resize(
                cropped, (target_w, fg_h), interpolation=cv2.INTER_LANCZOS4
            )

            bg[y_offset : y_offset + fg_h, :] = fg
            out_frame = bg
        else:
            out_frame = cv2.resize(
                cropped, (target_w, target_h), interpolation=cv2.INTER_LANCZOS4
            )

        ffproc.stdin.write(out_frame.tobytes())
        idx += 1

    cap.release()
    ffproc.stdin.close()
    ffproc.wait()

    if ffproc.returncode != 0:
        raise RuntimeError(f"FFmpeg smart-track failed (rc={ffproc.returncode})")

    return out


def _pick_best_face(detections: list, src_w: int) -> object:
    center = src_w / 2

    def score(det: object) -> float:
        bb = det.bounding_box
        face_cx = bb.origin_x + bb.width / 2
        size_score = bb.width * bb.height
        dist_ratio = abs(face_cx - center) / max(center, 1)
        center_bonus = 1.0 + 0.5 * (1.0 - min(dist_ratio, 1.0))
        return size_score * center_bonus

    return max(detections, key=score)


def _smooth_trajectory(
    samples: list[tuple[int, int]], n_frames: int, alpha: float = 0.04
) -> np.ndarray:
    xs = np.array([s[0] for s in samples])
    ys = np.array([s[1] for s in samples], dtype=np.float64)
    full = np.interp(np.arange(n_frames), xs, ys)

    forward = np.copy(full)
    for i in range(1, len(forward)):
        forward[i] = alpha * full[i] + (1 - alpha) * forward[i - 1]

    backward = np.copy(full)
    for i in range(len(backward) - 2, -1, -1):
        backward[i] = alpha * full[i] + (1 - alpha) * backward[i + 1]

    return (forward + backward) / 2.0
