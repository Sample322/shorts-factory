"""Pre-flight checks: что готово/не готово перед запуском завода.

Каждый check возвращает (ok: bool, label: str, detail: str).
UI отображает зелёные/красные галочки.
"""

from __future__ import annotations

import shutil
from pathlib import Path


def check_kimi(cfg: dict) -> tuple[bool, str, str]:
    k = cfg.get("kimi", {})
    if not k.get("enabled"):
        return False, "Kimi LLM", "выключен в config (выкл = пойдёт по chain дальше)"
    if not k.get("api_key"):
        return False, "Kimi LLM", "нет api_key (положи в config.local.yaml)"
    return True, "Kimi LLM", f"включён, model={k.get('model', '?')}"


def check_ollama() -> tuple[bool, str, str]:
    try:
        import ollama
        cli = ollama.Client(host="http://localhost:11434")
        resp = cli.list()
        models = resp.get("models", []) if isinstance(resp, dict) else getattr(resp, "models", [])
        n = len(models)
        if n == 0:
            return False, "Ollama fallback", "сервер отвечает, но 0 моделей (mojibake?)"
        return True, "Ollama fallback", f"{n} моделей доступно"
    except Exception as e:
        return False, "Ollama fallback", f"сервер не отвечает: {str(e)[:80]}"


def check_youtube_token() -> tuple[bool, str, str]:
    token = Path("cache/youtube_token.json")
    if not token.exists():
        return False, "YouTube токен", "нет файла (авторизуйся через UI)"
    try:
        from google.oauth2.credentials import Credentials
        creds = Credentials.from_authorized_user_file(str(token))
        if creds.expired and not creds.refresh_token:
            return False, "YouTube токен", "истёк, нет refresh — авторизуйся заново"
        return True, "YouTube токен", "валиден"
    except Exception as e:
        return False, "YouTube токен", f"битый: {str(e)[:60]}"


def check_tiktok_creds() -> tuple[bool, str, str]:
    client = Path("secrets/tiktok_client.json")
    token = Path("cache/tiktok_token.json")
    if not client.exists():
        return False, "TikTok creds", "secrets/tiktok_client.json отсутствует"
    if not token.exists():
        return False, "TikTok токен", "не авторизован (см. tiktok_setup.md)"
    return True, "TikTok creds + токен", "готово"


def check_vram(min_gb: float = 6.0) -> tuple[bool, str, str]:
    try:
        import torch
        if not torch.cuda.is_available():
            return False, "GPU/VRAM", "CUDA не доступна"
        free_gb = torch.cuda.mem_get_info()[0] / 1024**3
        total_gb = torch.cuda.get_device_properties(0).total_memory / 1024**3
        ok = free_gb >= min_gb
        return ok, "GPU/VRAM", f"{free_gb:.1f} / {total_gb:.1f} GB свободно"
    except Exception as e:
        return False, "GPU/VRAM", f"проверка упала: {e}"


def check_disk_space(min_gb: float = 10.0, path: str = ".") -> tuple[bool, str, str]:
    try:
        free_gb = shutil.disk_usage(path).free / 1024**3
        ok = free_gb >= min_gb
        return ok, "Диск", f"{free_gb:.0f} GB свободно"
    except Exception as e:
        return False, "Диск", f"проверка упала: {e}"


def check_ace_step_checkpoint() -> tuple[bool, str, str]:
    ckpt = Path("cache/ace-step-models")
    if not ckpt.exists():
        return False, "ACE-Step checkpoint", "не скачан (~7 GB; скачается при первом запуске)"
    files = list(ckpt.rglob("*.safetensors"))
    if not files:
        return False, "ACE-Step checkpoint", "директория есть но weights нет"
    total_gb = sum(f.stat().st_size for f in files) / 1024**3
    return True, "ACE-Step checkpoint", f"{total_gb:.1f} GB готов"


def run_all_checks(cfg: dict) -> list[tuple[bool, str, str]]:
    """Все проверки разом. Используется в UI перед запуском завода."""
    return [
        check_kimi(cfg),
        check_ollama(),
        check_youtube_token(),
        check_tiktok_creds(),
        check_vram(min_gb=6.0),
        check_disk_space(min_gb=10.0),
        check_ace_step_checkpoint(),
    ]
