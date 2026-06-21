"""Streamlit GUI для Shorts Factory."""

import json
import os
import shutil
import time
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(page_title="Shorts Factory", page_icon="\U0001f3ac", layout="wide")

# Ленивый импорт pipeline — чтобы страница грузилась даже если pipeline сломан
_pipeline_error = None
try:
    from pipeline.render import run_job
    from pipeline.subtitle import AVAILABLE_FONTS, ANIMATION_STYLES, GOLDEN_STANDARD
except Exception as e:
    _pipeline_error = str(e)
    AVAILABLE_FONTS = {"Montserrat ExtraBold": None, "Impact": None, "Arial Black": None}
    ANIMATION_STYLES = {"karaoke": "Караоке", "instant": "Мгновенная", "fade": "Плавная", "none": "Нет"}
    GOLDEN_STANDARD = {
        "font_name": "Montserrat ExtraBold", "font_size": 72,
        "words_per_line": 3, "text_color": "#FFFFFF",
        "highlight_color": "#FFD700", "outline_color": "#000000",
        "outline_width": 6, "margin_v": 220, "margin_h": 60,
        "alignment": 2, "uppercase": True, "animation": "karaoke",
        "timing_offset_ms": 0,
    }

st.markdown(
    "<style>.stApp{max-width:1400px;margin:0 auto}</style>",
    unsafe_allow_html=True,
)

st.title("\U0001f3ac Shorts Factory")
st.caption("Локальная нарезка длинных видео в YouTube Shorts — GPU, без облака")

if _pipeline_error:
    st.error(f"Ошибка загрузки pipeline: {_pipeline_error}")


def _cleanup_old_uploads(max_age_hours: int = 24) -> None:
    """Удаляет загруженные файлы старше N часов из cache/uploads."""
    upload_dir = Path("cache") / "uploads"
    if not upload_dir.exists():
        return
    cutoff = time.time() - max_age_hours * 3600
    for f in upload_dir.iterdir():
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


if "uploads_cleaned" not in st.session_state:
    _cleanup_old_uploads()
    st.session_state["uploads_cleaned"] = True


_STAGES_META = [
    ("ingest",     "📥", "Приём видео"),
    ("extract",    "🎚️", "Извлечение аудио"),
    ("transcribe", "🎙️", "Транскрипция (Whisper)"),
    ("analyze",    "🧠", "Анализ моментов (LLM)"),
    ("clips",      "✂️", "Сборка клипов"),
    ("finalize",   "💾", "Финализация"),
]


def _fmt_time(sec: float) -> str:
    """Форматирует секунды как '1ч 23м 45с' / '12м 03с' / '45с'."""
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}ч {m:02d}м {s:02d}с"
    if m:
        return f"{m}м {s:02d}с"
    return f"{s}с"


def _llm_chain_label() -> str:
    try:
        from pipeline.analyze import describe_llm_chain
        from pipeline.utils import load_config

        return describe_llm_chain(load_config())
    except Exception:
        return "LLM fallback"


# --- HTML helpers ---
# КРИТИЧНО: эти функции возвращают HTML БЕЗ переносов строк и без отступов
# в начале строк. Streamlit Markdown интерпретирует строки с 4+ пробелами
# в начале как блок кода, и тогда HTML вываливается сырым в UI.
# В рендере мы используем st.html() (Streamlit 1.35+), который не проходит
# через Markdown-парсер вообще — это второй слой защиты.


def _stage_card_html(state: dict) -> str:
    """Горизонтальная лента этапов с подсветкой активного."""
    current_stage = state.get("stage")
    completed = state.get("completed_stages", set())

    parts = ['<div style="display:flex;gap:8px;margin:10px 0 16px 0;">']
    for stage_id, icon, label in _STAGES_META:
        if stage_id in completed:
            color = "#22c55e"
            bg = "rgba(34,197,94,0.14)"
            glow = ""
        elif stage_id == current_stage:
            color = "#fbbf24"
            bg = "rgba(251,191,36,0.20)"
            glow = f"box-shadow:0 0 16px {color}66;"
        else:
            color = "#475569"
            bg = "rgba(71,85,105,0.10)"
            glow = ""
        parts.append(
            f'<div style="flex:1;min-width:0;background:{bg};'
            f'border:1px solid {color};border-radius:10px;padding:12px 8px;'
            f'text-align:center;transition:all 0.3s;{glow}">'
            f'<div style="font-size:24px;line-height:1;">{icon}</div>'
            f'<div style="color:{color};font-size:11px;font-weight:600;'
            f'margin-top:6px;white-space:nowrap;overflow:hidden;'
            f'text-overflow:ellipsis;">{label}</div>'
            f'</div>'
        )
    parts.append('</div>')
    return "".join(parts)


def _big_metric_html(label: str, value: str, sub: str = "") -> str:
    sub_html = (
        f'<div style="color:#64748b;font-size:11px;margin-top:3px;">{sub}</div>'
        if sub else ''
    )
    return (
        '<div style="background:rgba(255,255,255,0.04);border-radius:12px;'
        'padding:14px 16px;border:1px solid rgba(255,255,255,0.08);">'
        f'<div style="color:#94a3b8;font-size:11px;text-transform:uppercase;'
        f'letter-spacing:0.8px;font-weight:600;">{label}</div>'
        f'<div style="color:#fafafa;font-size:26px;font-weight:700;'
        f'margin-top:4px;line-height:1.1;">{value}</div>'
        f'{sub_html}</div>'
    )


def _metrics_row_html(blocks: list[str]) -> str:
    """Обёртка для нескольких _big_metric_html в одной grid-строке."""
    return (
        '<div style="display:grid;grid-template-columns:repeat(3,1fr);'
        f'gap:12px;margin:6px 0 14px 0;">{"".join(blocks)}</div>'
    )


def _clip_card_html(clip: dict) -> str:
    status = clip.get("status", "pending")
    title = clip.get("title", "—")
    i = clip.get("i", 0)
    n = clip.get("n", 0)
    dur = clip.get("duration", 0)
    substep = clip.get("substep", "")
    fail_reason = clip.get("reason", "")

    palette = {
        "done":   ("#22c55e", "✅", "rgba(34,197,94,0.10)"),
        "active": ("#fbbf24", "⚙️", "rgba(251,191,36,0.12)"),
        "failed": ("#ef4444", "❌", "rgba(239,68,68,0.10)"),
        "pending":("#475569", "⏳", "rgba(71,85,105,0.06)"),
    }
    bar, icon, bg = palette.get(status, palette["pending"])

    extra_line = ""
    if status == "active" and substep:
        extra_line = (
            f'<div style="color:#fbbf24;font-size:11px;margin-top:3px;">'
            f'⚙ {substep}</div>'
        )
    elif status == "failed" and fail_reason:
        msg = fail_reason[:60] + ("…" if len(fail_reason) > 60 else "")
        extra_line = (
            f'<div style="color:#ef4444;font-size:11px;margin-top:3px;">'
            f'{msg}</div>'
        )

    title_short = title[:55] + ("…" if len(title) > 55 else "")
    return (
        f'<div style="display:flex;gap:12px;padding:10px 12px;'
        f'background:{bg};border-left:4px solid {bar};border-radius:8px;'
        f'margin-bottom:6px;">'
        f'<div style="font-size:18px;line-height:1.2;">{icon}</div>'
        f'<div style="flex:1;min-width:0;">'
        f'<div style="display:flex;justify-content:space-between;gap:8px;">'
        f'<div style="color:#fafafa;font-size:13px;font-weight:600;'
        f'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">'
        f'{i}/{n}. {title_short}</div>'
        f'<div style="color:#94a3b8;font-size:12px;white-space:nowrap;">'
        f'{dur:.0f}с</div></div>'
        f'{extra_line}</div></div>'
    )


def _clips_grid_html(clips: list[dict]) -> str:
    return "<div>" + "".join(_clip_card_html(c) for c in clips) + "</div>"


def _detail_banner_html(text: str) -> str:
    return (
        '<div style="background:rgba(251,191,36,0.08);'
        'border-left:3px solid #fbbf24;padding:12px 14px;'
        'border-radius:8px;margin:8px 0;color:#fafafa;font-size:14px;">'
        f'{text}</div>'
    )


