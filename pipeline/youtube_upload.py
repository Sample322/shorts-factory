"""Загрузка готовых клипов на YouTube через Data API v3.

Архитектура
-----------
1. **OAuth 2.0 Desktop flow** (InstalledAppFlow). Первая загрузка открывает
   браузер для consent → сохраняет token в `cache/youtube_token.json`.
   Дальше токен авто-обновляется (refresh_token).

2. **Resumable upload** через MediaFileUpload(chunksize=...) — устойчив
   к обрывам сети, прогресс репортуется в callback.

3. **Поля snippet/status** по чеклисту 2025: selfDeclaredMadeForKids=False,
   notifySubscribers — настраиваемо, categoryId 22 (People & Blogs) по умолч.

Setup для пользователя (см. youtube_setup.md):
    1. console.cloud.google.com → создать проект
    2. Enable "YouTube Data API v3"
    3. OAuth consent screen → External, Testing, scope youtube.upload
    4. Credentials → Create OAuth client ID → Desktop application
    5. Download JSON → положить в `secrets/youtube_client_secret.json`
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from .utils import get_logger

log = get_logger("youtube")

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
CLIENT_SECRET_PATH = Path("secrets/youtube_client_secret.json")
TOKEN_PATH = Path("cache/youtube_token.json")

# Лимиты от YouTube Data API v3
MAX_TITLE_LEN = 100
MAX_DESCRIPTION_LEN = 5000
MAX_TAGS_TOTAL_LEN = 500   # суммарно по всем тегам
MAX_SINGLE_TAG_LEN = 30

# ───────────────────── Daily quota tracker ─────────────────────
# YouTube без advanced verification ограничивает: ~15 shorts/день каналу,
# плюс Data API quota = 10000 units/день = ~6 uploads (1600 unit/upload).
# Лимитирующий = API quota, поэтому ставим 6 как hard cap.
# Сброс — в 00:00 Pacific Time (так YouTube/Google квоты считают).
DAILY_UPLOAD_LIMIT = 6
QUOTA_FILE = Path("cache/youtube_quota.json")


class QuotaExceeded(Exception):
    """Дневной лимит upload'ов исчерпан. Жди завтра."""


class AuthRequired(Exception):
    """Токен мёртв (refresh не сработал). Нужна re-authorization через UI."""


def _today_pt_str() -> str:
    """Текущая дата в Pacific Time (часовом поясе квоты YouTube)."""
    from datetime import datetime, timedelta, timezone
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
    except Exception:
        # Fallback если tzdata нет: UTC-8 (PST). На DST разница в 1 ч ОК
        # для нашей задачи (мы не считаем точные миллисекунды).
        return (datetime.now(timezone.utc) + timedelta(hours=-8)).strftime("%Y-%m-%d")


def _load_quota() -> dict:
    today = _today_pt_str()
    if not QUOTA_FILE.exists():
        return {"date": today, "used": 0}
    try:
        data = json.loads(QUOTA_FILE.read_text(encoding="utf-8"))
        if data.get("date") != today:
            return {"date": today, "used": 0}
        # Защита от мусора в файле
        data["used"] = max(0, int(data.get("used", 0)))
        return data
    except Exception:
        return {"date": today, "used": 0}


def _save_quota(data: dict) -> None:
    QUOTA_FILE.parent.mkdir(parents=True, exist_ok=True)
    QUOTA_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def get_quota_status() -> tuple[int, int]:
    """Возвращает (использовано, лимит) на сегодня по Pacific Time."""
    q = _load_quota()
    return q["used"], DAILY_UPLOAD_LIMIT


def check_quota_available() -> bool:
    used, limit = get_quota_status()
    return used < limit


def _increment_quota() -> int:
    """Атомарно инкрементит use. Возвращает новое значение."""
    q = _load_quota()
    q["used"] = q["used"] + 1
    _save_quota(q)
    return q["used"]


def reset_quota() -> None:
    """Принудительный сброс (для тестов / если случайно списалось)."""
    _save_quota({"date": _today_pt_str(), "used": 0})


