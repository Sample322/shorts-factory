"""TikTok Content Posting API integration for finished Shorts Factory clips.

The module mirrors the local YouTube uploader shape: credentials live under
``secrets/``, OAuth tokens live under ``cache/``, and upload calls are blocking
so the worker can report deterministic progress.
"""

from __future__ import annotations

import json
import math
import mimetypes
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib import error, parse, request

from .utils import get_logger
from .youtube_upload import VideoMetadata

log = get_logger("tiktok")

CLIENT_SECRET_PATH = Path("secrets/tiktok_client.json")
TOKEN_PATH = Path("cache/tiktok_token.json")

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
REVOKE_URL = "https://open.tiktokapis.com/v2/oauth/revoke/"
CREATOR_INFO_URL = "https://open.tiktokapis.com/v2/post/publish/creator_info/query/"
DIRECT_POST_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"

DEFAULT_SCOPES = ["user.info.basic", "video.publish"]
MAX_CAPTION_LEN = 2200
MIN_CHUNK_SIZE = 5 * 1024 * 1024
MAX_CHUNK_SIZE = 64 * 1024 * 1024


class SetupRequired(Exception):
    """TikTok client config is missing."""


class AuthRequired(Exception):
    """TikTok user token is missing, expired, or refresh failed."""


class TikTokAPIError(Exception):
    """TikTok API returned an error response."""

    def __init__(self, message: str, code: str = "", log_id: str = "") -> None:
        self.code = code
        self.log_id = log_id
        super().__init__(message)

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.code:
            parts.append(f"code={self.code}")
        if self.log_id:
            parts.append(f"log_id={self.log_id}")
        return " | ".join(parts)


@dataclass
class TikTokClientConfig:
    client_key: str
    client_secret: str
    redirect_uri: str


@dataclass
class TikTokMetadata:
    caption: str
    privacy_level: str = "SELF_ONLY"
    disable_duet: bool = False
    disable_comment: bool = False
    disable_stitch: bool = False
    video_cover_timestamp_ms: int | None = 1000
    brand_content_toggle: bool = False
    brand_organic_toggle: bool = False
    is_aigc: bool = False

    def sanitize(self) -> "TikTokMetadata":
        caption = (self.caption or "").strip()[:MAX_CAPTION_LEN]
        return TikTokMetadata(
            caption=caption,
            privacy_level=self.privacy_level,
            disable_duet=bool(self.disable_duet),
            disable_comment=bool(self.disable_comment),
            disable_stitch=bool(self.disable_stitch),
            video_cover_timestamp_ms=self.video_cover_timestamp_ms,
            brand_content_toggle=bool(self.brand_content_toggle),
            brand_organic_toggle=bool(self.brand_organic_toggle),
            is_aigc=bool(self.is_aigc),
        )


@dataclass
class TikTokUploadResult:
    publish_id: str
    status: str = "PROCESSING_UPLOAD"
    public_post_ids: list[str] = field(default_factory=list)
    uploaded_bytes: int | None = None


def is_setup_complete() -> bool:
    try:
        load_client_config()
        return True
    except SetupRequired:
        return False


def is_authorized() -> bool:
    if not TOKEN_PATH.exists():
        return False
    try:
        token = _load_token()
        return bool(
            token.get("access_token")
            and (
                _access_token_valid(token)
                or _refresh_token_valid(token)
                or token.get("refresh_token")
            )
        )
    except Exception:
        return False


