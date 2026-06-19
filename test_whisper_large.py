"""Тест large-v3 с int8 и batch_size=1 — минимум VRAM."""
import os
import sys
import gc
import traceback

os.environ["PATH"] = r"C:\ffmpeg\bin" + os.pathsep + os.environ.get("PATH", "")

log_path = os.path.join(os.path.dirname(__file__), "logs", "whisper_large_test.log")
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

print("=== WhisperX large-v3 int8 Test ===")

try:
    import torch
    print(f"PyTorch: {torch.__version__}, CUDA: {torch.cuda.is_available()}")
    if torch.cuda.is_available():
        print(f"GPU: {torch.cuda.get_device_name(0)}")
        props = torch.cuda.get_device_properties(0)
        print(f"VRAM: {props.total_memory / 1024**3:.1f} GB")
        # Очищаем VRAM перед стартом
        torch.cuda.empty_cache()
        free, total = torch.cuda.mem_get_info()
        print(f"Free VRAM: {free / 1024**3:.1f} GB / {total / 1024**3:.1f} GB")

    import whisperx
    print("WhisperX imported")

    video = sys.argv[1] if len(sys.argv) > 1 else None
    if not video:
        print("Usage: python test_whisper_large.py <video>")
        sys.exit(1)

    print(f"\nLoading audio: {video}")
    sys.stdout.flush()
    audio = whisperx.load_audio(video)
    # Только первые 60 секунд для быстрого теста
    clip_secs = 60
    audio_clip = audio[: clip_secs * 16000]
    print(f"Audio: {len(audio_clip) / 16000:.0f}s (first {clip_secs}s)")
    sys.stdout.flush()

    print("\nLoading large-v3 (int8, CUDA)...")
    sys.stdout.flush()
    model = whisperx.load_model(
        "large-v3",
        device="cuda",
        compute_type="int8",
        language="ru",
    )
    free, total = torch.cuda.mem_get_info()
    print(f"Model loaded! Free VRAM: {free / 1024**3:.1f} GB")
    sys.stdout.flush()

    print("Transcribing (batch_size=1)...")
    sys.stdout.flush()
    result = model.transcribe(audio_clip, batch_size=1, language="ru")
    print(f"Got {len(result.get('segments', []))} segments")
    for s in result.get("segments", [])[:5]:
        print(f"  [{s['start']:.1f}-{s['end']:.1f}] {s['text']}")
    sys.stdout.flush()

    # Free model VRAM
    del model
    gc.collect()
    torch.cuda.empty_cache()

    print("\nAligning words...")
    sys.stdout.flush()
    align_model, metadata = whisperx.load_align_model(
        language_code=result["language"], device="cuda"
    )
    aligned = whisperx.align(
        result["segments"], align_model, metadata, audio_clip, "cuda",
        return_char_alignments=False,
    )
    print(f"Aligned: {len(aligned.get('word_segments', []))} words")

    del align_model
    gc.collect()
    torch.cuda.empty_cache()

    print("\n=== SUCCESS ===")

except Exception as e:
    print(f"\n=== ERROR ===")
    traceback.print_exc()
finally:
    log_file.close()
    input("\nPress Enter...")