@dataclass
class VideoMetadata:
    """Метаданные одного клипа для загрузки."""

    title: str
    description: str
    tags: list[str] = field(default_factory=list)
    category_id: str = "22"          # People & Blogs — самая безопасная по умолч.
    default_language: str = "ru"
    default_audio_language: str = "ru"
    privacy_status: str = "public"   # "public" | "unlisted" | "private"
    self_declared_made_for_kids: bool = False
    notify_subscribers: bool = True
    publish_at: str | None = None    # ISO 8601, требует privacy_status="private"

    def sanitize(self) -> "VideoMetadata":
        """Обрезает поля по лимитам API, возвращает новую копию."""
        title = self.title.strip()[:MAX_TITLE_LEN]
        # YouTube ругается на угловые скобки в title — обычная нагрузка от LLM
        title = title.replace("<", "").replace(">", "")

        description = self.description.strip()[:MAX_DESCRIPTION_LEN]

        # Теги: каждый ≤30 симв, суммарно ≤500
        cleaned_tags: list[str] = []
        total = 0
        for t in self.tags:
            t = t.strip().replace(",", " ")[:MAX_SINGLE_TAG_LEN]
            if not t:
                continue
            # "длина с кавычками" если есть пробелы
            cost = len(t) + (2 if " " in t else 0) + (1 if cleaned_tags else 0)
            if total + cost > MAX_TAGS_TOTAL_LEN:
                break
            cleaned_tags.append(t)
            total += cost

        return VideoMetadata(
            title=title or "Untitled",
            description=description,
            tags=cleaned_tags,
            category_id=self.category_id,
            default_language=self.default_language,
            default_audio_language=self.default_audio_language,
            privacy_status=self.privacy_status,
            self_declared_made_for_kids=self.self_declared_made_for_kids,
            notify_subscribers=self.notify_subscribers,
            publish_at=self.publish_at,
        )


# ---------- Auth ----------

def is_setup_complete() -> bool:
    """True если есть client_secret.json от Google Cloud."""
    return CLIENT_SECRET_PATH.exists()


def is_authorized() -> bool:
    """True если уже есть валидный токен в кеше."""
    if not TOKEN_PATH.exists():
        return False
    try:
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        return bool(creds and (creds.valid or creds.refresh_token))
    except Exception:
        return False


def get_credentials(
    interactive: bool = True, port: int = 8765
) -> Credentials:
    """Возвращает валидные OAuth-креды, обновляя/запрашивая по необходимости.

    interactive=False: только использует существующий токен, без браузера.
    """
    creds: Credentials | None = None

    if TOKEN_PATH.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        except Exception as e:
            log.warning(f"Битый token.json, удаляю: {e}")
            try:
                TOKEN_PATH.unlink()
            except OSError:
                pass
            creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(creds)
            return creds
        except Exception as e:
            err_text = str(e).lower()
            log.warning(f"refresh не удался: {e}")
            # invalid_grant = токен отозван/протух → файл бесполезен,
            # удаляем чтобы не пытаться его читать снова в этом же джобе.
            if "invalid_grant" in err_text or "revoked" in err_text or "expired" in err_text:
                try:
                    TOKEN_PATH.unlink()
                    log.info("Мёртвый token.json удалён")
                except OSError:
                    pass
            creds = None

    if not interactive:
        raise AuthRequired(
            "YouTube токен мёртв (Testing mode = 7 дней TTL). "
            "Открой UI → сайдбар → «🔐 Авторизовать YouTube» заново."
        )

    if not CLIENT_SECRET_PATH.exists():
        raise FileNotFoundError(
            f"Нет файла {CLIENT_SECRET_PATH}. Сначала создай OAuth client ID "
            "в Google Cloud Console и положи JSON туда. "
            "Подробности: см. youtube_setup.md"
        )

    flow = InstalledAppFlow.from_client_secrets_file(
        str(CLIENT_SECRET_PATH), SCOPES
    )
    # run_local_server откроет браузер на http://localhost:{port}/
    creds = flow.run_local_server(port=port, open_browser=True)
    _save_token(creds)
    return creds


def _save_token(creds: Credentials) -> None:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")


def revoke() -> None:
    """Удаляет локальный токен — следующая загрузка попросит авторизацию."""
    if TOKEN_PATH.exists():
        TOKEN_PATH.unlink()


# ---------- Upload ----------

@dataclass
class UploadResult:
    video_id: str
    url: str
    title: str


