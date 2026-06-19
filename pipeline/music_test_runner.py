"""CLI для тест-генерации одного трека из subprocess.

Не блокирует Streamlit. Пишет результат в status.json.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path


def _atomic_write(path: Path, data: dict) -> None:
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status-file", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--mood", default="upbeat")
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--steps", type=int, default=100)
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    started = time.time()
    _atomic_write(args.status_file, {
        "status": "loading", "pct": 0,
        "label": "Загружаю модель в VRAM...",
        "started_at": started, "updated_at": started,
    })

    try:
        from pipeline.music_gen import (
            generate_variants, unload_pipeline, is_model_available,
        )

        ok, reason = is_model_available()
        if not ok:
            _atomic_write(args.status_file, {
                "status": "error", "error": reason,
                "started_at": started, "updated_at": time.time(),
            })
            return 1

        def cb(stg: str, pct: float) -> None:
            stage_map = {"loading": 5, "generating": 30, "saving": 95}
            base = stage_map.get(stg, 0)
            overall = min(99, int(base + pct * 0.7))
            _atomic_write(args.status_file, {
                "status": "running", "stage": stg, "pct": overall,
                "label": f"{stg}: {pct:.0f}%",
                "started_at": started, "updated_at": time.time(),
            })

        variants = generate_variants(
            args.output_dir,
            mood=args.mood,
            duration_sec=args.duration,
            n_variants=1,
            num_inference_steps=args.steps,
            progress_cb=cb,
        )
        unload_pipeline()

        elapsed = time.time() - started
        _atomic_write(args.status_file, {
            "status": "done", "pct": 100,
            "label": f"Готово за {elapsed:.0f}с",
            "file": str(variants[0].path.resolve()),
            "elapsed_sec": round(elapsed, 1),
            "started_at": started, "updated_at": time.time(),
        })
        return 0
    except Exception as exc:
        _atomic_write(args.status_file, {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
            "started_at": started, "updated_at": time.time(),
        })
        return 1


if __name__ == "__main__":
    sys.exit(main())
