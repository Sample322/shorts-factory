"""Worker CLI: запускает run_job в отдельном процессе.

Пишет состояние в JSON-файл, который UI читает и отображает.
Это полностью изолирует тяжёлый pipeline от Streamlit:
- UI/браузер можно перезагрузить — pipeline продолжит работу
- WebSocket-таймауты Streamlit не влияют
- Если UI крашится — pipeline переживает
- Можно подключиться к идущей задаче с другого браузера

Usage:
    pythonw -m pipeline.worker --params-file path/to/params.json

Структура params.json:
    {
        "source": "C:\\path\\to\\video.mkv",
        "reframe_mode": "blur",
        "add_subtitles": true,
        "add_music": false,
        "clips_count": 6,
        "target_duration": 35,
        "subtitle_overrides": {...},
        "smart_zoom_out": 0.45,
        "progress_file": "C:\\path\\to\\progress.json"
    }

Структура progress.json (атомарно перезаписывается):
    {
        "status": "running" | "done" | "error",
        "pid": 12345,
        "started_at": 1737030000.0,
        "updated_at": 1737030042.0,
        "stage": "transcribe",
        "stage_label": "Транскрипция (Whisper)",
        "pct_overall": 42.0,
        "label": "Распознано 1240с / 8610с",
        "extra": {...},
        "clips": [...],
        "events": [...],   # последние 200 событий
        "result": {...},   # только когда status=done
        "error": "..."     # только когда status=error
    }
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import traceback
from collections import deque
from pathlib import Path
from threading import Lock

from .render import run_job

# Лимит на размер ленты событий
_EVENT_BUFFER = 200


def _atomic_write_json(path: Path, data: dict) -> None:
    """Атомарная запись JSON через временный файл + os.replace.

    Это критично — UI может читать файл в любой момент. Если писать
    не атомарно, UI может прочитать половину файла и упасть на json.loads.
    """
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--params-file", required=True, type=Path)
    args = parser.parse_args()

    # Делаем stdout/stderr unbuffered и без кодирующих сбоев,
    # чтобы все логи (включая обрывки от CUDA) попадали в лог-файл.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    params = json.loads(args.params_file.read_text(encoding="utf-8"))
    progress_file = Path(params["progress_file"])
    progress_file.parent.mkdir(parents=True, exist_ok=True)

    print(f"[worker] start pid={os.getpid()} params={args.params_file}",
          flush=True)

    write_lock = Lock()
    started_at = time.time()
    state: dict = {
        "status": "running",
        "pid": os.getpid(),
        "started_at": started_at,
        "updated_at": started_at,
        "stage": None,
        "stage_label": None,
        "pct_overall": 0.0,
        "label": "Запуск…",
        "extra": {},
        "clips": [],
        "events": deque(maxlen=_EVENT_BUFFER),
    }

    last_write = 0.0

    def flush(force: bool = False) -> None:
        nonlocal last_write
        now = time.time()
        # Записываем не чаще 4 раз в секунду, кроме force
        if not force and (now - last_write) < 0.25:
            return
        with write_lock:
            snapshot = dict(state)
            snapshot["updated_at"] = now
            snapshot["events"] = list(state["events"])
            try:
                _atomic_write_json(progress_file, snapshot)
                last_write = now
            except OSError:
                pass  # UI прочитает в следующий раз

    def cb(event: dict) -> None:
        with write_lock:
            t = event.get("type")
            state["events"].append(event)

            if t == "stage":
                state["stage"] = event["stage"]
                state["stage_label"] = _stage_label(event["stage"])
                state["pct_overall"] = event["pct_overall"]
                state["label"] = event["label"]
                state["extra"] = {}

            elif t == "substage":
                state["stage"] = event["stage"]
                state["stage_label"] = _stage_label(event["stage"])
                state["pct_overall"] = event["pct_overall"]
                state["label"] = event["label"]
                state["extra"] = event.get("extra", {})

            elif t == "plan":
                state["clips"] = [
                    {**c, "n": len(event["clips"]), "status": "pending"}
                    for c in event["clips"]
                ]

            elif t == "clip_start":
                for c in state["clips"]:
                    if c.get("i") == event["i"]:
                        c["status"] = "active"
                        c["substep"] = "запуск"

            elif t == "clip_substep":
                for c in state["clips"]:
                    if c.get("i") == event["i"]:
                        c["substep"] = event["label"]

            elif t == "clip_done":
                for c in state["clips"]:
                    if c.get("i") == event["i"]:
                        c["status"] = "done"
                        c["substep"] = ""
                        c["file"] = event.get("file")

            elif t == "clip_failed":
                for c in state["clips"]:
                    if c.get("i") == event["i"]:
                        c["status"] = "failed"
                        c["reason"] = event.get("reason", "")

            elif t == "music_pick_required":
                state["awaiting_music_pick"] = True
                state["music_variants"] = event["variants"]
                state["label"] = "Жду выбор музыки от пользователя"

            elif t == "music_picked" or t == "music_skipped":
                state["awaiting_music_pick"] = False
                state.pop("music_variants", None)

            elif t == "done":
                state["status"] = "done"
                state["pct_overall"] = 100.0
                state["label"] = "Готово!"
                state["result"] = event["meta"]

            elif t == "error":
                state["status"] = "error"
                state["error"] = event.get("message", "")
                state["label"] = f"Ошибка: {event.get('message', '?')[:80]}"

        # Критичные события (которые меняют state в "точку ожидания/финала")
        # форсим ЗАПИСЬ — иначе они могут потеряться из-за 250мс throttling,
        # если worker сразу уходит в долгое ожидание (как _wait_for_music_choice
        # на 20 минут — за это время следующий flush не успеет случиться).
        _CRITICAL = {"stage", "music_pick_required", "music_picked", "music_skipped",
                     "plan", "done", "error"}
        flush(force=(t in _CRITICAL))

    try:
        flush(force=True)
        meta = run_job(
            source=params["source"],
            reframe_mode=params.get("reframe_mode", "blur"),
            add_subtitles=params.get("add_subtitles", True),
            add_music=params.get("add_music", False),
            progress_cb=cb,
            clips_count=params.get("clips_count"),
            target_duration=params.get("target_duration"),
            subtitle_overrides=params.get("subtitle_overrides"),
            smart_zoom_out=params.get("smart_zoom_out"),
            youtube_upload=params.get("youtube_upload", False),
            youtube_privacy=params.get("youtube_privacy", "unlisted"),
            youtube_publish_at=params.get("youtube_publish_at"),
            youtube_source_context=params.get("youtube_source_context", ""),
            tiktok_upload=params.get("tiktok_upload", False),
            tiktok_privacy=params.get("tiktok_privacy", "SELF_ONLY"),
            tiktok_disable_comment=params.get("tiktok_disable_comment", False),
            tiktok_disable_duet=params.get("tiktok_disable_duet", False),
            tiktok_disable_stitch=params.get("tiktok_disable_stitch", False),
            tiktok_is_aigc=params.get("tiktok_is_aigc", False),
            cut_silences=params.get("cut_silences", True),
            silence_min_sec=params.get("silence_min_sec", 0.5),
            silence_threshold_db=params.get("silence_threshold_db", -30.0),
            silence_padding_sec=params.get("silence_padding_sec", 0.12),
            color_grade=params.get("color_grade", True),
            generate_music=params.get("generate_music", False),
            music_duration_sec=params.get("music_duration_sec", 30.0),
            music_n_variants=params.get("music_n_variants", 3),
            music_custom_hint=params.get("music_custom_hint", ""),
            music_decision_file=params.get("music_decision_file"),
            music_wait_timeout_sec=params.get("music_wait_timeout_sec", 600),
            music_volume=params.get("music_volume"),
            music_lora_repo=params.get("music_lora_repo"),
            music_lora_weight=float(params.get("music_lora_weight", 0.0)),
            vocal_isolation_enabled=params.get("vocal_isolation_enabled"),
            speed_enabled=params.get("speed_enabled", False),
            speed_factor=float(params.get("speed_factor", 1.0)),
            watermark_enabled=params.get("watermark_enabled", False),
            thumbnail_enabled=params.get("thumbnail_enabled", False),
        )
        with write_lock:
            if state["status"] != "error":
                state["status"] = "done"
                state["pct_overall"] = 100.0
                state["label"] = "Готово!"
                state["result"] = meta
        flush(force=True)
        return 0
    except BaseException as exc:  # noqa: BLE001 — ловим даже SystemExit/KeyboardInterrupt
        tb = traceback.format_exc()
        print(f"[worker] EXCEPTION: {type(exc).__name__}: {exc}\n{tb}",
              file=sys.stderr, flush=True)
        try:
            with write_lock:
                state["status"] = "error"
                state["error"] = f"{type(exc).__name__}: {exc}"
                state["traceback"] = tb
                state["label"] = f"Ошибка: {str(exc)[:80]}"
            flush(force=True)
        except Exception as write_err:
            print(f"[worker] cannot write progress: {write_err}",
                  file=sys.stderr, flush=True)
        return 1
    finally:
        print("[worker] exit", flush=True)


def _stage_label(stage_id: str) -> str:
    labels = {
        "ingest":     "Приём видео",
        "extract":    "Извлечение аудио",
        "transcribe": "Транскрипция (Whisper)",
        "analyze":    "Анализ моментов (LLM)",
        "clips":      "Сборка клипов",
        "finalize":   "Финализация",
    }
    return labels.get(stage_id, stage_id)


if __name__ == "__main__":
    sys.exit(main())