def upload_clip(
    video_path: Path,
    metadata: VideoMetadata,
    thumbnail_path: Path | None = None,
    progress_cb: Callable[[float], None] | None = None,
) -> UploadResult:
    """Загружает один MP4 на YouTube. Блокирующий вызов.

    progress_cb(pct: float 0-100) репортуется по мере загрузки чанков.
    """
    if not video_path.exists():
        raise FileNotFoundError(video_path)

    # Hard cap дневной квоты — ДО любой работы с API.
    used, limit = get_quota_status()
    if used >= limit:
        raise QuotaExceeded(
            f"YouTube квота на сегодня исчерпана: {used}/{limit} загрузок. "
            f"Сброс в 00:00 по тихоокеанскому времени."
        )

    meta = metadata.sanitize()
    creds = get_credentials(interactive=False)
    # cache_discovery=False — иначе на Windows ругается на permissions /tmp
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)

    body = {
        "snippet": {
            "title": meta.title,
            "description": meta.description,
            "tags": meta.tags,
            "categoryId": meta.category_id,
            "defaultLanguage": meta.default_language,
            "defaultAudioLanguage": meta.default_audio_language,
        },
        "status": {
            "privacyStatus": meta.privacy_status,
            "selfDeclaredMadeForKids": meta.self_declared_made_for_kids,
            "embeddable": True,
            "publicStatsViewable": True,
        },
    }
    if meta.publish_at and meta.privacy_status == "private":
        body["status"]["publishAt"] = meta.publish_at

    # 4 МБ чанки — баланс между скоростью и устойчивостью к обрыву.
    media = MediaFileUpload(
        str(video_path),
        mimetype="video/*",
        chunksize=4 * 1024 * 1024,
        resumable=True,
    )

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        notifySubscribers=meta.notify_subscribers,
        media_body=media,
    )

    log.info(
        f"Загружаю '{meta.title[:60]}' "
        f"({video_path.stat().st_size / 1024 / 1024:.1f} МБ) "
        f"privacy={meta.privacy_status}"
    )

    response = None
    last_pct = -1.0
    backoff = 1.0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status and progress_cb:
                pct = float(status.progress() * 100)
                if pct - last_pct > 0.5:
                    progress_cb(pct)
                    last_pct = pct
            backoff = 1.0  # reset после успешного чанка
        except HttpError as e:
            # Retriable: 500, 502, 503, 504
            if e.resp.status in (500, 502, 503, 504) and backoff <= 32:
                log.warning(
                    f"YouTube вернул {e.resp.status}, ретрай через {backoff:.0f}с"
                )
                time.sleep(backoff)
                backoff *= 2
                continue
            raise

    if progress_cb:
        progress_cb(100.0)

    video_id = response["id"]
    url = f"https://www.youtube.com/shorts/{video_id}"
    new_used = _increment_quota()
    log.info(f"Загружено: {url} (квота сегодня: {new_used}/{DAILY_UPLOAD_LIMIT})")

    if thumbnail_path and thumbnail_path.exists():
        try:
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(str(thumbnail_path)),
            ).execute()
        except HttpError as e:
            # Custom thumbnails требуют verified channel (10k subs / phone-verify)
            log.warning(f"Thumbnail не загружен (нужна верификация канала): {e}")

    return UploadResult(video_id=video_id, url=url, title=meta.title)


def upload_clips_batch(
    clips: list[tuple[Path, VideoMetadata]],
    progress_cb: Callable[[dict], None] | None = None,
    space_seconds: int = 0,
) -> list[UploadResult | dict]:
    """Последовательная загрузка нескольких клипов.

    Между клипами пауза space_seconds (если 0 — без паузы).
    Возвращает список: UploadResult для успешных, dict {"error": ...} для упавших.
    """
    results: list[UploadResult | dict] = []
    n = len(clips)
    for i, (path, meta) in enumerate(clips):
        if progress_cb:
            progress_cb({"type": "upload_start", "i": i + 1, "n": n,
                         "title": meta.title})

        def cb(pct: float, _i=i + 1, _n=n) -> None:
            if progress_cb:
                progress_cb({"type": "upload_progress", "i": _i, "n": _n,
                             "pct": pct})

        try:
            res = upload_clip(path, meta, progress_cb=cb)
            results.append(res)
            if progress_cb:
                progress_cb({"type": "upload_done", "i": i + 1, "n": n,
                             "url": res.url, "video_id": res.video_id})
        except QuotaExceeded as qe:
            # Дальше нет смысла пытаться — все остальные клипы тоже упрутся.
            log.warning(f"Стоп batch — квота исчерпана: {qe}")
            for j in range(i, n):
                p, m = clips[j]
                results.append({
                    "error": "quota_exceeded", "path": str(p),
                    "title": m.title, "skipped": True,
                })
            if progress_cb:
                progress_cb({"type": "upload_quota_exceeded",
                             "uploaded": i, "remaining": n - i,
                             "reason": str(qe)})
            break
        except Exception as exc:  # noqa: BLE001
            log.exception(f"Загрузка {path.name} упала: {exc}")
            results.append({"error": str(exc), "path": str(path),
                            "title": meta.title})
            if progress_cb:
                progress_cb({"type": "upload_failed", "i": i + 1, "n": n,
                             "reason": str(exc)})
        if space_seconds and i + 1 < n:
            time.sleep(space_seconds)
    return results
