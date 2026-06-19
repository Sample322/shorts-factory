"""Скачивает чекпойнт ACE-Step v1-3.5B (~7GB) в ASCII-путь, плоская структура.

ACE-Step pipeline ожидает: <checkpoint_dir>/music_dcae_f8c8/,
<checkpoint_dir>/music_vocoder/, <checkpoint_dir>/ace_step_transformer/,
<checkpoint_dir>/umt5-base/. Поэтому используем local_dir + symlinks для
плоского layout.

Использует hf_transfer для multi-connection resume.
"""

import os
import sys
from pathlib import Path

os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "1"

from huggingface_hub import snapshot_download

REPO_ID = "ACE-Step/ACE-Step-v1-3.5B"
LOCAL_DIR = Path(r"C:\shorts-factory\cache\ace-step-models")


def main() -> int:
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Скачиваю {REPO_ID} → {LOCAL_DIR} (~7 GB)", flush=True)
    try:
        path = snapshot_download(
            REPO_ID,
            local_dir=str(LOCAL_DIR),
            max_workers=4,
        )
        print(f"OK: {path}", flush=True)
        return 0
    except Exception as e:
        print(f"FAIL: {e}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
