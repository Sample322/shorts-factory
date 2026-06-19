"""Quick test: cuDNN DLL available?"""
import os
import sys

_venv = os.path.dirname(os.path.dirname(sys.executable))
_cudnn_bin = os.path.join(_venv, "Lib", "site-packages", "nvidia", "cudnn", "bin")
_cublas_bin = os.path.join(_venv, "Lib", "site-packages", "nvidia", "cublas", "bin")
for _d in [r"C:\ffmpeg\bin", _cudnn_bin, _cublas_bin]:
    if _d not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _d + os.pathsep + os.environ.get("PATH", "")

print(f"cuDNN bin: {_cudnn_bin}")
print(f"Exists: {os.path.exists(_cudnn_bin)}")
dll = os.path.join(_cudnn_bin, "cudnn_ops_infer64_8.dll")
print(f"DLL exists: {os.path.exists(dll)}")

import ctypes
try:
    lib = ctypes.CDLL(dll)
    print(f"cuDNN DLL loaded OK: {lib}")
except Exception as e:
    print(f"Failed to load cuDNN: {e}")

print("\nLoading ctranslate2...")
import ctranslate2
print(f"ctranslate2 {ctranslate2.__version__} loaded OK")
print(f"CUDA: {ctranslate2.get_cuda_device_count()} devices")

print("\nLoading whisperx model (medium, int8, cuda)...")
import whisperx
model = whisperx.load_model("medium", device="cuda", compute_type="int8", language="ru")
print("Model loaded successfully!")

video = sys.argv[1] if len(sys.argv) > 1 else None
if video:
    print(f"\nLoading audio: {video}")
    audio = whisperx.load_audio(video)
    clip = audio[:30 * 16000]
    print(f"Transcribing {len(clip)/16000:.0f}s...")
    result = model.transcribe(clip, batch_size=2, language="ru")
    print(f"Got {len(result.get('segments',[]))} segments")
    for s in result.get("segments", [])[:3]:
        print(f"  [{s['start']:.1f}-{s['end']:.1f}] {s['text']}")
    print("\n=== SUCCESS ===")
else:
    print("\nNo video provided, but model loaded OK!")
    print("=== SUCCESS ===")