def load_client_config() -> TikTokClientConfig:
    """Load TikTok app credentials from secrets/tiktok_client.json or env vars."""
    data: dict[str, str] = {}
    if CLIENT_SECRET_PATH.exists():
        try:
            loaded = json.loads(CLIENT_SECRET_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data.update({str(k): str(v) for k, v in loaded.items() if v is not None})
        except (OSError, json.JSONDecodeError) as exc:
            raise SetupRequired(f"Cannot read {CLIENT_SECRET_PATH}: {exc}") from exc

    import os

    client_key = data.get("client_key") or data.get("client_id") or os.getenv("TIKTOK_CLIENT_KEY", "")
    client_secret = data.get("client_secret") or os.getenv("TIKTOK_CLIENT_SECRET", "")
    redirect_uri = data.get("redirect_uri") or os.getenv("TIKTOK_REDIRECT_URI", "")

    if not client_key or not client_secret or not redirect_uri:
        raise SetupRequired(
            f"Need {CLIENT_SECRET_PATH} with client_key, client_secret, redirect_uri"
        )
    return TikTokClientConfig(
        client_key=client_key.strip(),
        client_secret=client_secret.strip(),
        redirect_uri=redirect_uri.strip(),
    )


def build_authorization_url(
    scopes: list[str] | None = None,
    state: str | None = None,
) -> str:
    cfg = load_client_config()
    state = state or secrets.token_urlsafe(18)
    params = {
        "client_key": cfg.client_key,
        "response_type": "code",
        "scope": ",".join(scopes or DEFAULT_SCOPES),
        "redirect_uri": cfg.redirect_uri,
        "state": state,
        "disable_auto_auth": "0",
    }
    return AUTH_URL + "?" + parse.urlencode(params)


def exchange_code(code_or_redirect_url: str) -> dict:
    cfg = load_client_config()
    code = extract_authorization_code(code_or_redirect_url)
    if not code:
        raise AuthRequired("TikTok authorization code is empty")
    data = _http_form(
        TOKEN_URL,
        {
            "client_key": cfg.client_key,
            "client_secret": cfg.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": cfg.redirect_uri,
        },
    )
    token = _normalize_token(data)
    _save_token(token)
    return token


def extract_authorization_code(code_or_redirect_url: str) -> str:
    raw = (code_or_redirect_url or "").strip()
    if not raw:
        return ""
    if "code=" in raw:
        parsed = parse.urlparse(raw)
        qs = parse.parse_qs(parsed.query)
        return (qs.get("code") or [""])[0].strip()
    return parse.unquote(raw)


def get_access_token() -> str:
    token = _load_token()
    if _access_token_valid(token):
        return str(token["access_token"])
    if _refresh_token_valid(token) or token.get("refresh_token"):
        try:
            refreshed = refresh_access_token(str(token["refresh_token"]))
            return str(refreshed["access_token"])
        except Exception as exc:
            raise AuthRequired(f"TikTok token refresh failed: {exc}") from exc
    raise AuthRequired("TikTok authorization required")


def refresh_access_token(refresh_token: str | None = None) -> dict:
    cfg = load_client_config()
    refresh_token = refresh_token or str(_load_token().get("refresh_token", ""))
    if not refresh_token:
        raise AuthRequired("TikTok refresh token is missing")
    data = _http_form(
        TOKEN_URL,
        {
            "client_key": cfg.client_key,
            "client_secret": cfg.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
    )
    token = _normalize_token(data)
    _save_token(token)
    return token


def revoke() -> None:
    try:
        cfg = load_client_config()
        token = _load_token()
        access_token = token.get("access_token")
        if access_token:
            _http_form(
                REVOKE_URL,
                {
                    "client_key": cfg.client_key,
                    "client_secret": cfg.client_secret,
                    "token": str(access_token),
                },
            )
    except Exception as exc:  # noqa: BLE001
        log.warning(f"TikTok revoke failed, removing local token anyway: {exc}")
    if TOKEN_PATH.exists():
        TOKEN_PATH.unlink()


def query_creator_info() -> dict:
    data = _http_json(CREATOR_INFO_URL, None, access_token=get_access_token())
    return data.get("data", {}) if isinstance(data, dict) else {}


def fetch_publish_status(publish_id: str) -> dict:
    data = _http_json(
        STATUS_URL,
        {"publish_id": publish_id},
        access_token=get_access_token(),
    )
    return data.get("data", {}) if isinstance(data, dict) else {}


def wait_for_publish_status(
    publish_id: str,
    timeout_sec: float = 45.0,
    interval_sec: float = 5.0,
) -> dict:
    deadline = time.monotonic() + max(0.0, timeout_sec)
    last: dict = {}
    while True:
        last = fetch_publish_status(publish_id)
        status = str(last.get("status", ""))
        if status in {"PUBLISH_COMPLETE", "FAILED", "SEND_TO_USER_INBOX"}:
            return last
        if time.monotonic() >= deadline:
            return last
        time.sleep(max(1.0, interval_sec))


def upload_clip(
    video_path: Path,
    metadata: TikTokMetadata,
    progress_cb: Callable[[float], None] | None = None,
    poll_timeout_sec: float = 45.0,
    poll_interval_sec: float = 5.0,
) -> TikTokUploadResult:
    if not video_path.exists():
        raise FileNotFoundError(video_path)

    meta = metadata.sanitize()
    access_token = get_access_token()
    creator = query_creator_info()
    privacy_options = creator.get("privacy_level_options") or []
    meta.privacy_level = _select_privacy(meta.privacy_level, privacy_options)

    size = video_path.stat().st_size
    chunk_size, total_chunks = make_upload_plan(size)
    init_data = _http_json(
        DIRECT_POST_URL,
        {
            "post_info": _post_info(meta),
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": size,
                "chunk_size": chunk_size,
                "total_chunk_count": total_chunks,
            },
        },
        access_token=access_token,
        timeout=60,
    )
    payload = init_data.get("data", {}) if isinstance(init_data, dict) else {}
    publish_id = str(payload.get("publish_id", ""))
    upload_url = str(payload.get("upload_url", ""))
    if not publish_id or not upload_url:
        raise TikTokAPIError("TikTok did not return publish_id/upload_url")

    if progress_cb:
        progress_cb(1.0)
    uploaded = _upload_binary(
        video_path, upload_url, chunk_size, total_chunks, progress_cb
    )

    status_data = wait_for_publish_status(
        publish_id,
        timeout_sec=poll_timeout_sec,
        interval_sec=poll_interval_sec,
    )
    status = str(status_data.get("status") or "PROCESSING_UPLOAD")
    public_ids = [str(v) for v in status_data.get("publicaly_available_post_id", [])]
    return TikTokUploadResult(
        publish_id=publish_id,
        status=status,
        public_post_ids=public_ids,
        uploaded_bytes=int(status_data.get("uploaded_bytes") or uploaded),
    )


def adapt_metadata_for_tiktok(
    youtube_meta: VideoMetadata,
    cfg: dict | None = None,
) -> TikTokMetadata:
    """Convert YouTube SEO metadata into a TikTok-native caption."""
    cfg = cfg or {}
    tt_cfg = cfg.get("tiktok", {})
    max_hashtags = int(tt_cfg.get("max_caption_hashtags", 14))

    first_line = _first_caption_line(youtube_meta.description, youtube_meta.title)
    hashtags = _collect_tiktok_hashtags(youtube_meta, max_hashtags=max_hashtags)
    caption = (first_line + "\n\n" + " ".join(hashtags)).strip()
    return TikTokMetadata(
        caption=caption[:MAX_CAPTION_LEN],
        privacy_level=str(tt_cfg.get("default_privacy", "SELF_ONLY")),
        disable_duet=bool(tt_cfg.get("disable_duet", False)),
        disable_comment=bool(tt_cfg.get("disable_comment", False)),
        disable_stitch=bool(tt_cfg.get("disable_stitch", False)),
        is_aigc=bool(tt_cfg.get("is_aigc", False)),
    )


def make_upload_plan(file_size: int) -> tuple[int, int]:
    if file_size <= 0:
        raise ValueError("file_size must be positive")
    if file_size <= MAX_CHUNK_SIZE:
        return file_size, 1
    total_chunks = math.ceil(file_size / MAX_CHUNK_SIZE)
    chunk_size = file_size // total_chunks
    chunk_size = max(MIN_CHUNK_SIZE, min(MAX_CHUNK_SIZE, chunk_size))
    return chunk_size, total_chunks


def _post_info(meta: TikTokMetadata) -> dict:
    data = {
        "title": meta.caption,
        "privacy_level": meta.privacy_level,
        "disable_duet": meta.disable_duet,
        "disable_comment": meta.disable_comment,
        "disable_stitch": meta.disable_stitch,
        "brand_content_toggle": meta.brand_content_toggle,
        "brand_organic_toggle": meta.brand_organic_toggle,
        "is_aigc": meta.is_aigc,
    }
    if meta.video_cover_timestamp_ms is not None:
        data["video_cover_timestamp_ms"] = int(meta.video_cover_timestamp_ms)
    return data


def _select_privacy(requested: str, options: list[str]) -> str:
    if not options:
        return requested
    if requested in options:
        return requested
    if "SELF_ONLY" in options:
        return "SELF_ONLY"
    return str(options[0])


def _upload_binary(
    video_path: Path,
    upload_url: str,
    chunk_size: int,
    total_chunks: int,
    progress_cb: Callable[[float], None] | None,
) -> int:
    total = video_path.stat().st_size
    mime = mimetypes.guess_type(str(video_path))[0] or "video/mp4"
    uploaded = 0
    with video_path.open("rb") as f:
        for chunk_index in range(total_chunks):
            remaining = total - uploaded
            read_size = remaining if chunk_index == total_chunks - 1 else chunk_size
            chunk = f.read(read_size)
            if not chunk:
                break
            start = uploaded
            end = uploaded + len(chunk) - 1
            headers = {
                "Content-Type": mime,
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {start}-{end}/{total}",
            }
            req = request.Request(upload_url, data=chunk, headers=headers, method="PUT")
            try:
                with request.urlopen(req, timeout=300) as resp:
                    resp.read()
            except error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                raise TikTokAPIError(
                    f"TikTok upload failed HTTP {exc.code}: {body[:300]}"
                ) from exc
            uploaded += len(chunk)
            if progress_cb:
                progress_cb(min(100.0, uploaded / total * 100.0))
    return uploaded


def _load_token() -> dict:
    if not TOKEN_PATH.exists():
        raise AuthRequired("TikTok token is missing")
    try:
        return json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthRequired(f"TikTok token cannot be read: {exc}") from exc


def _save_token(token: dict) -> None:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(json.dumps(token, ensure_ascii=False, indent=2), encoding="utf-8")


def _normalize_token(data: dict) -> dict:
    now = time.time()
    expires_in = int(data.get("expires_in") or 0)
    refresh_expires_in = int(data.get("refresh_expires_in") or 0)
    return {
        "access_token": data.get("access_token", ""),
        "refresh_token": data.get("refresh_token", ""),
        "open_id": data.get("open_id", ""),
        "scope": data.get("scope", ""),
        "token_type": data.get("token_type", "Bearer"),
        "expires_at": now + expires_in,
        "refresh_expires_at": now + refresh_expires_in,
        "created_at": now,
    }


def _access_token_valid(token: dict) -> bool:
    return bool(token.get("access_token")) and float(token.get("expires_at", 0)) > time.time() + 120


def _refresh_token_valid(token: dict) -> bool:
    expires_at = float(token.get("refresh_expires_at", 0))
    return bool(token.get("refresh_token")) and (expires_at == 0 or expires_at > time.time() + 120)


def _http_form(url: str, form: dict[str, str]) -> dict:
    encoded = parse.urlencode(form).encode("utf-8")
    req = request.Request(
        url,
        data=encoded,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Cache-Control": "no-cache",
        },
        method="POST",
    )
    return _send_json_request(req)


