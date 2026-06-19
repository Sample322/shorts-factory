"""Standalone CLI для скачивания модели с HuggingFace в фоне.

Запускается через subprocess из app.py чтобы UI не блокировался на 30-60 минут.

Usage:
    python -m pipeline.hf_downloader --repo-id stabilityai/stable-audio-open-1.0
                                     --status-file cache/hf_download.status.json

Status-файл (атомарно перезаписывается каждые 2 сек):
    {
        "status": "running" | "done" | "error",
        "downloaded_mb": float,
        "total_mb_estimate": float,
        "pct": float,
        "speed_mb_s": float,
        "eta_sec": float,
        "started_at": float,
        "updated_at": float,
        "error": str | null
    }
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

# Дефолтные оценки размера популярных моделей (только нужные diffusers
# файлы, без model.ckpt и корневого model.safetensors).
# Реальный замер: transformer 2.4 ГБ + vae 0.4 ГБ + T5 text_encoder 0.6 ГБ
# + мелочь = ~3.5 ГБ.
_DEFAULT_SIZE_ESTIMATES_MB = {
    "stabilityai/stable-audio-open-1.0": 3500,
}


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _folder_size_mb(folder: Path) -> float:
    if not folder.exists():
        return 0.0
    total = 0
    for p in folder.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total / (1024 * 1024)


def _monitor_progress(
    cache_folder: Path,
    status_file: Path,
    total_mb_estimate: float,
    stop_event: threading.Event,
    started_at: float,
) -> None:
    """В фоне опрашивает размер папки и пишет в status.json."""
    last_mb = 0.0
    last_t = started_at
    while not stop_event.is_set():
        try:
            cur_mb = _folder_size_mb(cache_folder)
            now = time.time()
            dt = now - last_t
            speed = (cur_mb - last_mb) / dt if dt > 0 else 0
            pct = min(100.0, (cur_mb / total_mb_estimate) * 100) if total_mb_estimate else 0
            eta = ((total_mb_estimate - cur_mb) / speed) if speed > 0.1 else 0

            _atomic_write_json(status_file, {
                "status": "running",
                "downloaded_mb": round(cur_mb, 1),
                "total_mb_estimate": total_mb_estimate,
                "pct": round(pct, 1),
                "speed_mb_s": round(speed, 2),
                "eta_sec": round(eta, 0),
                "started_at": started_at,
                "updated_at": now,
                "error": None,
            })
            last_mb = cur_mb
            last_t = now
        except Exception:
            pass
        # Поллим раз в 2 сек
        stop_event.wait(2.0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-id", required=True)
    parser.add_argument("--status-file", required=True, type=Path)
    parser.add_argument(
        "--allow-patterns", default="*.json,*.safetensors,*.model",
        help="Comma-separated patterns",
    )
    parser.add_argument(
        "--ignore-patterns",
        default="model.ckpt,model.safetensors,vae_model.ckpt,*.png,*.csv,*.md",
        help=("Comma-separated patterns to skip. "
              "By default skips Stable-Audio-Tools format files "
              "(diffusers doesn't need them)."),
    )
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    from huggingface_hub import snapshot_download
    from huggingface_hub.constants import HF_HUB_CACHE

    # Папка модели в hf cache
    repo_folder = Path(HF_HUB_CACHE) / f"models--{args.repo_id.replace('/', '--')}"

    total_estimate = _DEFAULT_SIZE_ESTIMATES_MB.get(args.repo_id, 1000)
    started_at = time.time()

    print(f"[hf_downloader] start repo={args.repo_id} cache={repo_folder}", flush=True)

    # Запускаем мониторинг в отдельном потоке
    stop_event = threading.Event()
    monitor = threading.Thread(
        target=_monitor_progress,
        args=(repo_folder, args.status_file, total_estimate, stop_event, started_at),
        daemon=True,
    )
    monitor.start()

    try:
        snapshot_download(
            repo_id=args.repo_id,
            allow_patterns=[p.strip() for p in args.allow_patterns.split(",") if p.strip()],
            ignore_patterns=[p.strip() for p in args.ignore_patterns.split(",") if p.strip()],
            max_workers=4,
        )
    except Exception as e:
        stop_event.set()
        monitor.join(timeout=3.0)
        cur_mb = _folder_size_mb(repo_folder)
        _atomic_write_json(args.status_file, {
            "status": "error",
            "downloaded_mb": round(cur_mb, 1),
            "total_mb_estimate": total_estimate,
            "pct": round((cur_mb / total_estimate) * 100, 1) if total_estimate else 0,
            "speed_mb_s": 0.0,
            "eta_sec": 0,
            "started_at": started_at,
            "updated_at": time.time(),
            "error": f"{type(e).__name__}: {e}",
        })
        print(f"[hf_downloader] EXCEPTION: {e}", file=sys.stderr, flush=True)
        return 1

    stop_event.set()
    monitor.join(timeout=3.0)
    final_mb = _folder_size_mb(repo_folder)
    _atomic_write_json(args.status_file, {
        "status": "done",
        "downloaded_mb": round(final_mb, 1),
        "total_mb_estimate": total_estimate,
        "pct": 100.0,
        "speed_mb_s": 0.0,
        "eta_sec": 0,
        "started_at": started_at,
        "updated_at": time.time(),
        "error": None,
    })
    print(f"[hf_downloader] done ({final_mb:.1f} МБ)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
