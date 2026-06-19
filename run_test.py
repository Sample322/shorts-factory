"""Тестовый прогон pipeline без Streamlit — логи пишутся в файл и консоль."""
import sys
import os
import traceback

# Гарантируем FFmpeg и cuDNN в PATH
_venv = os.path.dirname(os.path.dirname(sys.executable))  # .venv
_cudnn_bin = os.path.join(_venv, "Lib", "site-packages", "nvidia", "cudnn", "bin")
_cublas_bin = os.path.join(_venv, "Lib", "site-packages", "nvidia", "cublas", "bin")
for _d in [r"C:\ffmpeg\bin", _cudnn_bin, _cublas_bin]:
    if _d not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _d + os.pathsep + os.environ.get("PATH", "")

# Перенаправляем вывод в файл для отладки
log_path = os.path.join(os.path.dirname(__file__), "logs", "test_run.log")
os.makedirs(os.path.dirname(log_path), exist_ok=True)


class Tee:
    def __init__(self, *files):
        self.files = files

    def write(self, data):
        for f in self.files:
            try:
                f.write(data)
                f.flush()
            except Exception:
                pass

    def flush(self):
        for f in self.files:
            try:
                f.flush()
            except Exception:
                pass


log_file = open(log_path, "w", encoding="utf-8")
sys.stdout = Tee(sys.__stdout__, log_file)
sys.stderr = Tee(sys.__stderr__, log_file)

print("=== Shorts Factory Test Run ===")
print(f"Log: {log_path}")
print(f"Python: {sys.executable}")
print(f"CWD: {os.getcwd()}")

try:
    import torch

    print(f"PyTorch: {torch.__version__}, CUDA: {torch.cuda.is_available()}")
    if torch.cuda.is_available():
        print(f"GPU: {torch.cuda.get_device_name(0)}")
        props = torch.cuda.get_device_properties(0)
        total = props.total_memory / 1024**3
        print(f"VRAM: {total:.1f} GB")
except Exception as e:
    print(f"PyTorch check failed: {e}")

try:
    from pipeline.render import run_job

    video = sys.argv[1] if len(sys.argv) > 1 else None
    if not video:
        print("Usage: python run_test.py <path_to_video> [mode] [clips_count]")
        sys.exit(1)

    print(f"\nVideo: {video}")
    print(f"File exists: {os.path.exists(video)}")
    print(f"File size: {os.path.getsize(video) / 1024**2:.0f} MB")
    print(f"\nStarting pipeline...")

    def progress(msg, pct):
        line = f"[{pct}%] {msg}" if pct else msg
        print(line)

    mode = sys.argv[2] if len(sys.argv) > 2 else "smart"
    clips_count = int(sys.argv[3]) if len(sys.argv) > 3 else None
    print(f"Reframe mode: {mode}")
    if clips_count:
        print(f"Clips count: {clips_count}")

    meta = run_job(
        video,
        reframe_mode=mode,
        add_subtitles=True,
        add_music=False,
        progress_cb=progress,
        clips_count=clips_count,
    )

    print(f"\n=== DONE ===")
    print(f"Job ID: {meta['job_id']}")
    print(f"Clips: {len(meta['clips'])}")
    for c in meta["clips"]:
        print(f"  - {c['title']} ({c['duration']}s)")
except Exception as e:
    print(f"\n=== ERROR ===")
    traceback.print_exc()
finally:
    log_file.close()
    input("\nPress Enter to close...")