def _http_json(
    url: str,
    payload: dict | None,
    access_token: str,
    timeout: float = 60.0,
) -> dict:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
        method="POST",
    )
    return _send_json_request(req, timeout=timeout)


def _send_json_request(req: request.Request, timeout: float = 60.0) -> dict:
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except error.HTTPError as exc:
        body = exc.read()
        data = _decode_json(body)
        message, code, log_id = _extract_error(data)
        raise TikTokAPIError(
            message or f"TikTok HTTP {exc.code}",
            code=code or str(exc.code),
            log_id=log_id,
        ) from exc
    data = _decode_json(body)
    message, code, log_id = _extract_error(data)
    if code and code != "ok":
        raise TikTokAPIError(message or code, code=code, log_id=log_id)
    if data.get("error") and not isinstance(data.get("error"), dict):
        raise TikTokAPIError(
            str(data.get("error_description") or data.get("error")),
            code=str(data.get("error")),
            log_id=str(data.get("log_id", "")),
        )
    return data


def _decode_json(body: bytes) -> dict:
    if not body:
        return {}
    try:
        data = json.loads(body.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError as exc:
        raise TikTokAPIError(f"TikTok returned non-JSON response: {body[:200]!r}") from exc


def _extract_error(data: dict) -> tuple[str, str, str]:
    err = data.get("error")
    if isinstance(err, dict):
        return (
            str(err.get("message") or ""),
            str(err.get("code") or ""),
            str(err.get("log_id") or ""),
        )
    if err:
        return (
            str(data.get("error_description") or err),
            str(err),
            str(data.get("log_id") or ""),
        )
    return "", "", ""


_HASHTAG_RE = re.compile(r"(?<!\w)#([0-9A-Za-zА-Яа-яЁё_]+)", re.UNICODE)
_BLOCKED_TIKTOK_HASHTAGS = {
    "youtube",
    "youtubeshorts",
    "ytshorts",
    "ютуб",
    "ютубшортс",
    "fyp",
    "foryou",
    "foryoupage",
    "viral",
    "viralshorts",
    "trendingshorts",
}
_TIKTOK_DEFAULT_HASHTAGS = [
    "#tiktok",
    "#тикток",
    "#shorts",
    "#шортс",
    "#сериал",
    "#кино",
    "#movieclips",
    "#seriesclips",
]


def _first_caption_line(description: str, title: str) -> str:
    first = next((line.strip() for line in description.splitlines() if line.strip()), "")
    first = _HASHTAG_RE.sub("", first).strip()
    return first or title.strip() or "Сцена из сериала"


def _collect_tiktok_hashtags(meta: VideoMetadata, max_hashtags: int) -> list[str]:
    candidates: list[str] = []
    for text in [meta.description, " ".join(meta.tags or [])]:
        for match in _HASHTAG_RE.finditer(text or ""):
            candidates.append("#" + match.group(1))
    for tag in meta.tags or []:
        cleaned = tag.strip().lstrip("#")
        if cleaned:
            candidates.append("#" + cleaned.replace(" ", ""))
    candidates.extend(_TIKTOK_DEFAULT_HASHTAGS)

    seen: set[str] = set()
    result: list[str] = []
    for tag in candidates:
        normalized = tag.strip().lower().lstrip("#").replace("ё", "е")
        if not normalized or normalized in seen or normalized in _BLOCKED_TIKTOK_HASHTAGS:
            continue
        seen.add(normalized)
        result.append("#" + tag.strip().lstrip("#"))
        if len(result) >= max(1, max_hashtags):
            break
    return result
