"""ACE-Step 1.5 backend через isolated venv subprocess.

ACE-Step имеет жёсткие пины (diffusers==0.32.2, transformers==4.50.0,
gradio 5.x, spacy 3.8.4, japanese/korean deps) которые ломают main venv
(faster-whisper, stable_audio_open). Поэтому он живёт в C:\\shorts-factory\\
.venv-acestep и вызывается через subprocess + JSON protocol.

Worker entry point: tools/acestep_worker.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

from ..utils import get_logger
from .base import MusicVariant

log = get_logger("music_gen.ace")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ACE_VENV_PYTHON = PROJECT_ROOT / ".venv-acestep" / "Scripts" / "python.exe"
ACE_WORKER = PROJECT_ROOT / "tools" / "acestep_worker.py"


class ACEStepBackend:
    """Реализует MusicBackend через isolated venv subprocess."""

    name = "ace_step"

    def is_available(self) -> tuple[bool, str]:
        # Проверка отдельного venv
        if not ACE_VENV_PYTHON.exists():
            return False, (
                f"Нет isolated venv для ACE-Step ({ACE_VENV_PYTHON}). "
                "Запусти: python -m venv .venv-acestep && "
                ".venv-acestep\\Scripts\\pip install ace-step torch --index-url "
                "https://download.pytorch.org/whl/cu121"
            )

        if not ACE_WORKER.exists():
            return False, f"Worker отсутствует: {ACE_WORKER}"

        # Проверяем что acestep + torch CUDA реально установлены в venv
        try:
            r = subprocess.run(
                [
                    str(ACE_VENV_PYTHON), "-c",
                    "import torch, acestep; "
                    "import sys; "
                    "sys.exit(0 if torch.cuda.is_available() else 2)",
                ],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode == 2:
                return False, "ACE-Step venv не видит CUDA"
            if r.returncode != 0:
                return False, f"ACE-Step venv битый: {r.stderr.strip()[:200]}"
        except subprocess.TimeoutExpired:
            return False, "ACE-Step venv не отвечает (timeout)"
        except OSError as e:
            return False, f"Не могу запустить ACE-Step venv: {e}"

        # VRAM check — через текущий torch (main venv)
        try:
            import torch
            if torch.cuda.is_available():
                free_gb = torch.cuda.mem_get_info()[0] / 1024**3
                if free_gb < 6:
                    return False, f"Мало свободной VRAM ({free_gb:.1f} ГБ < 6 ГБ)"
        except ImportError:
            pass

        return True, "OK"

    def generate_variants(
        self,
        out_dir: Path,
        mood: str,
        duration_sec: float = 30.0,
        n_variants: int = 3,
        custom_hint: str = "",
        base_seed: int | None = None,
        progress_cb: Callable[[str, float], None] | None = None,
        lora_repo: str | None = None,
        lora_weight: float = 0.0,
        start_index: int = 1,
    ) -> list[MusicVariant]:
        out_dir.mkdir(parents=True, exist_ok=True)

        params = {
            "out_dir": str(out_dir),
            "mood": mood,
            "duration_sec": float(duration_sec),
            "n_variants": int(n_variants),
            "custom_hint": custom_hint,
            "base_seed": base_seed,
            "lora_repo": lora_repo,
            "lora_weight": float(lora_weight),
            "start_index": int(start_index),
        }
        stdin_payload = json.dumps(params, ensure_ascii=False)

        if progress_cb:
            progress_cb("loading", 0)

        log.info(
            f"ACE-Step subprocess: mood={mood}, duration={duration_sec}с, "
            f"n={n_variants}"
        )
        env = os.environ.copy()
        # Чтобы UTF-8 промпты не ломались на Windows
        env["PYTHONIOENCODING"] = "utf-8"

        try:
            r = subprocess.run(
                [str(ACE_VENV_PYTHON), str(ACE_WORKER)],
                input=stdin_payload,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=900,  # 15 минут на 3 варианта
                env=env,
                cwd=str(PROJECT_ROOT),
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("ACE-Step worker завис (>15 мин). Проверь GPU.")

        # stderr содержит [acestep_worker] лог
        if r.stderr:
            for line in r.stderr.splitlines():
                if line.strip():
                    log.info(f"  {line}")

        if r.returncode != 0:
            raise RuntimeError(
                f"ACE-Step worker упал (exit {r.returncode}). См. лог выше."
            )

        if progress_cb:
            progress_cb("generating", 100)
            progress_cb("saving", 0)

        try:
            payload = json.loads(r.stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"ACE-Step worker вернул битый JSON: {e}. stdout={r.stdout[:500]}"
            )

        variants: list[MusicVariant] = []
        for item in payload.get("variants", []):
            variants.append(MusicVariant(
                index=item["index"],
                path=Path(item["path"]),
                preview_path=Path(item["preview_path"]),
                prompt=item["prompt"],
                seed=item["seed"],
                duration_sec=item["duration_sec"],
                mood=item["mood"],
            ))

        if progress_cb:
            progress_cb("saving", 100)

        return variants

    def unload(self) -> None:
        # Subprocess после выхода сам освобождает VRAM — нечего unload-ить
        pass
