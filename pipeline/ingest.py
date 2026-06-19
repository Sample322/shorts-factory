"""Приём входного видео: локальный файл или URL через yt-dlp.

Поддерживает batch — несколько источников за один вызов.
Используется когда хочется выбрать лучшие клипы across нескольких серий/фильмов.
"""

import shutil
import subprocess
import uuid
from pathlib import Path

from .utils import get_logger, file_hash

log = get_logger("ingest")


def ingest(source: str, cache_dir: Path) -> Path:
    """source — путь к файлу или URL. Возвращает локальный путь к mp4."""
    cache_dir.mkdir(parents=True, exist_ok=True)

    if source.startswith(("http://", "https://")):
        return _download(source, cache_dir)

    src = Path(source)
    if not src.exists():
        raise FileNotFoundError(f"File not found: {source}")

    dst = cache_dir / f"input_{file_hash(src)}{src.suffix}"
    if not dst.exists():
        shutil.copy(src, dst)
    return dst


def ingest_many(sources: list[str], cache_dir: Path) -> list[Path]:
    """Принимает список путей/URL → возвращает список локальных файлов.

    Сохраняет порядок. Дубликаты допустимы (Whisper-кеш сработает).
    Если источник недоступен — поднимает FileNotFoundError для всего батча
    (лучше упасть рано, чем дойти до transcribe и потерять время).
    """
    if not sources:
        raise ValueError("ingest_many: пустой список источников")
    locals_: list[Path] = []
    for i, src in enumerate(sources, 1):
        log.info(f"[{i}/{len(sources)}] Приём: {src}")
        locals_.append(ingest(src, cache_dir))
    return locals_


def _download(url: str, cache_dir: Path) -> Path:
    """yt-dlp пишет в уникальную подпапку, чтобы не подхватить старый mp4."""
    download_dir = cache_dir / f"yt_{uuid.uuid4().hex[:10]}"
    download_dir.mkdir(parents=True, exist_ok=True)
    out_template = str(download_dir / "%(id)s.%(ext)s")
    log.info(f"Скачивание {url}...")
    subprocess.run(
        [
            "yt-dlp",
            "-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]",
            "--merge-output-format", "mp4",
            "-o", out_template,
            url,
        ],
        check=True,
    )
    mp4s = list(download_dir.glob("*.mp4"))
    if not mp4s:
        raise RuntimeError(f"yt-dlp не создал mp4-файл в {download_dir}")
    return mp4s[0]