def _render_clip_youtube_block(
    clip: dict, clip_path: Path, meta: dict, i: int
) -> None:
    """Кнопка ручной загрузки на YouTube + показ статуса/ссылки для одного клипа."""
    if clip.get("youtube_url"):
        st.success(f"📺 [Видео на YouTube]({clip['youtube_url']})")
        return

    if clip.get("youtube_error"):
        st.warning(f"⚠ YouTube: {clip['youtube_error'][:80]}")

    if not clip_path.exists():
        return

    try:
        from pipeline.youtube_upload import (
            QuotaExceeded as _QuotaExceeded,
            check_quota_available as _yt_quota_ok,
            get_quota_status as _yt_quota,
            is_authorized as yt_is_auth,
            is_setup_complete as yt_setup,
            upload_clip as yt_upload,
        )
        from pipeline.seo_generator import generate_seo
        from pipeline.utils import load_config as _load_cfg
    except Exception:
        return

    if not yt_setup():
        return

    btn_key = f"yt_upload_{meta['job_id']}_{i}"
    if not yt_is_auth():
        st.caption("📺 Авторизуйся в сайдбаре, чтобы загрузить")
        return

    quota_used, quota_lim = _yt_quota()
    if quota_used >= quota_lim:
        st.error(
            f"📺 Квота на сегодня: {quota_used}/{quota_lim}. "
            "Сброс в 00:00 Pacific Time."
        )
        return

    with st.expander("📺 Загрузить на YouTube"):
        st.caption(f"Остаток квоты: {quota_lim - quota_used}/{quota_lim}")
        privacy = st.selectbox(
            "Видимость",
            ["unlisted", "public", "private"],
            index=["unlisted", "public", "private"].index(
                st.session_state.get("youtube_privacy", "unlisted")
            ),
            key=f"yt_priv_{btn_key}",
            format_func=lambda v: {
                "public": "🌍 Public",
                "unlisted": "🔗 Unlisted",
                "private": "🔒 Private",
            }[v],
        )
        if st.button(
            f"🚀 Загрузить clip_{i+1:02d}", key=btn_key,
            use_container_width=True
        ):
            try:
                with st.spinner("Генерирую SEO + загружаю..."):
                    cfg = _load_cfg()
                    meta_video = generate_seo(
                        cfg,
                        clip_title=clip.get("title", "Short"),
                        clip_description=clip.get("description", ""),
                        clip_tags_hint=clip.get("tags", []),
                        music_mood=clip.get("music_mood", ""),
                        source_context=st.session_state.get(
                            "youtube_source_context", ""
                        ),
                        language=cfg.get("whisper", {}).get("language", "ru"),
                    )
                    meta_video.privacy_status = privacy
                    progress_bar = st.progress(0, text="Загрузка...")
                    result = yt_upload(
                        clip_path, meta_video,
                        progress_cb=lambda p: progress_bar.progress(
                            min(1.0, p / 100), text=f"Загрузка {p:.0f}%"
                        ),
                    )
                    progress_bar.progress(1.0, text="✅ Готово")
                # Записываем в meta.json чтобы не загрузить второй раз
                meta_file = Path("output") / meta["job_id"] / "meta.json"
                if meta_file.exists():
                    cur = json.loads(meta_file.read_text(encoding="utf-8"))
                    cur["clips"][i]["youtube_url"] = result.url
                    cur["clips"][i]["youtube_video_id"] = result.video_id
                    meta_file.write_text(
                        json.dumps(cur, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                st.success(f"✅ [Видео]({result.url})")
                st.rerun()
            except _QuotaExceeded as qe:
                st.error(f"📺 {qe}")
            except Exception as e:
                st.error(f"❌ {type(e).__name__}: {e}")


def _render_clip_tiktok_block(
    clip: dict, clip_path: Path, meta: dict, i: int
) -> None:
    """Manual TikTok upload/status for a finished clip."""
    if clip.get("tiktok_publish_id"):
        status = clip.get("tiktok_status", "PROCESSING")
        st.success(f"🎵 TikTok: `{clip['tiktok_publish_id']}` · {status}")
        if clip.get("tiktok_public_post_ids"):
            st.caption("Post ID: " + ", ".join(clip["tiktok_public_post_ids"]))
        return

    if clip.get("tiktok_error"):
        st.warning(f"⚠ TikTok: {clip['tiktok_error'][:80]}")

    if not clip_path.exists():
        return

    try:
        from pipeline.seo_generator import generate_seo
        from pipeline.tiktok_upload import (
            adapt_metadata_for_tiktok,
            is_authorized as tt_is_auth,
            is_setup_complete as tt_setup,
            upload_clip as tt_upload,
        )
        from pipeline.utils import load_config as _load_cfg
    except Exception:
        return

    if not tt_setup():
        return

    btn_key = f"tt_upload_{meta['job_id']}_{i}"
    if not tt_is_auth():
        st.caption("🎵 Авторизуйся в TikTok в сайдбаре, чтобы загрузить")
        return

    with st.expander("🎵 Загрузить в TikTok"):
        privacy_values = [
            "SELF_ONLY",
            "PUBLIC_TO_EVERYONE",
            "MUTUAL_FOLLOW_FRIENDS",
            "FOLLOWER_OF_CREATOR",
        ]
        privacy = st.selectbox(
            "Видимость TikTok",
            privacy_values,
            index=privacy_values.index(
                st.session_state.get("tiktok_privacy", "SELF_ONLY")
            ),
            key=f"tt_priv_{btn_key}",
            format_func=lambda v: {
                "PUBLIC_TO_EVERYONE": "Public",
                "MUTUAL_FOLLOW_FRIENDS": "Friends",
                "FOLLOWER_OF_CREATOR": "Followers",
                "SELF_ONLY": "Private",
            }[v],
        )
        if st.button(
            f"🚀 Загрузить в TikTok clip_{i+1:02d}",
            key=btn_key,
            use_container_width=True,
        ):
            try:
                with st.spinner("Генерирую caption + загружаю в TikTok..."):
                    cfg = _load_cfg()
                    meta_video = generate_seo(
                        cfg,
                        clip_title=clip.get("title", "Short"),
                        clip_description=clip.get("description", ""),
                        clip_tags_hint=clip.get("tags", []),
                        music_mood=clip.get("music_mood", ""),
                        source_context=st.session_state.get(
                            "youtube_source_context", ""
                        ),
                        language=cfg.get("whisper", {}).get("language", "ru"),
                    )
                    tt_meta = adapt_metadata_for_tiktok(meta_video, cfg)
                    tt_meta.privacy_level = privacy
                    tt_meta.disable_comment = bool(
                        st.session_state.get("tiktok_disable_comment", False)
                    )
                    tt_meta.disable_duet = bool(
                        st.session_state.get("tiktok_disable_duet", False)
                    )
                    tt_meta.disable_stitch = bool(
                        st.session_state.get("tiktok_disable_stitch", False)
                    )
                    tt_meta.is_aigc = bool(
                        st.session_state.get("tiktok_is_aigc", False)
                    )
                    progress_bar = st.progress(0, text="Загрузка...")
                    tt_cfg = cfg.get("tiktok", {})
                    result = tt_upload(
                        clip_path,
                        tt_meta,
                        progress_cb=lambda p: progress_bar.progress(
                            min(1.0, p / 100), text=f"Загрузка {p:.0f}%"
                        ),
                        poll_timeout_sec=float(
                            tt_cfg.get("status_poll_timeout_sec", 45)
                        ),
                        poll_interval_sec=float(
                            tt_cfg.get("status_poll_interval_sec", 5)
                        ),
                    )
                    progress_bar.progress(1.0, text="✅ Готово")
                meta_file = Path("output") / meta["job_id"] / "meta.json"
                if meta_file.exists():
                    cur = json.loads(meta_file.read_text(encoding="utf-8"))
                    cur["clips"][i]["tiktok_publish_id"] = result.publish_id
                    cur["clips"][i]["tiktok_status"] = result.status
                    cur["clips"][i]["tiktok_meta"] = {
                        "caption": tt_meta.caption,
                        "privacy_level": tt_meta.privacy_level,
                    }
                    if result.public_post_ids:
                        cur["clips"][i]["tiktok_public_post_ids"] = (
                            result.public_post_ids
                        )
                    meta_file.write_text(
                        json.dumps(cur, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                st.success(f"✅ TikTok publish_id: `{result.publish_id}`")
                st.rerun()
            except Exception as e:
                st.error(f"❌ {type(e).__name__}: {e}")


def _render_music_preflight() -> None:
    """Pre-flight чекер для AI-музыки: HF auth + скачивание модели + тест."""
    model_id = "stabilityai/stable-audio-open-1.0"

    # Шаг 1: HF auth
    hf_authed = False
    hf_username: str | None = None
    try:
        from huggingface_hub import whoami
        info = whoami()
        hf_username = info.get("name") if isinstance(info, dict) else None
        hf_authed = bool(hf_username)
    except Exception:
        hf_authed = False

    if hf_authed:
        st.success(f"✅ HuggingFace: вошёл как `{hf_username}`")
    else:
        st.warning("⚠️ Не авторизован в HuggingFace")
        st.markdown(
            f"1. Создай токен (тип **Read**): https://huggingface.co/settings/tokens\n"
            f"2. Открой страницу модели → нажми **Agree and access repository**: "
            f"https://huggingface.co/{model_id}\n"
            "3. Вставь токен ниже:"
        )
        token = st.text_input(
            "HuggingFace token", type="password",
            placeholder="hf_xxxxxxxxxxxxxxxxxxxx",
            key="hf_token_input",
        )
        if st.button("🔐 Сохранить токен", use_container_width=True):
            if not token.startswith("hf_"):
                st.error("Токен должен начинаться с `hf_`")
            else:
                try:
                    from huggingface_hub import login as hf_login
                    hf_login(token=token, add_to_git_credential=False)
                    st.success("✅ Сохранён, перезагружаю...")
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ {e}")
        return  # без auth дальше нет смысла

    # Шаг 2: проверка скачана ли модель
    try:
        from huggingface_hub import try_to_load_from_cache
        # Проверим один из ключевых файлов модели
        cached = try_to_load_from_cache(model_id, "model_config.json")
        model_downloaded = cached is not None
    except Exception:
        model_downloaded = False

    # Проверяем идёт ли уже фоновая загрузка
    dl_status_file = Path("cache/hf_download.status.json")
    dl_status = None
    if dl_status_file.exists():
        try:
            dl_status = json.loads(dl_status_file.read_text(encoding="utf-8"))
        except Exception:
            dl_status = None

    if model_downloaded and (not dl_status or dl_status.get("status") == "done"):
        st.success("✅ Модель Stable Audio Open скачана (готова к работе)")
    elif dl_status and dl_status.get("status") == "running":
        # Идёт фоновая загрузка — показываем живой прогресс
        pct = dl_status.get("pct", 0)
        mb = dl_status.get("downloaded_mb", 0)
        total = dl_status.get("total_mb_estimate", 4500)
        speed = dl_status.get("speed_mb_s", 0)
        eta = dl_status.get("eta_sec", 0)
        # Проверяем что процесс жив (не "потерян")
        age_sec = time.time() - dl_status.get("updated_at", 0)
        if age_sec > 60:
            st.warning(
                f"⚠️ Прогресс не обновлялся {int(age_sec)}с. "
                "Возможно процесс завис. Можешь отменить и запустить заново."
            )

        st.progress(
            min(1.0, pct / 100),
            text=f"⬇️ Скачиваю модель: {mb:.0f} / {total:.0f} МБ ({pct:.0f}%)"
        )
        eta_str = _fmt_time(eta) if eta > 0 else "оценивается…"
        speed_str = f"{speed:.2f} МБ/с" if speed > 0 else "оценивается…"
        st.caption(f"⏱ Осталось ≈ {eta_str}  ·  📡 {speed_str}")

        col_a, col_b = st.columns(2)
        with col_a:
            if st.button("🔄 Обновить", use_container_width=True):
                st.rerun()
        with col_b:
            if st.button("⛔ Отменить", use_container_width=True):
                pid = st.session_state.get("hf_dl_pid")
                if pid:
                    _kill_worker(pid)
                try:
                    dl_status_file.unlink()
                except OSError:
                    pass
                st.session_state.pop("hf_dl_pid", None)
                st.rerun()
        # Авто-обновление каждые 3 сек — НЕ блокирует, просто перерисовка
        time.sleep(3.0)
        st.rerun()
        return
    elif dl_status and dl_status.get("status") == "error":
        st.error(f"❌ Скачивание упало: {dl_status.get('error', '?')}")
        st.caption("Часто причина — не нажат 'Agree and access' на странице модели.")
        if st.button("🔄 Попробовать снова", use_container_width=True):
            try:
                dl_status_file.unlink()
            except OSError:
                pass
            st.rerun()
        return
    else:
        st.info("ℹ️ Модель ещё не скачана (~4.5 ГБ — один раз)")
        st.caption(
            "Скачивание идёт в фоне — можешь свернуть это окно или закрыть. "
            "UI не замораживается, прогресс обновляется каждые 3 секунды."
        )
        if st.button("⬇️ Скачать модель в фоне",
                     use_container_width=True, type="primary"):
            try:
                # Удаляем старый status-файл если есть
                if dl_status_file.exists():
                    dl_status_file.unlink()
                # Запускаем downloader как detached subprocess
                import subprocess as _sp
                python_exe = str(Path(".venv/Scripts/python.exe").resolve())
                env = os.environ.copy()
                env["PYTHONIOENCODING"] = "utf-8"
                env["PYTHONUTF8"] = "1"
                DETACHED = 0x00000008
                NEW_GROUP = 0x00000200
                log_file = Path("cache/jobs/hf_download.log")
                log_file.parent.mkdir(parents=True, exist_ok=True)
                proc = _sp.Popen(
                    [
                        python_exe, "-m", "pipeline.hf_downloader",
                        "--repo-id", model_id,
                        "--status-file", str(dl_status_file.resolve()),
                    ],
                    stdout=open(log_file, "w", encoding="utf-8"),
                    stderr=_sp.STDOUT,
                    cwd=str(Path.cwd()),
                    creationflags=DETACHED | NEW_GROUP,
                    close_fds=True,
                    env=env,
                )
                st.session_state["hf_dl_pid"] = proc.pid
                st.success(f"🚀 Запущено в фоне (PID {proc.pid}). Обновление через секунду...")
                time.sleep(1.5)
                st.rerun()
            except Exception as e:
                st.error(f"❌ Не запустилось: {e}")
        return

    # Шаг 3: тест-генерация через subprocess (не блокирует Streamlit!)
    st.caption("Готово к генерации. Можешь смело включать «Авто-музыку» в запуске.")
    test_status_file = Path("cache/test_music_status.json")
    test_status = None
    if test_status_file.exists():
        try:
            test_status = json.loads(test_status_file.read_text(encoding="utf-8"))
        except Exception:
            test_status = None

    # Если тест идёт — показываем прогресс
    if test_status and test_status.get("status") in ("running", "loading"):
        pct = test_status.get("pct", 0)
        label = test_status.get("label", "…")
        st.progress(min(1.0, pct / 100), text=f"🧪 {label}")
        age = time.time() - test_status.get("updated_at", 0)
        if age > 60:
            st.warning(
                f"⚠️ Прогресс не обновлялся {int(age)}с — возможно процесс умер"
            )
        col_a, col_b = st.columns(2)
        with col_a:
            if st.button("🔄 Обновить", use_container_width=True,
                         key="test_refresh"):
                st.rerun()
        with col_b:
            if st.button("⛔ Отменить", use_container_width=True,
                         key="test_cancel"):
                pid = st.session_state.get("test_gen_pid")
                if pid:
                    _kill_worker(pid)
                try:
                    test_status_file.unlink()
                except OSError:
                    pass
                st.session_state.pop("test_gen_pid", None)
                st.rerun()
        time.sleep(2.0)
        st.rerun()
        return

    if test_status and test_status.get("status") == "done":
        elapsed = test_status.get("elapsed_sec", 0)
        st.success(f"✅ Тест-генерация прошла за {elapsed:.0f}с")
        file_path = Path(test_status.get("file", ""))
        if file_path.exists():
            # bytes (не Path!) — иначе Streamlit polling_path_watcher на
            # Windows крашится с access violation при I/O в output/cache.
            st.audio(_load_file_bytes(str(file_path)))
            st.caption(f"Файл: `{file_path}`")
        if st.button("🔄 Сгенерить ещё", use_container_width=True,
                     key="test_again"):
            test_status_file.unlink()
            st.rerun()
        return

    if test_status and test_status.get("status") == "error":
        st.error(f"❌ {test_status.get('error', '?')}")
        tb = test_status.get("traceback")
        if tb:
            with st.expander("Детали"):
                st.code(tb)
        if st.button("🔄 Попробовать снова", use_container_width=True,
                     key="test_retry"):
            test_status_file.unlink()
            st.rerun()
        return

    # Нет статуса — кнопка запуска
    if st.button("🧪 Тест: сгенерить 10-сек трек (в фоне)",
                 use_container_width=True):
        try:
            import subprocess as _sp
            python_exe = str(Path(".venv/Scripts/python.exe").resolve())
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"
            DETACHED = 0x00000008
            NEW_GROUP = 0x00000200
            log_file = Path("cache/jobs/test_music.log")
            log_file.parent.mkdir(parents=True, exist_ok=True)
            proc = _sp.Popen(
                [
                    python_exe, "-m", "pipeline.music_test_runner",
                    "--status-file", str(test_status_file.resolve()),
                    "--output-dir", str(Path("cache/test_music").resolve()),
                    "--mood", "upbeat",
                    "--duration", "10",
                    "--steps", "100",
                ],
                stdout=open(log_file, "w", encoding="utf-8"),
                stderr=_sp.STDOUT,
                cwd=str(Path.cwd()),
                creationflags=DETACHED | NEW_GROUP,
                close_fds=True,
                env=env,
            )
            st.session_state["test_gen_pid"] = proc.pid
            st.success(f"🚀 Запущен в фоне (PID {proc.pid})")
            time.sleep(1.5)
            st.rerun()
        except Exception as e:
            st.error(f"❌ {type(e).__name__}: {e}")


def _render_youtube_sidebar() -> None:
    """Блок YouTube в сайдбаре: статус авторизации, тоггл авто-загрузки."""
    try:
        from pipeline.youtube_upload import (
            CLIENT_SECRET_PATH, is_authorized, is_setup_complete, revoke,
        )
    except Exception as e:
        st.caption(f"⚠ YouTube-модуль недоступен: {e}")
        st.session_state["youtube_enabled"] = False
        return

    if not is_setup_complete():
        st.warning(
            "Нужен файл `youtube_client_secret.json` от Google Cloud (один раз)."
        )
        with st.expander("📖 Пошаговая настройка (открой шаги по одному)",
                         expanded=True):
            st.markdown(
                """**Шаг 1.** [Открыть Google Cloud Console]
(https://console.cloud.google.com/projectcreate) → создать проект с именем
`shorts-factory` → Create"""
            )
            st.markdown(
                """**Шаг 2.** [Включить YouTube Data API v3]
(https://console.cloud.google.com/apis/library/youtube.googleapis.com)
→ нажать большую синюю кнопку **ENABLE**"""
            )
            st.markdown(
                """**Шаг 3.** [Настроить OAuth consent screen]
(https://console.cloud.google.com/apis/credentials/consent) →
выбрать **External** → CREATE →
заполнить только три поля (имя, email, email) → SAVE AND CONTINUE →
на странице Scopes нажать **ADD OR REMOVE SCOPES** → в поиске ввести
`youtube.upload` → отметить галочкой → UPDATE → SAVE AND CONTINUE →
на странице Test users добавить свой email → SAVE AND CONTINUE"""
            )
            st.markdown(
                """**Шаг 4.** [Создать OAuth Client ID]
(https://console.cloud.google.com/apis/credentials) →
**CREATE CREDENTIALS** → **OAuth client ID** → Application type:
**Desktop app** → имя любое → CREATE → нажать **DOWNLOAD JSON**"""
            )
            target = CLIENT_SECRET_PATH.resolve()
            st.markdown(
                f"""**Шаг 5.** Переименовать скачанный файл в
`youtube_client_secret.json` и положить ровно сюда:
```
{target}
```"""
            )

            col_x, col_y = st.columns(2)
            with col_x:
                if st.button("📁 Открыть папку secrets/",
                             use_container_width=True,
                             help="Откроет explorer на нужной папке"):
                    import subprocess
                    target.parent.mkdir(parents=True, exist_ok=True)
                    subprocess.Popen(["explorer", str(target.parent)])
            with col_y:
                if st.button("✅ Я положил файл — проверь",
                             use_container_width=True,
                             type="primary"):
                    st.rerun()
        st.session_state["youtube_enabled"] = False
        return

    authorized = is_authorized()
    if authorized:
        st.success("✅ Авторизован")

        # Индикатор дневной квоты (Pacific Time, сброс 00:00 PT)
        try:
            from pipeline.youtube_upload import (
                get_quota_status, DAILY_UPLOAD_LIMIT, reset_quota,
            )
            used, limit = get_quota_status()
            remaining = limit - used
            pct = used / limit if limit else 0
            if remaining == 0:
                st.error(
                    f"📊 Квота: **{used}/{limit}** — исчерпана. "
                    f"Сброс в 00:00 Pacific Time."
                )
            elif remaining <= 2:
                st.warning(f"📊 Квота: **{used}/{limit}** ({remaining} осталось)")
            else:
                st.info(f"📊 Квота: **{used}/{limit}** ({remaining} осталось сегодня)")
            st.progress(min(1.0, pct))
            if used > 0 and st.button("🔄 Сбросить счётчик (тест)",
                                       help="Локальный сброс — не влияет на реальную API-квоту Google"):
                reset_quota()
                st.rerun()
        except Exception:
            pass

        col_a, col_b = st.columns(2)
        with col_a:
            yt_enabled = st.toggle(
                "Авто-загрузка",
                value=st.session_state.get("youtube_enabled", False),
                help="Каждый готовый клип сразу публикуется на канал. "
                     "Hard cap: 6 загрузок/сутки.",
            )
            st.session_state["youtube_enabled"] = yt_enabled
        with col_b:
            if st.button("🔁 Сброс", help="Удалить токен и переавторизоваться"):
                revoke()
                st.rerun()

        if yt_enabled:
            privacy = st.selectbox(
                "Видимость",
                ["unlisted", "public", "private"],
                index=["unlisted", "public", "private"].index(
                    st.session_state.get("youtube_privacy", "unlisted")
                ),
                format_func=lambda v: {
                    "public": "🌍 Public — все видят",
                    "unlisted": "🔗 Unlisted — только по ссылке",
                    "private": "🔒 Private — только ты",
                }[v],
                help="Для первых тестов рекомендую Unlisted",
            )
            st.session_state["youtube_privacy"] = privacy

            # Schedule publish — отложенная публикация в peak hours
            schedule_enabled = st.checkbox(
                "🕒 Отложенная публикация",
                value=st.session_state.get("youtube_schedule_enabled", False),
                help="Загрузка как private + publishAt в указанное время. "
                     "Privacy переключится в private автоматом.",
            )
            st.session_state["youtube_schedule_enabled"] = schedule_enabled
            if schedule_enabled:
                import datetime as _dt
                schedule_date = st.date_input(
                    "Дата публикации",
                    value=st.session_state.get(
                        "youtube_schedule_date",
                        _dt.date.today() + _dt.timedelta(days=1),
                    ),
                )
                schedule_time = st.time_input(
                    "Время (UTC)",
                    value=st.session_state.get(
                        "youtube_schedule_time", _dt.time(18, 0),
                    ),
                    help="В UTC! Peak hours: 17-19 UTC = 20-22 МСК",
                )
                st.session_state["youtube_schedule_date"] = schedule_date
                st.session_state["youtube_schedule_time"] = schedule_time
                _publish_at = _dt.datetime.combine(
                    schedule_date, schedule_time
                ).replace(tzinfo=_dt.timezone.utc).isoformat()
                st.session_state["youtube_publish_at"] = _publish_at
                st.caption(f"📅 Будет опубликовано: `{_publish_at}`")

            context = st.text_input(
                "Контекст источника",
                value=st.session_state.get("youtube_source_context", ""),
                placeholder="Например: «фильм Трансформеры 2007»",
                help="Используется LLM для генерации SEO-описаний",
            )
            st.session_state["youtube_source_context"] = context
            st.caption(
                "ℹ️ Лимит API: ~6 видео/сутки на дефолтной квоте. "
                "Для роста — Audit в Google Cloud."
            )
    else:
        st.warning(
            "⚠️ Токен мёртв или отсутствует. "
            "OAuth в **Testing mode** = refresh token живёт **7 дней**. "
            "Жми «Авторизовать» — займёт 20 секунд."
        )
        if st.button("🔐 Авторизовать YouTube", use_container_width=True,
                     type="primary"):
            try:
                from pipeline.youtube_upload import get_credentials
                with st.spinner("Открываю браузер для consent..."):
                    get_credentials(interactive=True)
                st.success("✅ Авторизация прошла. Перезагружаю...")
                st.rerun()
            except Exception as e:
                st.error(f"❌ {type(e).__name__}: {e}")
        st.session_state["youtube_enabled"] = False


def _render_tiktok_sidebar() -> None:
    """TikTok auth/status and auto-upload controls."""
    try:
        from pipeline.tiktok_upload import (
            CLIENT_SECRET_PATH,
            build_authorization_url,
            exchange_code,
            is_authorized,
            is_setup_complete,
            revoke,
        )
    except Exception as e:
        st.caption(f"⚠ TikTok-модуль недоступен: {e}")
        st.session_state["tiktok_enabled"] = False
        return

    if not is_setup_complete():
        st.warning("Нужен файл `tiktok_client.json` от TikTok for Developers.")
        target = CLIENT_SECRET_PATH.resolve()
        with st.expander("📖 Настройка TikTok API", expanded=True):
            st.markdown(
                "1. Создай приложение на https://developers.tiktok.com.\n"
                "2. Добавь продукты **Login Kit** и **Content Posting API**.\n"
                "3. Включи Direct Post и запроси scope `video.publish`.\n"
                "4. В Login Kit укажи HTTPS Redirect URI.\n"
                "5. Создай файл:"
            )
            st.code(
                json.dumps(
                    {
                        "client_key": "PASTE_CLIENT_KEY",
                        "client_secret": "PASTE_CLIENT_SECRET",
                        "redirect_uri": "https://your-domain.example/tiktok/callback",
                    },
                    indent=2,
                ),
                language="json",
            )
            st.markdown(f"Положить сюда:\n```text\n{target}\n```")
            col_x, col_y = st.columns(2)
            with col_x:
                if st.button("📁 Открыть secrets/", key="tt_open_secrets",
                             use_container_width=True):
                    import subprocess
                    target.parent.mkdir(parents=True, exist_ok=True)
                    subprocess.Popen(["explorer", str(target.parent)])
            with col_y:
                if st.button("✅ Проверить TikTok-файл", key="tt_check_setup",
                             use_container_width=True, type="primary"):
                    st.rerun()
        st.session_state["tiktok_enabled"] = False
        return

    if not is_authorized():
        st.warning("⚠ TikTok не авторизован.")
        try:
            auth_url = build_authorization_url()
            st.markdown(f"[Открыть авторизацию TikTok]({auth_url})")
            with st.expander("Показать OAuth URL"):
                st.code(auth_url, language=None)
            code = st.text_area(
                "Code или полный redirect URL",
                value="",
                placeholder="Вставь сюда code=... или весь URL после редиректа",
                key="tiktok_auth_code",
                height=90,
            )
            if st.button("🔐 Сохранить TikTok-токен", key="tt_save_token",
                         use_container_width=True, type="primary"):
                try:
                    exchange_code(code)
                    st.success("✅ TikTok авторизован. Перезагружаю...")
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ {type(e).__name__}: {e}")
        except Exception as e:
            st.error(f"❌ Не могу собрать OAuth URL: {e}")
        st.session_state["tiktok_enabled"] = False
        return

    st.success("✅ Авторизован")
    col_a, col_b = st.columns(2)
    with col_a:
        tt_enabled = st.toggle(
            "Авто-загрузка",
            value=st.session_state.get("tiktok_enabled", False),
            key="tiktok_enabled_toggle",
            help="Каждый готовый клип будет отправляться в TikTok после рендера.",
        )
        st.session_state["tiktok_enabled"] = tt_enabled
    with col_b:
        if st.button("🔁 Сброс", key="tt_revoke",
                     help="Удалить TikTok-токен и переавторизоваться"):
            revoke()
            st.rerun()

    if tt_enabled:
        privacy_values = [
            "SELF_ONLY",
            "PUBLIC_TO_EVERYONE",
            "MUTUAL_FOLLOW_FRIENDS",
            "FOLLOWER_OF_CREATOR",
        ]
        privacy = st.selectbox(
            "Видимость TikTok",
            privacy_values,
            index=privacy_values.index(
                st.session_state.get("tiktok_privacy", "SELF_ONLY")
            ),
            format_func=lambda v: {
                "PUBLIC_TO_EVERYONE": "Public",
                "MUTUAL_FOLLOW_FRIENDS": "Friends",
                "FOLLOWER_OF_CREATOR": "Followers",
                "SELF_ONLY": "Private",
            }[v],
            help="До audit TikTok обычно разрешает только private/self-only.",
        )
        st.session_state["tiktok_privacy"] = privacy

        context = st.text_input(
            "Контекст источника",
            value=st.session_state.get("youtube_source_context", ""),
            placeholder="Например: сериал Чернобыль: Зона отчуждения",
            key="tiktok_source_context_input",
            help="Общий контекст для SEO YouTube и caption TikTok.",
        )
        st.session_state["youtube_source_context"] = context

        st.session_state["tiktok_disable_comment"] = st.checkbox(
            "Отключить комментарии",
            value=st.session_state.get("tiktok_disable_comment", False),
        )
        c1, c2 = st.columns(2)
        with c1:
            st.session_state["tiktok_disable_duet"] = st.checkbox(
                "Отключить duet",
                value=st.session_state.get("tiktok_disable_duet", False),
            )
        with c2:
            st.session_state["tiktok_disable_stitch"] = st.checkbox(
                "Отключить stitch",
                value=st.session_state.get("tiktok_disable_stitch", False),
            )
        st.session_state["tiktok_is_aigc"] = st.checkbox(
            "Помечать как AI-generated",
            value=st.session_state.get("tiktok_is_aigc", False),
            help="Включай только если хочешь явно поставить TikTok AIGC-label.",
        )
        st.caption(
            "TikTok Direct Post вернет `publish_id`; публичный post_id может "
            "появиться позже после модерации."
        )


@st.cache_data(max_entries=20, show_spinner=False)
def _load_file_bytes(path_str: str) -> bytes:
    """Кеширует чтение файла — на rerun повторно с диска не читает."""
    return Path(path_str).read_bytes()


def _is_process_alive(pid: int) -> bool:
    """Кросс-платформенная проверка живости процесса по PID."""
    if not pid:
        return False
    try:
        # На Windows os.kill(pid, 0) кидает PermissionError если pid существует
        # но процесс чужой. Лучше использовать tasklist через psutil-like подход.
        import ctypes
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED, False, pid)
        if handle == 0:
            return False
        exit_code = ctypes.c_ulong(0)
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        kernel32.CloseHandle(handle)
        # STILL_ACTIVE = 259
        return bool(ok) and exit_code.value == 259
    except Exception:
        return True  # лучше не дёрнуть лишний раз "процесс умер"


def _spawn_worker(params: dict) -> tuple[int, Path, Path]:
    """Запускает pipeline.worker как полностью отдельный процесс.

    Возвращает (pid, progress_file, params_file). Процесс продолжит работу,
    даже если этот Streamlit-скрипт перезапустится / соединение упадёт.
    """
    import subprocess
    import uuid as _uuid

    jobs_dir = Path("cache") / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    job_uuid = _uuid.uuid4().hex[:10]
    params_file = jobs_dir / f"{job_uuid}.params.json"
    progress_file = jobs_dir / f"{job_uuid}.progress.json"
    decision_file = jobs_dir / f"{job_uuid}.decision.json"

    params["progress_file"] = str(progress_file.resolve())
    params["music_decision_file"] = str(decision_file.resolve())
    params_file.write_text(json.dumps(params, ensure_ascii=False), encoding="utf-8")

    # ВАЖНО: используем python.exe (не pythonw.exe) для worker.
    # pythonw МОЛЧА умирает на любой неперехваченной ошибке (нет stderr-консоли),
    # из-за чего мы теряем 20+ минут работы транскрипции и не видим причину.
    # python.exe записывает stderr в наш лог-файл.
    python_exe = str(Path(".venv/Scripts/python.exe").resolve())

    # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — процесс полностью независимый
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

    log_path = jobs_dir / f"{job_uuid}.worker.log"
    log_fd = open(log_path, "w", encoding="utf-8")

    # ENV для нормального чтения кириллицы в логах + ffmpeg/cuDNN PATH
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    proc = subprocess.Popen(
        [
            python_exe, "-m", "pipeline.worker",
            "--params-file", str(params_file.resolve()),
        ],
        stdout=log_fd, stderr=subprocess.STDOUT,
        cwd=str(Path.cwd()),
        creationflags=flags,
        close_fds=True,
        env=env,
    )
    return proc.pid, progress_file, params_file


def _read_progress(progress_file: Path) -> dict | None:
    """Читает progress.json безопасно (worker пишет атомарно)."""
    if not progress_file.exists():
        return None
    try:
        return json.loads(progress_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _write_progress(progress_file: Path, state: dict) -> None:
    """Пишет progress.json атомарно из UI для dead-worker диагностики."""
    try:
        progress_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = progress_file.with_name(f".{progress_file.name}.ui.tmp")
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(tmp, progress_file)
    except OSError:
        pass


def _render_pretty_progress(state: dict) -> None:
    """Рендерит весь блок прогресса по состоянию из progress.json.

    Использует st.html() везде где возможно — он не проходит через
    Markdown-парсер и не превращает HTML в блоки кода из-за индентации.
    """
    elapsed = max(0, time.time() - state.get("started_at", time.time()))
    pct = state.get("pct_overall", 0.0)
    label = state.get("label", "…")
    extra = state.get("extra", {})

    stage_ids = [s[0] for s in _STAGES_META]
    cur_stage = state.get("stage")
    completed: set[str] = set()
    if cur_stage and cur_stage in stage_ids:
        cur_idx = stage_ids.index(cur_stage)
        completed = set(stage_ids[:cur_idx])
    elif state.get("status") == "done":
        completed = set(stage_ids)
    ui_state = {"stage": cur_stage, "completed_stages": completed}

    # 1. Большой прогресс-бар
    st.progress(min(1.0, pct / 100), text=f"🚀 {label}  —  {pct:.0f}%")

    # 2. Лента этапов
    st.markdown(_stage_card_html(ui_state), unsafe_allow_html=True)

    # 3. Три большие метрики
    eta_sec = extra.get("eta_sec", 0)
    speed_x = extra.get("speed_x", 0)
    if pct > 1:
        est_total = elapsed / (pct / 100)
        est_remaining = max(0, est_total - elapsed)
    else:
        est_remaining = 0
    eta_display = _fmt_time(eta_sec) if eta_sec > 0 else _fmt_time(est_remaining)
    eta_sub = "до конца этапа" if eta_sec > 0 else "до конца проекта"

    if speed_x > 0:
        third = _big_metric_html("Скорость", f"{speed_x:.1f}×",
                                 "быстрее реального времени")
    else:
        third = _big_metric_html("Этап", state.get("stage_label", "—"))

    st.markdown(_metrics_row_html([
        _big_metric_html("Прошло", _fmt_time(elapsed)),
        _big_metric_html("Осталось ≈", eta_display, eta_sub),
        third,
    ]), unsafe_allow_html=True)

    # 4. Детальный баннер для текущего этапа
    detail = None
    if cur_stage == "transcribe" and extra:
        pos = extra.get("position_sec", 0)
        tot = extra.get("total_sec", 0)
        segs = extra.get("segments_done", 0)
        detail = (
            f"🎙️ Распознано <b>{_fmt_time(pos)}</b> из <b>{_fmt_time(tot)}</b>"
            f" · <b>{segs}</b> сегментов · скорость {speed_x:.1f}×"
        )
    elif cur_stage == "extract":
        detail = "🎚️ ffmpeg извлекает первую аудиодорожку (моно 16 кГц)…"
    elif cur_stage == "analyze":
        # Показываем счётчик чтобы было видно что не зависло.
        analyze_elapsed = _stage_elapsed(state, "analyze")
        elapsed_str = _fmt_time(analyze_elapsed) if analyze_elapsed > 0 else "…"
        chain_label = _llm_chain_label()
        detail = (
            f"🧠 LLM выбирает лучшие моменты ({chain_label}). "
            f"<b>Думает: {elapsed_str}</b> "
            "<span style='color:#94a3b8;font-size:12px;'>"
            "— на длинном батче это нормально, локальная модель может думать 5-10 мин"
            "</span>"
        )
    elif cur_stage == "clips":
        clips = state.get("clips", [])
        done = sum(1 for c in clips if c.get("status") == "done")
        active = next((c for c in clips if c.get("status") == "active"), None)
        if active:
            detail = (
                f"✂️ Готово <b>{done}/{len(clips)}</b>. "
                f"Сейчас: клип {active.get('i')} — <b>{active.get('title','')[:60]}</b>"
            )

    if detail:
        st.markdown(_detail_banner_html(detail), unsafe_allow_html=True)

    # 5. Грид клипов
    clips = state.get("clips", [])
    if clips:
        done_count = sum(1 for c in clips if c.get("status") == "done")
        failed_count = sum(1 for c in clips if c.get("status") == "failed")
        header = f"### 🎞️ Клипы — готово {done_count}/{len(clips)}"
        if failed_count:
            header += f" · упало {failed_count}"
        st.markdown(header)
        st.markdown(_clips_grid_html(clips), unsafe_allow_html=True)


def _stage_elapsed(state: dict, stage_id: str) -> float:
    """Вычисляет сколько секунд уже работает указанный stage по событиям."""
    events = state.get("events", []) or []
    start_time = None
    for ev in events:
        if ev.get("type") == "stage" and ev.get("stage") == stage_id:
            # elapsed_sec в событии — от начала job, не stage. Берём первый.
            start_time = ev.get("elapsed_sec")
            break
    if start_time is None:
        return 0.0
    return max(0.0, time.time() - state.get("started_at", time.time()) - start_time)


def _run_factory_with_pretty_progress(
    source: str,
    reframe_mode: str,
    add_subtitles: bool,
    add_music: bool,
    clips_count: int,
    target_dur: int,
    sub_overrides: dict | None,
    smart_zoom: float,
) -> None:
    """Запускает pipeline в отдельном процессе и сохраняет ссылку в session_state.

    Real rendering происходит в _show_active_job() при следующем rerun.
    """
    params = {
        "source": source,
        "reframe_mode": reframe_mode,
        "add_subtitles": add_subtitles,
        "add_music": add_music,
        "clips_count": clips_count,
        "target_duration": target_dur,
        "subtitle_overrides": sub_overrides,
        "smart_zoom_out": smart_zoom,
        "youtube_upload": bool(st.session_state.get("youtube_enabled", False)),
        "youtube_privacy": st.session_state.get("youtube_privacy", "unlisted"),
        "youtube_publish_at": (
            st.session_state.get("youtube_publish_at")
            if st.session_state.get("youtube_schedule_enabled")
            else None
        ),
        "youtube_source_context": st.session_state.get(
            "youtube_source_context", ""
        ),
        "tiktok_upload": bool(st.session_state.get("tiktok_enabled", False)),
        "tiktok_privacy": st.session_state.get("tiktok_privacy", "SELF_ONLY"),
        "tiktok_disable_comment": bool(
            st.session_state.get("tiktok_disable_comment", False)
        ),
        "tiktok_disable_duet": bool(
            st.session_state.get("tiktok_disable_duet", False)
        ),
        "tiktok_disable_stitch": bool(
            st.session_state.get("tiktok_disable_stitch", False)
        ),
        "tiktok_is_aigc": bool(st.session_state.get("tiktok_is_aigc", False)),
        "cut_silences": bool(st.session_state.get("cut_silences", True)),
        "silence_min_sec": float(st.session_state.get("silence_min_sec", 0.5)),
        "silence_threshold_db": float(
            st.session_state.get("silence_threshold_db", -30.0)
        ),
        "silence_padding_sec": float(
            st.session_state.get("silence_padding_sec", 0.12)
        ),
        "color_grade": bool(st.session_state.get("color_grade", True)),
        "vocal_isolation_enabled": bool(
            st.session_state.get("vocal_isolation_enabled", True)
        ),
        "speed_enabled": bool(st.session_state.get("speed_enabled", False)),
        "speed_factor": float(st.session_state.get("speed_factor", 1.1)),
        "watermark_enabled": bool(st.session_state.get("watermark_enabled", False)),
        "thumbnail_enabled": bool(st.session_state.get("thumbnail_enabled", False)),
        "generate_music": bool(st.session_state.get("generate_music", False)),
        # music_duration_sec НЕ передаём — render.py авто-считает по длине клипов
        "music_n_variants": int(st.session_state.get("music_n_variants", 3)),
        "music_custom_hint": st.session_state.get("music_custom_hint", ""),
        # Громкость от 0 до 50% — конвертируем в 0.0-0.5
        "music_volume": float(
            st.session_state.get("music_volume_pct", 15)
        ) / 100.0,
        # LoRA-адаптер (только ace_step backend; None = базовая модель)
        "music_lora_repo": st.session_state.get("music_lora_repo"),
        "music_lora_weight": float(
            st.session_state.get("music_lora_weight", 0.0)
        ),
    }
    pid, progress_file, params_file = _spawn_worker(params)

    st.session_state["active_job"] = {
        "pid": pid,
        "progress_file": str(progress_file),
        "params_file": str(params_file),
        "started_at": time.time(),
    }
    st.rerun()


def _fire_browser_notification(
    title: str, body: str, tag: str, set_tab_marker: bool = True
) -> None:
    """Шлёт уведомление через Web Notifications API.

    КРИТИЧНО: используем st.markdown(unsafe_allow_html=True) вместо
    components.html(). components.html() рендерит в sandbox iframe,
    из которого Notifications API заблокирован браузером без
    allow="notifications". st.markdown инжектит JS в основную страницу.

    set_tab_marker=True: ставит 🔔 в title вкладки (для pick — нужно
    attention). False для "done" — задача завершена, маркер не нужен.
    """
    safe_title = title.replace("'", "\\'").replace("\n", " ").replace("<", "&lt;")
    safe_body = body.replace("'", "\\'").replace("\n", " ").replace("<", "&lt;")
    safe_tag = tag.replace("'", "\\'")
    marker_js = (
        "if (!document.title.startsWith('🔔')) { "
        "document.title = '🔔 ' + document.title; }"
        if set_tab_marker
        else "if (document.title.startsWith('🔔')) { "
        "document.title = document.title.replace(/^🔔 /, ''); }"
    )
    js = f"""<script>
(function() {{
    if (!('Notification' in window)) return;
    const fire = () => {{
        if (Notification.permission === 'granted') {{
            try {{
                const n = new Notification('{safe_title}', {{
                    body: '{safe_body}',
                    tag: '{safe_tag}',
                    requireInteraction: true,
                    icon: 'data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22%3E%3Ctext y=%22.9em%22 font-size=%2290%22%3E🎬%3C/text%3E%3C/svg%3E'
                }});
                n.onclick = () => {{ window.focus(); n.close(); }};
            }} catch(e) {{ console.error('Notification error:', e); }}
        }}
    }};
    if (Notification.permission === 'default') {{
        Notification.requestPermission().then(p => {{ if (p === 'granted') fire(); }});
    }} else {{
        fire();
    }}
    {marker_js}
}})();
</script>"""
    st.markdown(js, unsafe_allow_html=True)


def _maybe_request_notification_permission() -> None:
    """Один раз за сессию просит permission через st.markdown (не iframe)."""
    if st.session_state.get("notif_perm_requested"):
        return
    st.session_state["notif_perm_requested"] = True
    st.markdown(
        """<script>
if ('Notification' in window && Notification.permission === 'default') {
    Notification.requestPermission();
}
</script>""",
        unsafe_allow_html=True,
    )


def _clear_tab_title_marker() -> None:
    """Убирает 🔔 из title когда пользователь закончил/выбрал музыку."""
    st.markdown(
        """<script>
if (document.title.startsWith('🔔')) {
    document.title = document.title.replace(/^🔔 /, '');
}
</script>""",
        unsafe_allow_html=True,
    )


def _regen_variant_inline(
    old_variant: dict, variants_list: list[dict], state: dict,
    progress_file_path: str | None = None,
) -> None:
    """Inline-перегенерация одного варианта музыки.

    Удаляет старый wav/mp3/meta.json, запускает music backend на 1 вариант
    с новым seed + текущим LoRA из session_state. Подменяет запись в
    music_variants[].
    """
    import random as _rand
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).parent.resolve()))
    from pipeline.music_gen import generate_variants as _gen, unload_pipeline as _unl

    old_wav = Path(old_variant["path"])
    out_dir = old_wav.parent
    mood = old_variant["mood"]
    duration_sec = float(old_variant["duration_sec"])
    idx = int(old_variant["index"])

    # Удаляем старые файлы
    for ext in (".wav", ".mp3", ".meta.json"):
        f = old_wav.with_suffix(ext)
        if f.exists():
            try:
                f.unlink()
            except OSError:
                pass

    new_seed = _rand.randint(1, 2**31 - 1)
    lora_repo = st.session_state.get("music_lora_repo")
    lora_weight = float(st.session_state.get("music_lora_weight", 0.0))
    custom_hint = st.session_state.get("music_custom_hint", "")

    with st.spinner(
        f"Перегенерирую #{idx} (seed={new_seed}, mood={mood})... "
        "~10-15 сек на ACE-Step"
    ):
        try:
            new_vars = _gen(
                out_dir=out_dir,
                mood=mood,
                duration_sec=duration_sec,
                n_variants=1,
                custom_hint=custom_hint,
                base_seed=new_seed,
                lora_repo=lora_repo,
                lora_weight=lora_weight,
                start_index=idx,
            )
            _unl()
        except Exception as e:
            st.error(f"Перегенерация упала: {e}")
            return

    if not new_vars:
        st.error("Backend не вернул вариант")
        return

    new = new_vars[0]
    new_record = {
        "index": idx,
        "path": str(new.path),
        "preview_path": str(new.preview_path),
        "prompt": new.prompt,
        "seed": new.seed,
        "duration_sec": new.duration_sec,
        "mood": new.mood,
    }
    for i, v in enumerate(variants_list):
        if int(v["index"]) == idx:
            variants_list[i] = new_record
            break
    state["music_variants"] = variants_list

    # Persist в progress.json — иначе rerun перечитает старые variants
    if progress_file_path:
        try:
            pf = Path(progress_file_path)
            if pf.exists():
                cur = json.loads(pf.read_text(encoding="utf-8"))
                cur["music_variants"] = variants_list
                pf.write_text(
                    json.dumps(cur, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        except Exception as e:
            st.warning(f"Не смог сохранить в progress.json: {e}")

    st.success(f"#{idx} перегенерён (seed={new.seed})")


def _render_music_pick(state: dict, job: dict) -> None:
    """Большая интерактивная карточка: 3 аудио + кнопки выбора."""
    variants = state.get("music_variants", [])
    decision_file = Path("cache/jobs") / f"{job['progress_file'].split('/')[-1].split('.')[0]}.decision.json"
    # Берём имя файла из progress_file и меняем суффикс
    pf = Path(job["progress_file"])
    decision_file = pf.parent / pf.name.replace(".progress.json", ".decision.json")

    st.markdown("---")
    st.markdown(
        f"### 🎼 Выбери музыкальный трек ({len(variants)} варианта)"
    )
    st.caption(
        "Сгенерировано Stable Audio Open под доминирующее настроение клипов. "
        "Прослушай и нажми «Использовать» на лучшем. "
        "Завод применит выбранный трек ко всем клипам как фоновую дорожку с ducking."
    )

    cols = st.columns(len(variants))
    for i, v in enumerate(variants):
        with cols[i]:
            st.markdown(f"**Вариант {v['index']}**")
            preview_path = Path(v.get("preview_path") or v["path"])
            wav_path = Path(v["path"])

            if preview_path.exists():
                st.audio(preview_path.read_bytes(),
                         format="audio/mp3" if preview_path.suffix == ".mp3"
                                else "audio/wav")
            else:
                st.warning(f"Файл не найден: {preview_path}")

            # Fallback: если веб-плеер не играет (бывает на некоторых
            # браузерах), пользователь открывает файл в проводнике/системном
            # плеере и слушает там.
            col_a, col_b = st.columns(2)
            with col_a:
                if st.button("📁 Открыть в проводнике",
                             key=f"open_explorer_{v['index']}",
                             use_container_width=True,
                             help="Если веб-плеер не работает — откроется "
                                  "Explorer на папке с файлом, можно "
                                  "послушать в системном плеере"):
                    import subprocess as _sp
                    parent = wav_path.parent.resolve()
                    _sp.Popen(["explorer", "/select,", str(wav_path.resolve())])
            with col_b:
                if st.button("▶ Открыть WAV",
                             key=f"open_wav_{v['index']}",
                             use_container_width=True,
                             help="Откроет файл в системном плеере по умолчанию"):
                    import os as _os
                    try:
                        _os.startfile(str(wav_path.resolve()))
                    except Exception as e:
                        st.error(str(e))

            st.caption(f"⏱ {v['duration_sec']:.0f}с · 🎵 {v['mood']}")

            # Показать промпт + параметры (раскрывающийся блок)
            with st.expander(f"🔍 Что генерила модель (debug #{v['index']})"):
                prompt_text = v.get("prompt", "")
                st.markdown("**Промпт (что отправлено в ACE-Step / Stable Audio):**")
                st.code(prompt_text, language=None)
                st.caption(
                    f"seed={v.get('seed', '?')} · "
                    f"mood выбран LLM: **{v['mood']}** · "
                    f"длительность {v['duration_sec']:.0f}с"
                )
                # Подтягиваем meta JSON если есть (дополнительные параметры модели)
                meta_path = Path(v["path"]).with_suffix(".meta.json")
                if meta_path.exists():
                    try:
                        meta_dict = json.loads(meta_path.read_text(encoding="utf-8"))
                        # LoRA статус — что просили vs что применилось
                        lora_req = meta_dict.get("lora_requested", "none")
                        lora_used = meta_dict.get("lora_used", "none")
                        if lora_req and lora_req != "none":
                            if lora_used == "none":
                                st.warning(
                                    f"⚠️ LoRA `{lora_req}` несовместима с "
                                    "ACE-Step (формат/архитектура), сгенерено "
                                    "на базовой модели."
                                )
                            elif lora_used == lora_req:
                                st.success(f"✅ LoRA `{lora_used}` применена")
                        st.json(
                            {k: meta_dict[k] for k in (
                                "model", "infer_step", "num_inference_steps",
                                "guidance_scale", "scheduler", "cfg_type", "dtype",
                                "lora_requested", "lora_used", "lora_weight",
                            ) if k in meta_dict},
                            expanded=False,
                        )
                    except Exception:
                        pass
            # 🔄 Перегенерация — генерит новый вариант с другим seed
            # (опционально с другим mood + LoRA) поверх текущей дорожки
            if st.button(
                f"🔄 Перегенерить #{v['index']}",
                key=f"music_regen_{v['index']}",
                use_container_width=True,
                help="Заменит этот вариант новой генерацией с другим seed. "
                     "Длится ~10-15 сек на ACE-Step.",
            ):
                _regen_variant_inline(
                    v, variants, state,
                    progress_file_path=job.get("progress_file"),
                )
                st.rerun()

            if st.button(
                f"✅ Использовать #{v['index']}",
                key=f"music_pick_{v['index']}",
                use_container_width=True,
                type="primary",
            ):
                decision_file.write_text(
                    json.dumps({"music_index": v["index"]}),
                    encoding="utf-8",
                )
                _clear_tab_title_marker()
                st.rerun()

    if st.button("❌ Без музыки (пропустить)", use_container_width=True):
        decision_file.write_text(
            json.dumps({"music_index": None}), encoding="utf-8"
        )
        _clear_tab_title_marker()
        st.rerun()


def _show_active_job() -> bool:
    """Если есть активная задача — рендерит её и возвращает True.

    Каждую секунду перерисовывает UI через st.rerun() пока worker не закончит.
    """
    job = st.session_state.get("active_job")
    if not job:
        return False

    progress_file = Path(job["progress_file"])
    state = _read_progress(progress_file) or {
        "status": "running",
        "started_at": job["started_at"],
        "label": "Запускаю worker…",
        "pct_overall": 0,
    }

    pid = state.get("pid") or job["pid"]
    alive = _is_process_alive(pid)
    status = state.get("status", "running")

    # Worker умер а статус не done/error — записываем как краш
    if not alive and status == "running":
        state["status"] = "error"
        state["error"] = (
            f"Worker процесс (PID {pid}) умер не закончив работу. "
            "Смотри cache/jobs/*.worker.log"
        )
        state["label"] = "Worker умер до завершения"
        state["updated_at"] = time.time()
        _write_progress(progress_file, state)
        status = "error"

    # Заголовок + кнопка остановки
    col_h, col_btn = st.columns([4, 1])
    with col_h:
        st.markdown("### ⚙️ Идёт обработка видео")
    with col_btn:
        if status == "running" and st.button("⛔ Остановить", use_container_width=True):
            _kill_worker(pid)
            st.session_state.pop("active_job", None)
            st.rerun()

    _render_pretty_progress(state)

    # Просим permission на первое появление активного джоба
    _maybe_request_notification_permission()

    # Music pick — между анализом и сборкой клипов
    if state.get("awaiting_music_pick") and state.get("music_variants"):
        # Уведомление: один раз за вход в состояние "ждёт выбор"
        if not st.session_state.get(f"notif_pick_{job.get('pid')}"):
            st.session_state[f"notif_pick_{job.get('pid')}"] = True
            _fire_browser_notification(
                "🎼 Shorts Factory — нужен выбор",
                "Завод сгенерировал музыку, выбери лучший вариант",
                f"pick-{job.get('pid')}",
            )
        _render_music_pick(state, job)
        time.sleep(2.0)
        st.rerun()
        return True

    # Финал
    if status == "done":
        meta = state.get("result", {})
        n_clips = len(meta.get("clips", []))
        n_failed = len(meta.get("failed", []))
        total_time = meta.get("total_time_sec", time.time() - job["started_at"])

        # Уведомление о завершении (один раз) — без 🔔-маркера в title
        if not st.session_state.get(f"notif_done_{job.get('pid')}"):
            st.session_state[f"notif_done_{job.get('pid')}"] = True
            _fire_browser_notification(
                "✅ Shorts Factory — готово!",
                f"Создано {n_clips} клипов за {_fmt_time(total_time)}",
                f"done-{job.get('pid')}",
                set_tab_marker=False,
            )

        if n_failed:
            st.warning(
                f"⚠️ Создано **{n_clips}** клипов за {_fmt_time(total_time)} "
                f"(упало {n_failed}). Job ID: `{meta.get('job_id', '?')}`"
            )
        else:
            st.success(
                f"🎉 Создано **{n_clips}** клипов за {_fmt_time(total_time)}! "
                f"Job ID: `{meta.get('job_id', '?')}`"
            )
        if meta.get("job_id"):
            st.session_state["last_job"] = meta["job_id"]

        col_a, col_b = st.columns(2)
        with col_a:
            if st.button("🔄 Новый запуск", use_container_width=True):
                st.session_state.pop("active_job", None)
                st.rerun()
        with col_b:
            if st.button("📁 Открыть готовые работы", use_container_width=True):
                st.session_state.pop("active_job", None)
                st.rerun()

    elif status == "error":
        st.error(f"❌ {state.get('error', 'Неизвестная ошибка')}")
        tb = state.get("traceback")
        if tb:
            with st.expander("Стек ошибки"):
                st.code(tb)
        if st.button("🔄 Сбросить и попробовать снова"):
            st.session_state.pop("active_job", None)
            st.rerun()

    else:
        # Worker ещё работает — авто-обновление через 1 сек
        time.sleep(1.0)
        st.rerun()

    return True


def _kill_worker(pid: int) -> None:
    """Аккуратно убивает worker-процесс."""
    if not pid:
        return
    try:
        import subprocess as _sp
        _sp.run(["taskkill", "/F", "/PID", str(pid), "/T"],
                capture_output=True, timeout=5)
    except Exception:
        pass


def _render_subtitle_preview(
    font_name: str,
    font_size: int,
    text_color: str,
    highlight_color: str,
    outline_color: str,
    outline_width: int,
    uppercase: bool,
    words_per_line: int,
    animation: str,
    margin_v: int,
    margin_h: int,
) -> str:
    """Генерирует standalone HTML-страницу для preview субтитров."""
    sample_words = ["Привет", "это", "тестовый", "текст", "для", "субтитров",
                    "которые", "ты", "настроишь"]
    if uppercase:
        sample_words = [w.upper() for w in sample_words]

    line1 = sample_words[:words_per_line]
    line2 = sample_words[words_per_line : words_per_line * 2]

    # ASS на PlayResY=1920: 1 единица = 1 пиксель на финальном видео.
    # Превью-телефон 270×480px ↔ финал 1080×1920px → scale 0.25.
    # Никаких "магических" множителей — пиксель в пиксель как в реальном MP4.
    scale = 270 / 1080
    fs = font_size * scale
    outline_px = max(0.5, outline_width * scale)

    shadow_parts = []
    for dx in [-1, 0, 1]:
        for dy in [-1, 0, 1]:
            if dx == 0 and dy == 0:
                continue
            shadow_parts.append(
                f"{dx * outline_px}px {dy * outline_px}px 0 {outline_color}"
            )
    shadow = ",".join(shadow_parts)

    if animation in ("karaoke", "instant"):
        spans_1 = ""
        for j, w in enumerate(line1):
            c = highlight_color if j == 1 else text_color
            spans_1 += f'<span style="color:{c}">{w} </span>'
        spans_2 = ""
        for w in line2:
            spans_2 += f'<span style="color:{text_color}">{w} </span>'
    else:
        spans_1 = f'<span style="color:{text_color}">{" ".join(line1)}</span>'
        spans_2 = f'<span style="color:{text_color}">{" ".join(line2)}</span>'

    # margin_v в ASS — расстояние от низа экрана в той же координатной системе
    bottom_px = margin_v * scale  # сразу в пикселях preview-телефона
    bottom_pct = max(1, min(60, bottom_px / 480 * 100))

    font_css = font_name
    google_fonts_link = ""
    if "Montserrat" in font_name:
        google_fonts_link = '<link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@800;900&display=swap" rel="stylesheet">'
        font_css = "Montserrat"
    elif font_name == "Russo One":
        google_fonts_link = '<link href="https://fonts.googleapis.com/css2?family=Russo+One&display=swap" rel="stylesheet">'
        font_css = "'Russo One'"

    anim_label = ANIMATION_STYLES.get(animation, animation)

    return f"""<!DOCTYPE html>
<html><head>
{google_fonts_link}
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{
    display:flex; justify-content:center; align-items:center;
    height:100vh; background:transparent; overflow:hidden;
  }}
  .phone {{
    width:270px; height:480px;
    background:linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
    border-radius:20px;
    position:relative;
    overflow:hidden;
    box-shadow:0 8px 32px rgba(0,0,0,0.5);
    border:2px solid rgba(255,255,255,0.08);
  }}
  .play-icon {{
    position:absolute;
    top:50%; left:50%;
    transform:translate(-50%,-50%);
    width:60px; height:60px;
    border-radius:50%;
    background:rgba(255,255,255,0.1);
    display:flex; align-items:center; justify-content:center;
  }}
  .play-icon::after {{
    content:'';
    border-style:solid;
    border-width:12px 0 12px 22px;
    border-color:transparent transparent transparent rgba(255,255,255,0.25);
    margin-left:4px;
  }}
  .top-label {{
    position:absolute; top:16px; left:0; right:0;
    text-align:center; color:rgba(255,255,255,0.12);
    font:11px sans-serif; letter-spacing:1px;
  }}
  .subs {{
    position:absolute;
    bottom:{bottom_pct}%;
    left:12px; right:12px;
    text-align:center;
    font-family:{font_css}, 'Arial Black', Impact, sans-serif;
    font-size:{fs}px;
    font-weight:800;
    line-height:1.35;
    text-shadow:{shadow};
    letter-spacing:0.5px;
  }}
  .subs .line {{ margin-bottom:3px; }}
  .info {{
    position:absolute; bottom:8px; left:0; right:0;
    text-align:center; color:rgba(255,255,255,0.10);
    font:9px sans-serif;
  }}
  @keyframes pulse {{
    0%,100% {{ opacity:1; }}
    50% {{ opacity:0.7; }}
  }}
  .highlight {{ animation: pulse 1.5s ease-in-out infinite; }}
</style>
</head><body>
<div class="phone">
  <div class="top-label">PREVIEW 9:16</div>
  <div class="play-icon"></div>
  <div class="subs">
    <div class="line">{spans_1}</div>
    <div class="line">{spans_2}</div>
  </div>
  <div class="info">{anim_label} &middot; {font_name} {font_size}pt</div>
</div>
</body></html>"""


# ========== Sidebar ==========
with st.sidebar:
    st.header("⚙️ Настройки")

    reframe_mode = st.radio(
        "Режим вертикализации",
        options=["center", "blur", "smart"],
        format_func=lambda x: {
            "center": "\U0001f4d0 Центр-кроп (быстро)",
            "blur": "\U0001f32b️ Размытый фон",
            "smart": "\U0001f3af Smart tracking (за лицом)",
        }[x],
        index=2,
    )

    if reframe_mode == "smart":
        smart_zoom = st.slider(
            "Приближение (smart)",
            min_value=0.0,
            max_value=1.0,
            value=0.45,
            step=0.05,
            help="0 = максимальный зум на лицо, 1 = полная ширина + блюр",
        )
    else:
        smart_zoom = 0.45

    add_subtitles = st.checkbox("Добавить субтитры", value=True)
    add_music = st.checkbox("Добавить фоновую музыку", value=False)

    # --- Громкость музыки (общий слайдер для add_music и AI-музыки) ---
    # Дефолт из config = 0.15 (15% от голоса). Голос нормализуется до
    # -14 LUFS, музыка остаётся фоном даже без ducking.
    music_volume_pct = st.slider(
        "🔊 Громкость музыки",
        min_value=0, max_value=50,
        value=int(st.session_state.get("music_volume_pct", 15)),
        step=1,
        format="%d%%",
        help="Процент от громкости голоса. Голос нормализуется до -14 LUFS, "
             "музыка стартует на этой громкости и дополнительно гасится "
             "когда говорят (sidechain ducking 8x).",
    )
    st.session_state["music_volume_pct"] = music_volume_pct

    # --- Уникализация контента (анти-Content-ID) ---
    cut_silences = st.checkbox(
        "✂️ Резать длинные паузы",
        value=st.session_state.get("cut_silences", True),
        help="Убирает мёртвые моменты между фразами. Можно настроить агрессивность ниже.",
    )
    st.session_state["cut_silences"] = cut_silences

    if cut_silences:
        sil_aggro = st.select_slider(
            "Агрессивность",
            options=["осторожно", "средне", "агрессивно", "макс"],
            value=st.session_state.get("silence_aggro", "средне"),
            help="«Осторожно» оставляет естественные паузы. «Макс» делает речь почти без воздуха.",
        )
        st.session_state["silence_aggro"] = sil_aggro
        # Маппим на численные параметры
        _aggro_map = {
            # (min_silence_sec, threshold_db, keep_padding_sec)
            "осторожно":  (0.7, -32.0, 0.20),
            "средне":     (0.5, -30.0, 0.12),
            "агрессивно": (0.35, -28.0, 0.08),
            "макс":       (0.25, -26.0, 0.05),
        }
        sm, st_db, pad = _aggro_map[sil_aggro]
        st.session_state["silence_min_sec"] = sm
        st.session_state["silence_threshold_db"] = st_db
        st.session_state["silence_padding_sec"] = pad
        st.caption(
            f"Режу тишину ≥ {sm}с громкостью ≤ {st_db}dB, "
            f"оставляю {int(pad * 1000)}мс воздуха."
        )

    color_grade = st.checkbox(
        "🎨 Тонкий цветокор (уникализация)",
        value=st.session_state.get("color_grade", True),
        help="Малозаметные сдвиги saturation/gamma/RGB ±2-4%. Глазу почти "
             "не видно, но fingerprint видео уже другой — снижает риск "
             "Content ID match. Каждый клип получает свой набор.",
    )
    st.session_state["color_grade"] = color_grade

    # VRAM monitor — видно сколько занято перед запуском ACE-Step
    try:
        import torch as _torch_vram
        if _torch_vram.cuda.is_available():
            _free_gb = _torch_vram.cuda.mem_get_info()[0] / 1024**3
            _total_gb = _torch_vram.cuda.get_device_properties(0).total_memory / 1024**3
            _used = _total_gb - _free_gb
            _pct = int(_used / _total_gb * 100)
            st.metric(
                f"🎮 VRAM ({_pct}% занято)",
                f"{_free_gb:.1f} / {_total_gb:.1f} GB свободно",
            )
    except Exception:
        pass

    # Speed control — ускорить клип для динамики
    speed_enabled = st.checkbox(
        "⚡ Ускорить клипы",
        value=st.session_state.get("speed_enabled", False),
        help="Чуть-чуть ускоряет видео + аудио. Полезно для динамики Shorts.",
    )
    st.session_state["speed_enabled"] = speed_enabled
    if speed_enabled:
        speed_factor = st.slider(
            "Множитель скорости", 1.0, 1.5, 1.1, 0.05,
            help="1.0=обычно, 1.1=+10% быстрее, 1.25=хороший баланс",
        )
        st.session_state["speed_factor"] = speed_factor

    # Watermark — лого в углу
    watermark_enabled = st.checkbox(
        "💧 Watermark/лого",
        value=st.session_state.get("watermark_enabled", False),
        help="Накладывает PNG из assets/watermark.png в угол клипа. "
             "Положи свой лого по этому пути перед включением.",
    )
    st.session_state["watermark_enabled"] = watermark_enabled

    # Auto-thumbnail — лучший кадр с лицом + title
    thumbnail_enabled = st.checkbox(
        "🖼️ Auto-thumbnail (face+overlay)",
        value=st.session_state.get("thumbnail_enabled", False),
        help="MediaPipe ищет лучший кадр с лицом, Pillow рисует title overlay. "
             "Передаётся в YouTube как кастомный thumbnail.",
    )
    st.session_state["thumbnail_enabled"] = thumbnail_enabled

    # Vocal isolation toggle — иногда хочется выключить чтобы услышать
    # оригинальный звук без demucs-обработки и сравнить.
    try:
        import yaml as _yaml_vi
        _default_vi = (_yaml_vi.safe_load(
            open("config.yaml", encoding="utf-8")
        ) or {}).get("safety", {}).get("vocal_isolation_enabled", True)
    except Exception:
        _default_vi = True
    vocal_isolation_enabled = st.checkbox(
        "🎤 Vocal isolation (demucs)",
        value=st.session_state.get("vocal_isolation_enabled", _default_vi),
        help="Отделяет голос актёров от оригинальной музыки/score через demucs "
             "и убирает фоновую дорожку. Снижает риск Content ID на чужую "
             "музыку. Выключи если хочешь услышать как клип звучит с "
             "оригинальным звуком.",
    )
    st.session_state["vocal_isolation_enabled"] = vocal_isolation_enabled

    # AI-narration отключён: бесплатные LLM не дают качественного русского
    # пересказа (анахронизмы, скрытый дубляж, странная стилистика).
    # Код сохранён в pipeline/narration.py для возможного возврата на
    # платных LLM (Claude/GPT-4/Gemini Pro). См. config.yaml → narration.

    # --- AI генерация музыки ---
    # Backend выбирается в config.yaml → music.backend
    try:
        from pipeline.utils import load_config as _load_cfg
        _music_cfg = _load_cfg().get("music", {})
        _mb = _music_cfg.get("backend", "stable_audio_open")
    except Exception:
        _music_cfg = {}
        _mb = "stable_audio_open"
    _backend_label = {
        "stable_audio_open": "Stable Audio Open",
        "ace_step": "ACE-Step 1.5",
    }.get(_mb, _mb)
    generate_music = st.checkbox(
        f"🎼 AI-музыка ({_backend_label})",
        value=st.session_state.get("generate_music", False),
        help=f"Backend: {_backend_label} (config.yaml → music.backend). "
             "После анализа сгенерит 3 варианта фоновой музыки под настроение "
             "клипов. Ты выберешь лучший — он пойдёт фоном на все клипы.",
    )
    st.session_state["generate_music"] = generate_music

    if generate_music:
        if _mb == "ace_step":
            st.caption(
                "🎵 ACE-Step: длина трека = длине самого длинного клипа. "
                "Запускается в isolated venv (.venv-acestep). "
                "~10-15 сек на вариант на RTX 4070 Ti."
            )
        else:
            st.caption(
                "🎵 Длина трека = длине самого длинного клипа. "
                "Если клип >47с, музыка зациклится (Stable Audio Open до 47с)."
            )
        music_hint = st.text_input(
            "Доп. промпт (опционально)",
            value=st.session_state.get("music_custom_hint", ""),
            placeholder="например: 80s synthwave, no drums",
            help="Английский, добавляется в конец промпта по mood",
        )
        st.session_state["music_custom_hint"] = music_hint

        # LoRA адаптеры (только ace_step backend)
        if _mb == "ace_step":
            try:
                _lora_opts = _music_cfg.get("lora_options", [])
            except Exception:
                _lora_opts = []
            if not _lora_opts:
                _lora_opts = [{"name": "Без LoRA (базовая модель)",
                               "repo": None, "weight": 0.0}]
            _lora_labels = [opt["name"] for opt in _lora_opts]
            _selected_lora_name = st.selectbox(
                "Стилевой LoRA",
                options=_lora_labels + ["✏️ Custom HF repo..."],
                index=_lora_labels.index(
                    st.session_state.get("music_lora_name", _lora_labels[0])
                ) if st.session_state.get("music_lora_name") in _lora_labels else 0,
                help="LoRA дообучают ACE-Step под конкретный жанр. "
                     "Свои варианты добавляй в config.yaml → music.lora_options.",
            )
            if _selected_lora_name == "✏️ Custom HF repo...":
                _custom_repo = st.text_input(
                    "HF repo id (например: username/ace-step-phonk-lora)",
                    value=st.session_state.get("music_lora_custom_repo", ""),
                )
                _custom_weight = st.slider(
                    "Вес LoRA", 0.0, 1.5, 1.0, 0.1,
                    help="0 = базовая модель, 1.0 = полный эффект LoRA",
                )
                st.session_state["music_lora_name"] = _selected_lora_name
                st.session_state["music_lora_custom_repo"] = _custom_repo
                st.session_state["music_lora_repo"] = _custom_repo or None
                st.session_state["music_lora_weight"] = float(_custom_weight)
            else:
                _picked = next(
                    o for o in _lora_opts if o["name"] == _selected_lora_name
                )
                st.session_state["music_lora_name"] = _selected_lora_name
                st.session_state["music_lora_repo"] = _picked.get("repo")
                st.session_state["music_lora_weight"] = float(_picked.get("weight", 0.0))

        _render_music_preflight()

    st.divider()
    clips_count = st.slider("Клипов из видео", 1, 15, 6)
    target_dur = st.slider("Длительность клипа (сек)", 15, 90, 35)

    # --- Настройки субтитров ---
    if add_subtitles:
        st.divider()
        st.subheader("Субтитры")

        if "sub_settings" not in st.session_state:
            st.session_state["sub_settings"] = dict(GOLDEN_STANDARD)

        ss = st.session_state["sub_settings"]

        if st.button("⭐ Золотой стандарт", use_container_width=True,
                      help="Сбросить к проверенным настройкам для YouTube Shorts"):
            st.session_state["sub_settings"] = dict(GOLDEN_STANDARD)
            st.rerun()

        # Subtitle presets — быстрая замена десятка слайдеров
        try:
            from pipeline.subtitle import SUBTITLE_PRESETS, PRESET_LABELS
            preset_keys = list(SUBTITLE_PRESETS.keys())
            preset_pick = st.selectbox(
                "Пресет стиля",
                preset_keys,
                index=0,
                format_func=lambda k: PRESET_LABELS.get(k, k),
                help="Готовые пресеты: TikTok Bold, Mr.Beast, Minimal, Story Book",
            )
            if st.button("📥 Применить пресет", use_container_width=True):
                merged = dict(GOLDEN_STANDARD)
                merged.update(SUBTITLE_PRESETS[preset_pick])
                st.session_state["sub_settings"] = merged
                st.rerun()
        except Exception as _e_ps:
            st.caption(f"Пресеты недоступны: {_e_ps}")

        font_names = list(AVAILABLE_FONTS.keys())
        current_font_idx = (
            font_names.index(ss["font_name"])
            if ss["font_name"] in font_names
            else 0
        )
        sub_font = st.selectbox(
            "Шрифт",
            font_names,
            index=current_font_idx,
            help="Все шрифты поддерживают кириллицу",
        )
        ss["font_name"] = sub_font

        sub_font_size = st.slider(
            "Размер шрифта (pt на 1080×1920)", 20, 140,
            ss.get("font_size", 72),
            help="60-90pt — стандарт для YouTube Shorts. Меньше 40 — нечитаемо на телефоне.",
        )
        ss["font_size"] = sub_font_size

        anim_keys = list(ANIMATION_STYLES.keys())
        current_anim_idx = (
            anim_keys.index(ss["animation"])
            if ss["animation"] in anim_keys
            else 0
        )
        sub_animation = st.selectbox(
            "Анимация",
            anim_keys,
            format_func=lambda k: ANIMATION_STYLES[k],
            index=current_anim_idx,
        )
        ss["animation"] = sub_animation

        c1, c2 = st.columns(2)
        with c1:
            sub_text_color = st.color_picker(
                "Цвет текста", ss.get("text_color", "#FFFFFF")
            )
            ss["text_color"] = sub_text_color
        with c2:
            sub_highlight_color = st.color_picker(
                "Цвет подсветки",
                ss.get("highlight_color", "#FFD700"),
                help="Цвет активного слова",
            )
            ss["highlight_color"] = sub_highlight_color

        c3, c4 = st.columns(2)
        with c3:
            sub_outline_color = st.color_picker(
                "Цвет контура", ss.get("outline_color", "#000000")
            )
            ss["outline_color"] = sub_outline_color
        with c4:
            sub_outline_width = st.slider(
                "Толщина контура", 1, 14, ss.get("outline_width", 6),
                help="На крупном шрифте (70+pt) нужна толщина 6-10 для контраста"
            )
            ss["outline_width"] = sub_outline_width

        sub_uppercase = st.checkbox("КАПС", value=ss.get("uppercase", True))
        ss["uppercase"] = sub_uppercase

        sub_words_per_line = st.slider(
            "Слов в строке", 1, 5, ss.get("words_per_line", 3)
        )
        ss["words_per_line"] = sub_words_per_line

        sub_margin_v = st.slider(
            "Отступ снизу", 40, 500, ss.get("margin_v", 220),
            help="Расстояние от нижнего края видео в пикселях (1080×1920)"
        )
        ss["margin_v"] = sub_margin_v

    # --- YouTube ---
    st.divider()
    st.subheader("📺 YouTube")
    _render_youtube_sidebar()

    # --- TikTok ---
    st.divider()
    st.subheader("🎵 TikTok")
    _render_tiktok_sidebar()

    st.divider()
    st.caption(f"faster-whisper → {_llm_chain_label()} → FFmpeg NVENC")
    st.caption("RTX 4070 Ti · Локально")

# ========== Tabs ==========
tab1, tab2 = st.tabs(["\U0001f680 Запуск", "\U0001f4c1 Готовые работы"])

with tab1:
    # Активная задача и форма запуска — взаимоисключающие.
    # Используем if/else вместо st.stop() — он иногда не "ловит" вложенные
    # элементы из-за порядка выполнения Streamlit.
    if st.session_state.get("active_job"):
        _show_active_job()
        # Дальше внутри tab1 ничего не рендерим
        st.stop()

    main_col, preview_col = st.columns([3, 2])

    with main_col:
        source_type = st.radio(
            "Источник видео",
            ["\U0001f4c2 Путь(и) к файлу", "\U0001f4e4 Загрузить", "\U0001f310 URL(ы)"],
            horizontal=True,
        )

        sources: list[str] = []

        def _clean_path(raw: str) -> Path | None:
            """Нормализует один путь (кавычки, '& ', '\\\\?\\', слэши)."""
            cleaned = raw.strip()
            for ch in ('"', "'", "`"):
                cleaned = cleaned.strip(ch)
            cleaned = cleaned.strip()
            if cleaned.startswith("& "):
                cleaned = cleaned[2:].lstrip()
            if cleaned.startswith("\\\\?\\"):
                cleaned = cleaned[4:]
            if not cleaned:
                return None
            normalized = cleaned.replace("/", "\\")
            p = Path(normalized)
            try:
                p = p.resolve()
            except OSError:
                pass
            return p

        if source_type == "\U0001f4c2 Путь(и) к файлу":
            st.info(
                "\U0001f4a1 Несколько файлов — каждый с НОВОЙ строки. "
                "LLM выберет лучшие моменты ИЗ ВСЕХ видео сразу."
            )
            paths_raw = st.text_area(
                "Путь(и) к видео",
                placeholder=("C:\\Videos\\episode1.mkv\n"
                             "C:\\Videos\\episode2.mkv\n"
                             "C:\\Videos\\episode3.mkv"),
                height=120,
            )
            if paths_raw:
                lines = [ln for ln in paths_raw.splitlines() if ln.strip()]
                valid: list[str] = []
                for ln in lines:
                    p = _clean_path(ln)
                    if p is None:
                        continue
                    if p.exists() and p.is_file():
                        size_gb = p.stat().st_size / (1024 ** 3)
                        st.success(f"✅ {p.name} — {size_gb:.2f} ГБ")
                        valid.append(str(p))
                    else:
                        st.error(f"❌ Файл не найден: {p}")
                if valid:
                    sources = valid
                    if len(sources) > 1:
                        st.caption(f"📚 Будет обработано: **{len(sources)}** видео")

        elif source_type == "\U0001f4e4 Загрузить":
            st.caption(
                "⚠️ Для файлов > 500 МБ лучше указать путь — загрузка через "
                "браузер буферизует в памяти Streamlit. Можно выбрать несколько."
            )
            uploaded_list = st.file_uploader(
                "Перетащи одно или несколько видео",
                type=["mp4", "mov", "mkv", "webm", "avi"],
                accept_multiple_files=True,
            )
            if uploaded_list:
                upload_dir = Path("cache") / "uploads"
                upload_dir.mkdir(parents=True, exist_ok=True)
                valid_uploads: list[str] = []
                for uploaded in uploaded_list:
                    safe_name = Path(uploaded.name).name
                    tmp = upload_dir / safe_name
                    try:
                        with open(tmp, "wb") as f:
                            shutil.copyfileobj(uploaded, f, length=8 * 1024 * 1024)
                    except Exception as e:
                        st.error(f"❌ Не удалось сохранить {uploaded.name}: {e}")
                        continue
                    if tmp.exists():
                        size_mb = tmp.stat().st_size / (1024 ** 2)
                        st.success(f"✅ {uploaded.name} — {size_mb:.0f} МБ")
                        valid_uploads.append(str(tmp))
                if valid_uploads:
                    sources = valid_uploads
                    if len(sources) > 1:
                        st.caption(f"📚 Будет обработано: **{len(sources)}** видео")

        else:
            urls_raw = st.text_area(
                "URL(ы) видео (по одному на строку)",
                placeholder=("https://www.youtube.com/watch?v=AAA\n"
                             "https://www.youtube.com/watch?v=BBB"),
                height=100,
            )
            if urls_raw:
                lines = [ln.strip() for ln in urls_raw.splitlines() if ln.strip()]
                valid_urls = [u for u in lines if u.startswith("http")]
                if valid_urls:
                    sources = valid_urls
                    st.info(
                        f"Будет скачано через yt-dlp: **{len(sources)}** видео"
                    )

        if sources:
            # Pre-flight check экран — что готово/не готово
            with st.expander("🛫 Pre-flight: проверка системы", expanded=False):
                try:
                    from pipeline.preflight import run_all_checks
                    import yaml as _y_pf
                    _cfg_pf = _y_pf.safe_load(open("config.yaml", encoding="utf-8")) or {}
                    try:
                        _local = _y_pf.safe_load(open("config.local.yaml", encoding="utf-8")) or {}
                        for k, v in _local.items():
                            if isinstance(v, dict) and k in _cfg_pf:
                                _cfg_pf[k].update(v)
                            else:
                                _cfg_pf[k] = v
                    except FileNotFoundError:
                        pass
                    checks = run_all_checks(_cfg_pf)
                    blocking_fail = False
                    for ok, label, detail in checks:
                        icon = "✅" if ok else "❌"
                        st.write(f"{icon} **{label}** — {detail}")
                        # YouTube/TikTok creds — opt-in, не блокируют
                        if not ok and label.startswith(("Kimi", "Ollama", "GPU", "Диск")):
                            blocking_fail = True
                    if blocking_fail:
                        st.warning(
                            "Есть проблемы — завод может упасть. Включи Ollama "
                            "или поставь Kimi key в config.local.yaml."
                        )
                except Exception as e:
                    st.warning(f"Pre-flight упал: {e}")

            col_btn, col_info = st.columns([2, 3])
            with col_btn:
                run_btn = st.button(
                    "\U0001f680 Запустить завод",
                    type="primary",
                    use_container_width=True,
                )
            with col_info:
                videos_label = f"{len(sources)} видео" if len(sources) > 1 else "1 видео"
                st.caption(
                    f"Источников: **{videos_label}** | "
                    f"Режим: **{reframe_mode}** | "
                    f"Субтитры: **{'да' if add_subtitles else 'нет'}** | "
                    f"Клипов: **{clips_count}**"
                )

            if run_btn:
                if _pipeline_error:
                    st.error(f"Pipeline не загружен: {_pipeline_error}")
                else:
                    sub_overrides = None
                    if add_subtitles:
                        sub_overrides = dict(
                            st.session_state.get("sub_settings", GOLDEN_STANDARD)
                        )
                    # Передаём list ВСЕГДА — single даже из 1 элемента.
                    # run_job() принимает str | list[str], multi-flow в render.py.
                    _run_factory_with_pretty_progress(
                        source=sources if len(sources) > 1 else sources[0],
                        reframe_mode=reframe_mode,
                        add_subtitles=add_subtitles,
                        add_music=add_music,
                        clips_count=clips_count,
                        target_dur=target_dur,
                        sub_overrides=sub_overrides,
                        smart_zoom=smart_zoom,
                    )
        else:
            st.info("\U0001f446 Выбери источник видео")

    # --- Preview субтитров ---
    with preview_col:
        if add_subtitles:
            st.subheader("Предпоказ субтитров")
            ss = st.session_state.get("sub_settings", GOLDEN_STANDARD)
            preview_html = _render_subtitle_preview(
                font_name=ss.get("font_name", "Montserrat ExtraBold"),
                font_size=ss.get("font_size", 28),
                text_color=ss.get("text_color", "#FFFFFF"),
                highlight_color=ss.get("highlight_color", "#FFD700"),
                outline_color=ss.get("outline_color", "#000000"),
                outline_width=ss.get("outline_width", 4),
                uppercase=ss.get("uppercase", True),
                words_per_line=ss.get("words_per_line", 3),
                animation=ss.get("animation", "karaoke"),
                margin_v=ss.get("margin_v", 160),
                margin_h=ss.get("margin_h", 60),
            )
            components.html(preview_html, height=520)
            st.caption(
                "Меняй настройки в боковой панели — превью обновится в реальном времени."
            )

# ========== Tab 2 ==========
with tab2:
    output_root = Path("output")
    if not output_root.exists():
        output_root.mkdir(parents=True, exist_ok=True)

    job_dirs = sorted(
        [d for d in output_root.iterdir() if d.is_dir()],
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )

    if not job_dirs:
        st.info("Готовых работ пока нет. Запусти завод.")
    else:
        for job_dir in job_dirs[:10]:
            meta_file = job_dir / "meta.json"
            if not meta_file.exists():
                continue
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            is_latest = meta.get("job_id") == st.session_state.get("last_job")

            with st.expander(
                f"\U0001f3ac {meta['job_id']} — "
                f"{len(meta.get('clips', []))} клипов "
                f"({meta.get('reframe_mode', '?')})",
                expanded=is_latest,
            ):
                clips = meta.get("clips", [])
                total_dur_val = sum(c.get("duration", 0) for c in clips)
                c1, c2, c3 = st.columns(3)
                with c1:
                    st.metric("Клипов", len(clips))
                with c2:
                    st.metric("Длительность", f"{total_dur_val:.0f}с")
                with c3:
                    st.metric("Режим", meta.get("reframe_mode", "?"))

                # --- Music variants (если есть) ---
                music_variants_dir = job_dir / "music_variants"
                if music_variants_dir.exists():
                    music_files = sorted(music_variants_dir.glob("variant_*.wav"))
                    if music_files:
                        with st.expander(
                            f"🎼 Сгенерированная музыка ({len(music_files)} вариантов) — "
                            "прослушай и применить к клипам",
                            expanded=False,
                        ):
                            st.caption(
                                "Если в финальных клипах нет фоновой музыки — "
                                "выбери вариант и нажми «Применить к этим клипам»."
                            )
                            mcols = st.columns(len(music_files))
                            for mi, mf in enumerate(music_files):
                                with mcols[mi]:
                                    st.markdown(f"**Вариант {mi + 1}**")
                                    # bytes — Path-объект триггерит
                                    # polling_path_watcher и крашит Streamlit
                                    # на Windows когда worker меняет файлы.
                                    st.audio(_load_file_bytes(str(mf)))
                                    if st.button(
                                        f"🎵 Применить #{mi + 1} ко всем клипам",
                                        key=f"applymus_{meta['job_id']}_{mi}",
                                        use_container_width=True,
                                    ):
                                        try:
                                            from pipeline.audio_mix import mix_with_music
                                            from pipeline.utils import load_config as _lc
                                            cfg = _lc()
                                            with st.spinner(
                                                f"Микширую {len(clips)} клипов..."
                                            ):
                                                for c in clips:
                                                    src = job_dir / c["file"]
                                                    tmp = src.with_suffix(
                                                        ".withmusic.mp4"
                                                    )
                                                    mix_with_music(
                                                        src, mf, tmp, cfg
                                                    )
                                                    src.unlink()
                                                    tmp.rename(src)
                                            st.success(
                                                f"✅ Музыка #{mi + 1} применена "
                                                f"к {len(clips)} клипам"
                                            )
                                            st.rerun()
                                        except Exception as e:
                                            st.error(f"❌ {e}")

                st.divider()
                cols = st.columns(2)
                for i, clip in enumerate(clips):
                    with cols[i % 2]:
                        clip_path = job_dir / clip["file"]
                        if clip_path.exists():
                            # bytes (через @st.cache_data на _load_file_bytes).
                            # Path-объект тут НЕЛЬЗЯ: Streamlit добавляет
                            # путь в polling_path_watcher, который крашит
                            # процесс с access violation (0xc0000374) когда
                            # worker меняет файлы в output/cache на Windows.
                            video_bytes = _load_file_bytes(str(clip_path))
                            st.video(video_bytes, format="video/mp4")
                            st.download_button(
                                f"⬇️ Скачать clip_{i + 1:02d}.mp4",
                                data=video_bytes,
                                file_name=f"short_{meta['job_id']}_{i+1:02d}.mp4",
                                mime="video/mp4",
                                key=f"dl_{meta['job_id']}_{i}",
                            )
                        st.markdown(f"**{clip.get('title', '')}**")
                        if clip.get("description"):
                            st.caption(clip["description"])
                        dur = clip.get("duration", 0)
                        mood = clip.get("music_mood", "")
                        st.caption(f"⏱ {dur:.0f}с · \U0001f3b5 {mood}")
                        if clip.get("tags"):
                            st.code(
                                " ".join(f"#{t}" for t in clip["tags"]),
                                language=None,
                            )

                        # --- YouTube статус/кнопка загрузки ---
                        _render_clip_youtube_block(clip, clip_path, meta, i)
                        _render_clip_tiktok_block(clip, clip_path, meta, i)
