"""Минимальный тест WhisperX — изолируем проблему."""
import os
import sys
import traceback

os.environ["PATH"] = r"C:\ffmpeg\bin" + os.pathsep + os.environ.get("PATH", "")

print("=== Minimal WhisperX Test ===")
print(f"Python: {sys.executable}")

try:
    import torch
    print(f"PyTorch: {torch.__version__}")
    print(f"CUDA: {torch.cuda.is_available()}")
    if torch.cuda.is_available():
        print(f"GPU: {torch.cuda.get_device_name(0)}")
        props = torch.cuda.get_device_properties(0)
        print(f"VRAM: {props.total_memory / 1024**3:.1f} GB")

    import whisperx
    print(f"WhisperX imported OK")

    # Используем короткий фрагмент — первые 30 секунд
    video = sys.argv[1] if len(sys.argv) > 1 else None
    if not video:
        print("Usage: python test_whisper_minimal.py <video>")
        sys.exit(1)

    print(f"\nLoading audio from: {video}")
    audio = whisperx.load_audio(video)
    # Берём только первые 30 секунд (16000 samples/sec)
    audio_short = audio[: 30 * 16000]
    print(f"Audio loaded: {len(audio_short) / 16000:.0f} sec (trimmed to 30s)")

    print("\nLoading model (medium, int8)...")
    model = whisperx.load_model(
        "medium",
        device="cuda",
        compute_type="int8",
        language="ru",
    )
    print("Model loaded!")

    print("Transcribing 30s clip...")
    result = model.transcribe(audio_short, batch_size=2, language="ru")
    print(f"Transcription OK: {len(result.get('segments', []))} segments")
    for s in result.get("segments", [])[:3]:
        print(f"  [{s['start']:.1f}-{s['end']:.1f}] {s['text']}")

    print("\n=== SUCCESS ===")

except Exception as e:
    print(f"\n=== ERROR ===")
    traceback.print_exc()

input("\nPress Enter...")
