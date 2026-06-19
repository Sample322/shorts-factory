"""Проверяет что OAuth-настройка YouTube сделана правильно.

Запуск:
    .\.venv\Scripts\python.exe verify_youtube_setup.py

Печатает чек-лист с галочками. На каждом шаге если что-то не так — говорит
КОНКРЕТНО что и где.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

OK = "[OK]"
FAIL = "[FAIL]"
WARN = "[!]"

CLIENT_SECRET = Path("secrets/youtube_client_secret.json")
TOKEN = Path("cache/youtube_token.json")


def check_secrets_dir() -> bool:
    if not CLIENT_SECRET.parent.exists():
        print(f"{FAIL} Нет папки {CLIENT_SECRET.parent}/")
        print("     Создаю...")
        CLIENT_SECRET.parent.mkdir(parents=True, exist_ok=True)
        print(f"{OK} Папка создана: {CLIENT_SECRET.parent.resolve()}")
    else:
        print(f"{OK} Папка {CLIENT_SECRET.parent}/ существует")
    return True


def check_client_secret() -> bool:
    if not CLIENT_SECRET.exists():
        print(f"{FAIL} Нет файла {CLIENT_SECRET}")
        print(f"     Положи скачанный JSON от Google Cloud сюда:")
        print(f"     {CLIENT_SECRET.resolve()}")
        return False

    print(f"{OK} Файл {CLIENT_SECRET.name} найден")

    try:
        data = json.loads(CLIENT_SECRET.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"{FAIL} JSON битый: {e}")
        return False

    # Desktop OAuth client имеет ключ 'installed'
    if "installed" not in data:
        if "web" in data:
            print(f"{FAIL} Это OAuth для WEB-app, нужен DESKTOP-app")
            print("     В Google Cloud → Credentials → Create OAuth client ID")
            print("     выбери 'Desktop application', не 'Web application'")
        else:
            print(f"{FAIL} JSON не похож на OAuth client ID")
        return False

    inst = data["installed"]
    required = ["client_id", "client_secret", "auth_uri", "token_uri"]
    missing = [k for k in required if k not in inst]
    if missing:
        print(f"{FAIL} В JSON нет полей: {missing}")
        return False

    cid = inst["client_id"]
    print(f"{OK} JSON валидный (Desktop OAuth)")
    print(f"     client_id: ...{cid[-30:]}")
    return True


def check_token() -> bool:
    if not TOKEN.exists():
        print(f"{WARN} Нет токена {TOKEN} — авторизация ещё не пройдена")
        print("     В UI Shorts Factory нажми кнопку '[lock] Авторизовать YouTube'")
        return False

    try:
        from google.oauth2.credentials import Credentials
        creds = Credentials.from_authorized_user_file(str(TOKEN))
        if creds.valid:
            print(f"{OK} Токен валидный")
        elif creds.refresh_token:
            print(f"{OK} Токен есть, истёк — обновится автоматически при загрузке")
        else:
            print(f"{FAIL} Токен битый — нажми 'Сброс' в UI")
            return False
    except Exception as e:
        print(f"{FAIL} Не могу прочитать токен: {e}")
        return False
    return True


def check_channel_access() -> bool:
    """Делает реальный API-запрос: есть ли канал у пользователя."""
    if not TOKEN.exists():
        return False
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        creds = Credentials.from_authorized_user_file(str(TOKEN))
        yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
        resp = yt.channels().list(part="snippet,statistics", mine=True).execute()
        items = resp.get("items", [])
        if not items:
            print(f"{FAIL} У этого Google-аккаунта НЕТ YouTube-канала.")
            print("     Зайди на youtube.com и создай канал.")
            return False
        ch = items[0]
        title = ch["snippet"]["title"]
        subs = ch["statistics"].get("subscriberCount", "?")
        videos = ch["statistics"].get("videoCount", "?")
        print(f"{OK} Канал: '{title}' (подписчиков: {subs}, видео: {videos})")
        return True
    except Exception as e:
        print(f"{FAIL} Ошибка обращения к YouTube API: {e}")
        return False


def main() -> int:
    print("=" * 60)
    print(" Проверка настройки YouTube auto-upload")
    print("=" * 60)

    if not check_secrets_dir():
        return 1
    print()
    if not check_client_secret():
        print()
        print("=> ОСТАНОВЛЕНО. Сначала создай OAuth client ID:")
        print("   https://console.cloud.google.com/apis/credentials")
        return 1

    print()
    has_token = check_token()
    if has_token:
        print()
        check_channel_access()

    print()
    print("=" * 60)
    if has_token:
        print(" ВСЁ ГОТОВО. Можно включать 'Авто-загрузка' в сайдбаре.")
    else:
        print(" Осталось авторизоваться: нажми 'Авторизовать YouTube' в UI.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
