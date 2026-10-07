#!/usr/bin/env python3
"""Bot săn giftcode Telegram — BROWSER-ONLY.

Browser automation via Edge/CDP is the only submission route.

The bot never submits codes through HTTP APIs and does not use CAPTCHA-solving APIs.
Site-specific selectors and result handling live in browser_engine.py.
"""
from __future__ import annotations

import asyncio
import csv
import gc
import hashlib
import json
import os
import random
import re
import shutil
import socket
import tempfile
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from telethon import TelegramClient, events
from telethon.utils import get_peer_id
from telethon.errors.rpcerrorlist import AuthKeyDuplicatedError
from telethon.network import ConnectionTcpAbridged
from telethon.tl.types import DocumentAttributeVideo, MessageEntitySpoiler, MessageMediaDocument

from config import Config, get_effective_domain_accounts, is_domain_enabled
from browser_site_profiles import get_site_profile
from logger_setup import logger, reset_log_context, set_log_context
from code_validator import CodeValidator
from image_code_extractor import (
    _OCR_EXECUTOR,
    VIDEO_FORMATS,
    extract_frames_from_video,
    get_image_extractor,
    warmup_image_extractor,
    shutdown_ocr_executor,
)
from database import init_database
from durable_inbox import DurableInbox
from tg_ingest import IngressStage, ReceiverConfig, TelegramReceiver
from freshness import first_known_age
from monitoring import init_monitoring, stop_monitoring
from features import (
    BOT_VERSION,
    command_registry,
    get_shutdown_handler,
    print_version_info,
    register_default_commands,
    setup_admin_commands,
)
from timing import RequestTimer, get_current_timer, reset_current_timer, set_current_timer
from media_download_manager import MediaDownloadManager, cleanup_stale_files
from browser_adapter import BrowserEngineAdapter
from queue_manager import get_queue_manager, init_queue_manager
from dashboard import (
    disable_console_logging,
    report_download_finished,
    report_download_started,
    start_dashboard,
    stop_dashboard,
    update_dashboard,
)

# ═══ Browser engine ═══
# Browser engine is mandatory: submissions cannot use an HTTP/API fallback.
try:
    import browser_engine

    _BROWSER_ENGINE_OK = True
except Exception as _browser_import_err:  # pragma: no cover
    browser_engine = None
    _BROWSER_ENGINE_OK = False
    logger.error(
        f"❌ [Browser] Không load được browser_engine.py ({_browser_import_err}) — "
        f"Bot không thể submit khi browser_engine lỗi. "
        f"Kiểm tra: pip install playwright --break-system-packages"
    )

def _notify_browser_exhausted(domain: str, username: str) -> None:
    """Báo TabPool: tài khoản hết lượt → nhường tab cho tài khoản dự phòng."""
    if browser_engine is not None:
        try:
            browser_engine.mark_account_exhausted(domain, username)
        except Exception:
            pass


def _prefilled_accounts_for(domain: str) -> list[str]:
    if browser_engine is None:
        return []
    try:
        return [str(x).lower() for x in browser_engine.get_prefilled_accounts(domain)]
    except Exception:
        return []


# ✅ Executor riêng: tránh mặc định event loop share với OCR/DB/media.
# Nếu để None, nhiều task CPU-bound/IO-bound từ các domain cùng lúc sẽ chặn nhau.
_MEDIA_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="media-io")
_DB_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="db-io")
# ✅ Tách đường ghi ingress khỏi các thao tác state/maintenance của inbox.
# enqueue tin mới chỉ dùng pool 2 thread; claim/retry/complete/maintenance
# dùng pool state 4 thread nên retry-storm không làm chậm message mới.
_INBOX_INGRESS_EXECUTOR = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="inbox-ingress",
)
_INBOX_STATE_EXECUTOR = ThreadPoolExecutor(
    max_workers=6,
    thread_name_prefix="inbox-state",
)
# Alias tạm thời để các call-site state hiện hữu giữ nguyên và dễ rà soát.
_INBOX_EXECUTOR = _INBOX_STATE_EXECUTOR

_SITE_LOG_LABELS = {
    "xx88code.com": "XX88",
    "liverr88.net": "RR88",
    "gg88live.tv": "GG88",
    "livemm88.net": "MM88",
    "tangquaqq88.com": "QQ88",
    "hi88-freecode.pages.dev": "HI88",
}
_PROMO_LABEL_INLINE_RE = re.compile(r"(?im)^\s*M(?:Ã|A|4){1,2}\s*[:\-]\s*[A-Za-z0-9_]{2,14}\s*$")
_PROMO_LABEL_ONLY_RE = re.compile(r"(?im)^\s*M(?:Ã|A|4){1,2}\s*[:\-]?\s*$")
_XX88_BIGWIN_RE = re.compile(r"^[A-Za-z0-9](\*[A-Za-z0-9]){4,}$")
_XX88_OCR_BANNER_TOKENS = frozenset(
    {
        "THUMAY", "TOPWIN", "MEGA", "LIVE", "MEGALIVE", "TANG", "QUA",
        "PHAT", "CODE", "FREE", "GIFTCODE", "GIFT", "BONUS", "PROMO",
        "PROMOTION", "KHUYENMAI", "VIP", "GAME", "WIN", "WINNER",
    }
)
_QQ88_OCR_NOISE_RE = re.compile(
    r"(?:SCATTER|OLYMPUS|ANUBIS|MAHJONG|AZTEC|OFAKIND|"
    r"DCHOIDPHTTI|DCHOI.*PHTTI|QUAY(?:MINPH|MIENPHI)|"
    r"WINMULTIPLIER|BETVND|PROFITVND|BALANCEVND|"
    r"BET(?:SIZE|LEVEL)\d*|ROUND\d+|\d+WAYS)",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_WWW_RE = re.compile(r"www\.\S+", re.IGNORECASE)
_TME_RE = re.compile(r"t\.me/\S+", re.IGNORECASE)
_DOMAIN_RE = re.compile(r"\b[a-zA-Z0-9.-]+\.(?:com|net|org|vn|app|info)\b", re.IGNORECASE)
_HASHTAG_RE = re.compile(r"#\S+")
_CURRENCY_RE = re.compile(r"\d[\d.,]{3,}\s*(?:VND|VNĐ)\b", re.IGNORECASE)
_CODE_MARKER_RE = re.compile(
    r"NHẬN\s+CODE(?:\s+NGAY)?|NHAN\s+CODE(?:\s+NGAY)?|"
    r"NHẬP\s+CODE|NHAP\s+CODE|PHÁT\s+CODE|PHAT\s+CODE|"
    r"CODE\s+FREE|FREE\s+CODE|GIFT\s*CODE|GIFTCODE|"
    r"TẶNG\s+CODE|TANG\s+CODE",
    re.IGNORECASE,
)
_NOISE_RE = re.compile(
    r"HTTP|WWW|\.COM|FACEBOOK|TELEGRAM|TIKTOK|ZALO|CSKH|BOT|CHECK\s+LINK|LINK",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(
    rf"[A-Za-z0-9{re.escape(getattr(Config, 'SPECIAL_CODE_CHARS_30', ''))}]"
    rf"{{{getattr(Config, 'CODE_MIN_LENGTH', 6)},{getattr(Config, 'CODE_MAX_LENGTH', 15) + 30}}}"
)
_ALNUM_TOKEN_RE = re.compile(r"[a-zA-Z0-9]{6,15}")
_KJC_LABEL_RE = re.compile(
    r"(?:NHẬN\s+CODE|NHAN\s+CODE|CODE)\s*(?:NGAY|NGÀY)?\s*[:\-–—]?\s*"
    r"(MM88|RR88|XX88|GG88)",
    re.IGNORECASE,
)
_KJC_SITE_RE = {
    key: re.compile(rf"\b{re.escape(key)}\b", re.IGNORECASE)
    for key in ("MM88", "RR88", "XX88", "GG88")
}


# ═══════════════════════════════════════════════════════════════
# BROWSER-ONLY SUBMISSION STATE
# ═══════════════════════════════════════════════════════════════
_browser_adapter = None
_durable_inbox = None
_inbox_drain_task = None
_channel_poll_task = None
_inbox_wakeup = None
_receiver = None
_ingress_stage = None
_ingress_stage_task = None
_inbox_message_cache: dict[int, object] = {}
_INBOX_MSG_CACHE_MAX = 4000


def schedule_tracked_task(coro, task_set: set, name: str) -> asyncio.Task:
    """Create a tracked background task without an extra deployment module."""
    task = asyncio.create_task(coro, name=name)
    task_set.add(task)

    def _finish(done_task: asyncio.Task) -> None:
        task_set.discard(done_task)
        if done_task.cancelled():
            return
        try:
            error = done_task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            logger.error("❌ Telegram ingress task lỗi: %s", error)

    task.add_done_callback(_finish)
    return task


def _get_browser_adapter():
    global _browser_adapter
    if _browser_adapter is None:
        if not _BROWSER_ENGINE_OK:
            raise RuntimeError("browser_engine không sẵn sàng")
        _browser_adapter = BrowserEngineAdapter(browser_engine)
    return _browser_adapter


# ═══════════════════════════════════════════════════════════════
# BOT STATE
# ═══════════════════════════════════════════════════════════════
class BotState:
    def __init__(self):
        self.is_running = True
        self._site_code_seen: dict = {}
        self.handler_registered = False
        self._last_cleanup_time = time.time()
        self.bg_tasks: set = set()
        self._channel_account_index: dict = {}
        self._domain_account_cursor: dict[str, int] = {}
        self._rotation_loaded_domains: set[str] = set()
        self._daily_used: set = set()
        self._daily_date: str = datetime.now().strftime("%Y-%m-%d")
        self._inflight_codes: set = set()
        self._inflight_accounts: set = set()
        self._processed_message_hashes: dict = {}
        self._inbox_enqueued_ids: set[int] = set()
        self._work_item_enqueued_ids: set[int] = set()
        self.last_raw_update_at: float | None = None
        self.last_accepted_message_at: float | None = None
        self.raw_update_count: int = 0
        self.accepted_message_count: int = 0
        self.event_loop_lag_ms: float = 0.0


bot_state = BotState()
BOT_START_TIME: datetime = datetime.now(timezone.utc)

_media_download_manager = MediaDownloadManager(
    max_concurrent=getattr(Config, "MAX_CONCURRENT_MEDIA_DOWNLOADS", 4),
    retries=getattr(Config, "MEDIA_DOWNLOAD_RETRIES", 1),
    retry_delay=getattr(Config, "MEDIA_DOWNLOAD_RETRY_DELAY", 0.5),
    max_size_bytes=(int(getattr(Config, "MEDIA_DOWNLOAD_MAX_SIZE_MB", 200)) * 1024 * 1024 if getattr(Config, "MEDIA_DOWNLOAD_MAX_SIZE_MB", 200) else None),
    fast_download=getattr(Config, "FAST_TELEGRAM_DOWNLOAD", False),
    fast_min_bytes=getattr(Config, "FAST_DOWNLOAD_MIN_BYTES", 4 * 1024 * 1024),
    fast_workers=getattr(Config, "FAST_DOWNLOAD_WORKERS", 6),
    fast_chunk_kb=getattr(Config, "FAST_DOWNLOAD_CHUNK_KB", 512),
)

client = None
if Config.API_ID and Config.API_HASH.strip():
    client = TelegramClient(
        Config.SESSION_NAME,
        Config.API_ID,
        Config.API_HASH,
        device_model="Desktop Bot",
        system_version="Windows 10",
        app_version="1.0",
        connection=ConnectionTcpAbridged,
        connection_retries=getattr(Config, "TELEGRAM_CONNECTION_RETRIES", 0),
        retry_delay=1,
        auto_reconnect=getattr(Config, "TELEGRAM_AUTO_RECONNECT", False),
        # Telethon 1.35.0's timeout parameter is the socket/connect timeout;
        # per-operation timeouts are applied explicitly with asyncio.wait_for().
        timeout=getattr(Config, "TELEGRAM_CONNECT_TIMEOUT", 15.0),
        use_ipv6=False,
        flood_sleep_threshold=60,
        receive_updates=True,
        sequential_updates=False,
    )

_submit_success_notify_lock = asyncio.Lock()
_submit_success_notify_keys: set[tuple[str, str, str]] = set()


async def send_submit_success_notification(
    *, domain: str, user: str, code: str, has_points: bool, result_message: str = ""
) -> bool:
    """Notify one Telegram chat after a successful HI88/QQ88 submit.

    This uses the independent Bot API alert channel and runs the HTTP request
    off the event loop. The in-memory key prevents duplicate notifications
    when a success result is observed again during recovery/retry. A failed
    delivery releases the key so a later successful retry can notify again.
    """
    allowed = {"hi88-freecode.pages.dev", "tangquaqq88.com"}
    if domain not in allowed or not getattr(Config, "SUBMIT_SUCCESS_NOTIFY", True):
        return False
    token = str(getattr(Config, "ALERT_BOT_TOKEN", "") or "").strip()
    chat_id = getattr(Config, "SUBMIT_SUCCESS_CHAT_ID", 0)
    if not token or not chat_id:
        logger.warning("⚠️ [SubmitNotify] Thiếu ALERT_BOT_TOKEN hoặc SUBMIT_SUCCESS_CHAT_ID")
        return False

    clean_code = str(code or "").strip().upper()
    key = (domain, str(user or "").strip(), clean_code)
    async with _submit_success_notify_lock:
        if key in _submit_success_notify_keys:
            return True
        if len(_submit_success_notify_keys) > 2000:
            _submit_success_notify_keys.clear()
        _submit_success_notify_keys.add(key)

    site_label = "HI88" if domain == "hi88-freecode.pages.dev" else "QQ88"
    detail = str(result_message or "").replace("\r", " ").replace("\n", " ").strip()
    if len(detail) > 180:
        detail = detail[:177] + "..."
    text = "\n".join(
        (
            f"✅ SUBMIT THÀNH CÔNG {site_label}",
            f"• Account: {user}",
            f"• Code: {clean_code}",
            f"• Điểm: {'Có' if has_points else 'Không/không xác định'}",
            f"• Domain: {domain}",
            *( [f"• Kết quả: {detail}"] if detail else [] ),
            f"• Thời gian: {datetime.now().astimezone().isoformat(timespec='seconds')}",
        )
    )
    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urlencode({
        "chat_id": str(chat_id),
        "text": text,
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    request = Request(
        endpoint,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "giftcode-bot-submit-notify/1.0",
        },
        method="POST",
    )
    timeout = max(1.0, float(getattr(Config, "ALERT_HTTP_TIMEOUT", 10.0)))

    def _post() -> int:
        with urlopen(request, timeout=timeout) as response:
            response.read()
            return int(response.status)

    try:
        status = await asyncio.to_thread(_post)
        if 200 <= status < 300:
            logger.info("📣 [SubmitNotify] Đã gửi %s success tới chat_id=%s", site_label, chat_id)
            return True
    except Exception as exc:
        logger.warning("⚠️ [SubmitNotify] Gửi %s thất bại: %s", site_label, exc)
    async with _submit_success_notify_lock:
        _submit_success_notify_keys.discard(key)
    return False


async def send_auth_key_alert(exc: BaseException) -> bool:
    """Send a fatal Telegram-session alert through an independent Bot API bot.

    This deliberately does not use the Telethon ``client``: when
    AuthKeyDuplicatedError occurs, that client is the component that is no
    longer safe to reconnect.  The HTTP request runs in a worker thread so a
    slow/unreachable Telegram Bot API endpoint does not block the event loop.

    Returns:
        ``True`` when Telegram Bot API accepts the request (HTTP 2xx), else
        ``False``.  All failures are logged without exposing the bot token.
    """
    token = str(getattr(Config, "ALERT_BOT_TOKEN", "") or "").strip()
    chat_id = getattr(Config, "ALERT_CHAT_ID", 0)
    timeout = max(
        1.0,
        float(getattr(Config, "ALERT_HTTP_TIMEOUT", 10.0)),
    )

    if not token:
        logger.critical(
            "❌ [Alert] Thiếu ALERT_BOT_TOKEN — không thể gửi cảnh báo "
            "AuthKeyDuplicated"
        )
        return False

    if not chat_id:
        logger.critical(
            "❌ [Alert] Thiếu ALERT_CHAT_ID — không thể gửi cảnh báo "
            "AuthKeyDuplicated"
        )
        return False

    session_name = os.path.basename(str(getattr(Config, "SESSION_NAME", "")))
    error_text = str(exc).replace("\r", " ").replace("\n", " ").strip()
    if len(error_text) > 500:
        error_text = error_text[:497] + "..."

    text = "\n".join(
        (
            "🚨 BOT DỪNG KHẨN CẤP",
            "Lỗi: Telegram AuthKeyDuplicated",
            "",
            f"• Session: {session_name or '(unknown)' }",
            f"• Host: {socket.gethostname()}",
            f"• PID: {os.getpid()}",
            f"• Thời gian: {datetime.now().astimezone().isoformat()}",
            f"• Chi tiết: {error_text}",
            "",
            "Bot đã dừng retry tự động.",
            "Kiểm tra instance/IP đang dùng chung session, sau đó tạo "
            "session mới nếu cần.",
        )
    )

    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urlencode(
        {
            "chat_id": str(chat_id),
            "text": text,
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")

    request = Request(
        endpoint,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "giftcode-bot-alert/1.0",
        },
        method="POST",
    )

    def _post_alert() -> tuple[int, bytes]:
        with urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()

    try:
        status_code, response_body = await asyncio.to_thread(_post_alert)
        if 200 <= status_code < 300:
            logger.critical(
                "🚨 [Alert] Đã gửi cảnh báo AuthKeyDuplicated qua Bot API "
                "độc lập tới chat_id=%s",
                chat_id,
            )
            return True

        logger.critical(
            "❌ [Alert] Bot API trả HTTP %s: %s",
            status_code,
            response_body[:300].decode("utf-8", errors="replace"),
        )
        return False

    except Exception as alert_error:
        # Không log endpoint vì endpoint chứa bot token.
        logger.critical(
            "❌ [Alert] Gửi cảnh báo AuthKeyDuplicated thất bại: %s",
            alert_error,
        )
        return False


def backup_corrupt_telegram_session() -> dict[str, object]:
    """Backup the Telethon session and optionally remove the broken files.

    The session is never deleted before all existing session files have been
    copied successfully.  Besides the main SQLite file, SQLite sidecars are
    included when present (``-journal``, ``-wal`` and ``-shm``).

    Returns a small status dictionary for logging and alert text.  The
    operation is synchronous by design and should be called from the fatal
    error path before shutdown; it touches only local files.
    """
    enabled = bool(getattr(Config, "SESSION_AUTO_BACKUP", True))
    delete_after_backup = bool(
        getattr(Config, "SESSION_DELETE_AFTER_BACKUP", False)
    )
    session_value = str(getattr(Config, "SESSION_NAME", "") or "").strip()

    result: dict[str, object] = {
        "enabled": enabled,
        "deleted": False,
        "backup_dir": "",
        "files": [],
        "error": "",
    }

    if not enabled:
        logger.warning("⚠️ [Session] SESSION_AUTO_BACKUP=false — bỏ qua backup")
        return result

    if not session_value:
        result["error"] = "SESSION_NAME is empty"
        logger.error("❌ [Session] Không backup được: SESSION_NAME rỗng")
        return result

    session_path = Path(session_value)
    if session_path.suffix.lower() != ".session":
        session_path = session_path.with_name(session_path.name + ".session")

    source_files = [
        session_path,
        Path(str(session_path) + "-journal"),
        Path(str(session_path) + "-wal"),
        Path(str(session_path) + "-shm"),
    ]
    existing_files = [path for path in source_files if path.is_file()]

    if not existing_files:
        result["error"] = f"session files not found: {session_path}"
        logger.error(
            "❌ [Session] Không tìm thấy file session để backup: %s",
            session_path,
        )
        return result

    backup_root = Path(
        getattr(Config, "SESSION_BACKUP_DIR", "backups/sessions")
    )
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    backup_dir = backup_root / f"{session_path.stem}_{stamp}"
    result["backup_dir"] = str(backup_dir)

    copied_files: list[Path] = []
    try:
        backup_dir.mkdir(parents=True, exist_ok=False)

        for source in existing_files:
            destination = backup_dir / source.name
            shutil.copy2(source, destination)
            copied_files.append(source)

        result["files"] = [str(path) for path in copied_files]
        logger.warning(
            "📦 [Session] Đã backup %s file vào %s",
            len(copied_files),
            backup_dir,
        )

        # Xóa là opt-in. Chỉ xóa đúng các file đã copy thành công.
        if delete_after_backup:
            for source in copied_files:
                source.unlink()
            result["deleted"] = True
            logger.warning(
                "🗑️ [Session] Đã xóa %s file session sau khi backup thành công",
                len(copied_files),
            )
        else:
            logger.info(
                "ℹ️ [Session] Giữ nguyên session lỗi; bật "
                "SESSION_DELETE_AFTER_BACKUP=true nếu muốn xóa"
            )

        return result

    except Exception as backup_error:
        result["error"] = str(backup_error)
        logger.exception(
            "❌ [Session] Backup thất bại — không xóa session: %s",
            backup_error,
        )
        return result


_systems = None
message_queue: asyncio.Queue | None = None
message_workers: list = []
_history_queue: asyncio.Queue | None = None
_history_writer_task = None
_domain_semaphores: dict = {}
_submit_semaphore: asyncio.Semaphore | None = None
_active_submit_tasks: set = set()

_domain_queues: dict = {}
_domain_accounts: dict = {}
_domain_workers: dict = {}
_account_rotation_locks: dict[str, asyncio.Lock] = {}
_ocr_media_queue: asyncio.Queue | None = None
_ocr_media_workers: list = []

_queue_full_counter: int = 0
_proc_semaphore: asyncio.Semaphore | None = None
_ocr_semaphore: asyncio.Semaphore | None = None
_domain_rate_limiters: dict = {}

# OCR can be requested more than once for the same Telegram media after a
# retry/re-delivery. Keep the cache small and bounded so it saves inference
# time without retaining media or growing for the lifetime of the process.
_ocr_result_cache: dict[str, tuple[float, dict]] = {}


def _ocr_file_cache_key(
    path: str,
    target_url: str,
    crop_config,
    is_video: bool,
    single_code: bool,
    telegram_media_id=None,
) -> str:
    # Telegram media IDs are stable across redelivery and avoid hashing the
    # entire file before OCR. Include the target/config so the same media can
    # still be interpreted independently by different site profiles.
    if telegram_media_id is not None:
        media_key = f"telegram-media:{telegram_media_id}"
    else:
        digest = hashlib.sha256()
        with open(path, "rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        media_key = digest.hexdigest()
    params = json.dumps(
        {
            "target_url": target_url,
            "crop": crop_config,
            "video": bool(is_video),
            "single": bool(single_code),
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return f"{media_key}:{hashlib.sha256(params).hexdigest()}"


def _telegram_media_id(event):
    """Return the stable Telegram document/photo ID when available."""
    try:
        media = getattr(getattr(event, "message", None), "media", None)
        document = getattr(media, "document", None)
        if document is not None and getattr(document, "id", None) is not None:
            return f"document:{document.id}"
        photo = getattr(media, "photo", None)
        if photo is not None and getattr(photo, "id", None) is not None:
            return f"photo:{photo.id}"
    except Exception:
        pass
    return None


def _ocr_cache_get(key: str):
    now = time.monotonic()
    item = _ocr_result_cache.get(key)
    if not item:
        return None
    created, result = item
    ttl = max(1.0, float(getattr(Config, "OCR_CACHE_TTL_SECONDS", 600.0)))
    if now - created > ttl:
        _ocr_result_cache.pop(key, None)
        return None
    return {
        **result,
        "codes": [dict(code) for code in result.get("codes", [])],
        "message": "ocr_cache_hit",
    }


def _ocr_cache_put(key: str, result: dict):
    max_entries = max(1, int(getattr(Config, "OCR_CACHE_MAX_ENTRIES", 128)))
    _ocr_result_cache[key] = (time.monotonic(), {
        **result,
        "codes": [dict(code) for code in result.get("codes", [])],
    })
    if len(_ocr_result_cache) > max_entries:
        oldest = sorted(_ocr_result_cache.items(), key=lambda item: item[1][0])
        for old_key, _ in oldest[: max(1, len(_ocr_result_cache) - max_entries)]:
            _ocr_result_cache.pop(old_key, None)

# ✅ FIX LỖI DELAY NHẬN TIN: dedup ingress


# ═══════════════════════════════════════════════════════════════
# KJC SPECIAL SPOILER MODE
# ═══════════════════════════════════════════════════════════════
# Nguồn cấu hình duy nhất: Config (config.py). Không hardcode lại ở đây.
_KJC_SPECIAL_CHANNEL_IDS = Config.KJC_SPECIAL_CHANNEL_IDS
_KJC_BROADCAST_DOMAINS = Config.KJC_BROADCAST_DOMAINS
_KJC_BROADCAST_URLS = Config.KJC_BROADCAST_URLS


def is_kjc_special_channel(chat_id) -> bool:
    try:
        return int(chat_id) in _KJC_SPECIAL_CHANNEL_IDS
    except Exception:
        return False


def _iter_spoiler_texts(text: str, entities):
    """Yield Telegram spoiler spans, encoding the UTF-16 text only once."""
    if not text or not entities:
        return
    try:
        raw = text.encode("utf-16-le")
    except Exception:
        return
    for entity in entities:
        if not isinstance(entity, MessageEntitySpoiler):
            continue
        try:
            start = max(0, int(entity.offset)) * 2
            end = start + max(0, int(entity.length)) * 2
            spoiler_text = raw[start:end].decode("utf-16-le", errors="ignore").strip()
        except (AttributeError, TypeError, ValueError):
            continue
        if spoiler_text:
            yield spoiler_text


def extract_kjc_spoiler_codes(event) -> list[str]:
    if not is_kjc_special_channel(getattr(event, "chat_id", None)):
        return []

    message = getattr(event, "message", None)
    if message is None:
        return []

    full_text = (
        getattr(message, "message", None)
        or getattr(message, "text", None)
        or ""
    )

    if not full_text:
        return []

    entities = getattr(message, "entities", None) or []
    codes = []
    seen_codes = set()
    seen_candidates = set()
    for spoiler_text in _iter_spoiler_texts(full_text, entities):
        for line in spoiler_text.splitlines():
            line = line.strip()
            if not line:
                continue

            candidates = extract_tokens_from_line(line)
            if not candidates:
                candidates = [line]

            for candidate in candidates:
                cleaned = CodeValidator.clean_code(candidate)
                normalized_candidate = cleaned.upper()
                if not cleaned or normalized_candidate in seen_candidates:
                    continue
                seen_candidates.add(normalized_candidate)

                try:
                    result = validate_candidate(
                        cleaned,
                        "https://xx88code.com",
                        source="kjc_spoiler",
                    )
                except Exception as exc:
                    logger.warning(
                        "⚠️ [KJC] Không validate được '%s': %s",
                        cleaned,
                        exc,
                    )
                    continue

                if not result.get("valid"):
                    logger.warning(
                        "🚫 [KJC] Bỏ code không hợp lệ: %r",
                        cleaned,
                    )
                    continue

                code = result.get("clean_code") or cleaned
                if code.upper() not in seen_codes:
                    seen_codes.add(code.upper())
                    codes.append(code)
                    logger.info("🎯 [KJC-SPOILER] phát hiện: %s", code)

    # KJC có thể đăng đồng thời spoiler và code text. Luôn chạy text fallback
    # rồi gộp, không trả sớm sau khi thấy spoiler.
    try:
        fallback = extract_codes_from_message(
            event,
            full_text,
            "https://xx88code.com",
            channel_name="KJC",
            include_text_after_spoiler=True,
            spoiler_codes=[],
        )
        return unique_keep_order(codes + (fallback or []))
    except Exception as exc:
        logger.debug("⚠️ [KJC] text fallback lỗi: %s", exc)
        return unique_keep_order(codes)


def detect_kjc_labeled_domains(event, raw_text: str = "") -> tuple[str, ...]:
    """Return a single site when KJC carries an unambiguous site label."""
    message = getattr(event, "message", None)
    text = " ".join(
        part for part in (
            raw_text,
            getattr(message, "message", None) if message else "",
            getattr(message, "text", None) if message else "",
        ) if part
    ).upper()

    aliases = {
        "MM88": "livemm88.net",
        "RR88": "liverr88.net",
        "XX88": "xx88code.com",
        "GG88": "gg88live.tv",
    }
    # Ưu tiên mẫu nhãn ngay sau cụm nhận code; tránh nhầm hashtag/banner
    # liệt kê cả bốn thương hiệu ở cuối caption.
    label_match = _KJC_LABEL_RE.search(text)
    if label_match:
        return (aliases[label_match.group(1).upper()],)

    found = tuple(dict.fromkeys(aliases[k] for k in aliases if _KJC_SITE_RE[k].search(text)))
    return found if len(found) == 1 else ()


def build_kjc_broadcast_items(
    codes: list[str], channel_name: str, target_domains: tuple[str, ...] = ()
) -> list[dict]:
    items = []
    domains = target_domains or _KJC_BROADCAST_DOMAINS
    active_domains = set(getattr(Config, "ACTIVE_DOMAINS", ()) or ())
    if active_domains:
        domains = tuple(domain for domain in domains if domain in active_domains)
    for code in unique_keep_order(codes):
        for domain in domains:
            items.append(
                {
                    "code": code,
                    "channel_name": channel_name,
                    "target_url": _KJC_BROADCAST_URLS[domain],
                    "domain": domain,
                    "source": "kjc_spoiler",
                }
            )
    return items


# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════


def normalize_domain(url: str) -> str:
    p = urlparse(url or "")
    return (p.netloc or p.path).lower().replace("www.", "").strip("/")


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def get_site_log_tag(target_url: str) -> str:
    d = normalize_domain(target_url)
    return _SITE_LOG_LABELS.get(d, d or "SYSTEM")


def _refresh_daily_state():
    today = _today_str()
    if bot_state._daily_date != today:
        bot_state._daily_used.clear()
        bot_state._channel_account_index.clear()
        bot_state._domain_account_cursor.clear()
        bot_state._rotation_loaded_domains.clear()
        if browser_engine is not None:
            try:
                browser_engine.reset_exhausted_accounts()
            except Exception:
                pass
        bot_state._daily_date = today
        logger.info(f"🗓️ Ngày mới ({today})")


async def _mark_account_done_today(channel_key: str, username: str):
    _refresh_daily_state()
    today = _today_str()
    bot_state._daily_used.add((today, channel_key, username))
    _notify_browser_exhausted(channel_key, username)
    db = _systems.get("db") if _systems else None
    if db is not None and hasattr(db, "mark_account_exhausted"):
        await asyncio.get_running_loop().run_in_executor(
            _DB_EXECUTOR,
            db.mark_account_exhausted,
            channel_key,
            username,
            today,
        )


def _is_account_done_today(channel_key: str, username: str) -> bool:
    _refresh_daily_state()
    return (_today_str(), channel_key, username) in bot_state._daily_used


def _result_indicates_account_limit(message: str) -> bool:
    from submission_outcomes import is_account_limit_text
    return is_account_limit_text(message)


async def _get_next_available_account(channel_key: str, accounts: list):
    # Keep reservations and cursors on the event loop; serialize selections
    # per domain while SQLite reads/writes run in the DB executor.
    lock = _account_rotation_locks.setdefault(channel_key, asyncio.Lock())
    async with lock:
        _refresh_daily_state()
        ordered = sorted(accounts, key=lambda a: a.get("priority", 999))
        if not ordered:
            return None

        today = _today_str()
        persisted_cursor = 0
        persisted_exhausted = set()
        db = _systems.get("db") if _systems else None
        loop = asyncio.get_running_loop()
        if (
            db is not None
            and hasattr(db, "get_account_rotation")
            and channel_key not in bot_state._rotation_loaded_domains
        ):
            persisted_cursor, persisted_exhausted = await loop.run_in_executor(
                _DB_EXECUTOR, db.get_account_rotation, channel_key, today
            )
            bot_state._rotation_loaded_domains.add(channel_key)
            for username in persisted_exhausted:
                bot_state._daily_used.add((today, channel_key, username))
                _notify_browser_exhausted(channel_key, username)

        # Ưu tiên tài khoản đang được điền sẵn trong ô username của tab rảnh:
        # dùng đúng tài khoản đó thì chỉ cần nhập code (không đổi tên, không
        # đổi tab). Không đẩy con trỏ round-robin để không lệch vòng xoay.
        prefilled = _prefilled_accounts_for(channel_key)
        if prefilled:
            for pname in prefilled:
                for acc in ordered:
                    username = acc["username"]
                    if username.lower() != pname:
                        continue
                    if _is_account_done_today(channel_key, username):
                        continue
                    reservation_key = (channel_key, username)
                    if reservation_key in bot_state._inflight_accounts:
                        continue
                    bot_state._inflight_accounts.add(reservation_key)
                    return acc

        # Round-robin theo domain: mỗi lần reserve thành công sẽ đẩy con trỏ
        # sang account kế tiếp. Account bận hoặc đã hết lượt trong ngày được
        # bỏ qua; vòng lặp vẫn tiếp tục để tìm fallback khả dụng.
        start = bot_state._domain_account_cursor.get(
            channel_key, persisted_cursor
        ) % len(ordered)
        for offset in range(len(ordered)):
            acc = ordered[(start + offset) % len(ordered)]
            username = acc["username"]
            if _is_account_done_today(channel_key, username):
                continue
            reservation_key = (channel_key, username)
            if reservation_key in bot_state._inflight_accounts:
                continue
            bot_state._inflight_accounts.add(reservation_key)
            cursor = (start + offset + 1) % len(ordered)
            bot_state._domain_account_cursor[channel_key] = cursor
            if db is not None and hasattr(db, "save_account_cursor"):
                try:
                    await loop.run_in_executor(
                        _DB_EXECUTOR, db.save_account_cursor, channel_key, cursor, today
                    )
                except BaseException:
                    # Không để reservation treo nếu shutdown/cancellation xảy ra
                    # trước khi caller nhận được account.
                    bot_state._inflight_accounts.discard(reservation_key)
                    raise
            return acc
        return None


def _release_account_reservation(channel_key: str, username):
    if username:
        bot_state._inflight_accounts.discard((channel_key, username))


def _domain_has_unused_account(domain: str) -> bool:
    """Return whether at least one account can still be used today.

    An account may be temporarily unavailable because another worker is using
    it; that case should be retried briefly. If every account reached its
    daily limit, completing the item is preferable to blocking newer codes.
    """
    db = _systems.get("db") if _systems else None
    if db is not None and hasattr(db, "get_account_rotation") and domain not in bot_state._rotation_loaded_domains:
        _cursor, persisted_exhausted = db.get_account_rotation(domain, _today_str())
        bot_state._domain_account_cursor.setdefault(domain, _cursor)
        bot_state._rotation_loaded_domains.add(domain)
        for username in persisted_exhausted:
            bot_state._daily_used.add((_today_str(), domain, username))
    return any(
        not _is_account_done_today(domain, str(account.get("username") or ""))
        for account in _domain_accounts.get(domain, [])
        if account.get("username")
    )


def _spoiler_entity_signature(message) -> str:
    """Stable offsets/lengths for spoiler entities, without storing hidden text."""
    signature = []
    for entity in getattr(message, "entities", None) or []:
        if not isinstance(entity, MessageEntitySpoiler):
            continue
        try:
            signature.append((int(entity.offset), int(entity.length)))
        except (AttributeError, TypeError, ValueError):
            continue
    return json.dumps(signature, separators=(",", ":")) if signature else ""


def _message_content_hash(text: str, spoiler_signature: str = "") -> str:
    payload = text or ""
    if spoiler_signature:
        payload = f"{payload}\x1fspoiler:{spoiler_signature}"
    return hashlib.md5(payload.encode("utf-8", errors="ignore")).hexdigest()


def _on_message_queue_drop(item) -> None:
    """Allow a dropped queue item to be re-enqueued from durable inbox."""
    if isinstance(item, tuple) and len(item) == 2:
        try:
            row_id = int(item[1])
            bot_state._inbox_enqueued_ids.discard(row_id)
            _inbox_message_cache.pop(row_id, None)
        except (TypeError, ValueError):
            pass


async def _retry_dropped_domain_item(
    inbox_id: int, claim_token: str | None, work_item_id: int | None = None
) -> None:
    """Retry a dropped domain item without blocking QueueManager's event loop."""
    if _durable_inbox is None:
        return
    try:
        if work_item_id:
            await asyncio.get_running_loop().run_in_executor(
                _INBOX_STATE_EXECUTOR, _durable_inbox.retry_work_item,
                int(work_item_id), "domain queue overflow/drop", 5,
            )
        else:
            await asyncio.get_running_loop().run_in_executor(
                _INBOX_STATE_EXECUTOR, _durable_inbox.retry_or_fail,
                inbox_id, "domain queue overflow/drop", 5, claim_token,
            )
    except Exception as exc:
        logger.error("❌ Không thể retry inbox row=%s sau domain queue drop: %s", inbox_id, exc)


def _on_domain_queue_drop(item) -> None:
    """Schedule bounded inbox retry without blocking QueueManager's event loop."""
    inbox_id = item.get("inbox_id") if isinstance(item, dict) else None
    claim_token = item.get("claim_token") if isinstance(item, dict) else None
    work_item_id = item.get("work_item_id") if isinstance(item, dict) else None
    if inbox_id and _durable_inbox is not None:
        try:
            task = asyncio.create_task(
                _retry_dropped_domain_item(int(inbox_id), claim_token, work_item_id),
                name=f"retry-dropped-domain-{inbox_id}",
            )
            bot_state.bg_tasks.add(task)
            task.add_done_callback(bot_state.bg_tasks.discard)
        except (TypeError, ValueError, RuntimeError) as exc:
            logger.error("❌ Không thể schedule retry inbox row=%s sau domain queue drop: %s", inbox_id, exc)




# Chỉ DUY NHẤT kênh PHÁT CODE XX88 được OCR ảnh/video. Khóa cứng, không phụ
# thuộc .env (OCR_*_CHANNEL_IDS, OCR_FALLBACK_ALL_MEDIA) để không kênh nào khác
# vô tình tải media / chạy OCR. Mọi kênh còn lại chỉ đọc caption/text, ưu tiên
# mã spoiler.
_OCR_ONLY_CHANNEL_ID = -1002817093108


def _is_ocr_allowed_channel(chat_id) -> bool:
    try:
        return int(chat_id) == _OCR_ONLY_CHANNEL_ID
    except Exception:
        return False


def _has_hi88_code_link(event) -> bool:
    """HI88 chỉ OCR media khi bài có link nhập code, kể cả hidden URL."""
    message = getattr(event, "message", None)
    texts = [
        getattr(message, "text", "") or "",
        getattr(message, "message", "") or "",
    ]
    haystack = " ".join(texts).lower()
    if "hi88-freecode.pages.dev" in haystack:
        return True
    for entity in getattr(message, "entities", None) or []:
        url = getattr(entity, "url", "") or ""
        if "hi88-freecode.pages.dev" in url.lower():
            return True
    return False


def _prune_site_code_seen():
    ttl = float(getattr(Config, "SITE_CODE_DEDUP_TTL", 10.0))
    now = time.time()
    for k in [k for k, ts in bot_state._site_code_seen.items() if now - ts > ttl]:
        del bot_state._site_code_seen[k]


def _prune_processed_message_hashes():
    ttl, now = 300.0, time.time()
    for k in [k for k, (_, ts) in bot_state._processed_message_hashes.items() if now - ts > ttl]:
        del bot_state._processed_message_hashes[k]


def is_site_code_duplicate(domain: str, user: str, code: str) -> bool:
    ttl = float(getattr(Config, "SITE_CODE_DEDUP_TTL", 10.0))
    now = time.time()
    k = (domain, user, code.upper())
    if bot_state._site_code_seen.get(k) is not None and now - bot_state._site_code_seen[k] < ttl:
        return True
    bot_state._site_code_seen[k] = now
    return False


def _clear_site_code_duplicate(domain: str, user: str, code: str) -> None:
    bot_state._site_code_seen.pop((domain, user, code.upper()), None)


def _get_proc_semaphore() -> asyncio.Semaphore:
    global _proc_semaphore
    if _proc_semaphore is None:
        _proc_semaphore = asyncio.Semaphore(int(getattr(Config, "MAX_CONCURRENT_PROCESSING", 24)))
    return _proc_semaphore


def _get_submit_semaphore() -> asyncio.Semaphore:
    global _submit_semaphore
    if _submit_semaphore is None:
        _submit_semaphore = asyncio.Semaphore(
            int(getattr(Config, "MAX_CONCURRENT_SUBMITS", 8))
        )
    return _submit_semaphore


def _get_ocr_semaphore() -> asyncio.Semaphore:
    global _ocr_semaphore
    if _ocr_semaphore is None:
        _ocr_semaphore = asyncio.Semaphore(max(1, int(getattr(Config, "MAX_CONCURRENT_OCR", 2))))
    return _ocr_semaphore


def _domain_slot_limit(domain: str) -> int:
    """Never schedule more workers than the domain has warm tab slots."""
    limit = max(1, int(getattr(Config, "MAX_CONCURRENT_SUBMITS_PER_DOMAIN", 3)))
    profile = get_site_profile(domain)
    if profile is not None:
        limit = min(limit, max(1, int(profile.tab_slots)))
    return limit


class _TokenBucket:
    def __init__(self, rpm: float, burst: float):
        self.rate_per_sec = max(float(rpm), 1.0) / 60.0
        self.capacity = max(float(burst), 1.0)
        self.tokens = self.capacity
        self.last_refill = time.monotonic()
        self.lock = asyncio.Lock()

    async def acquire(self):
        while True:
            async with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.last_refill) * self.rate_per_sec)
                self.last_refill = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                wait = (1.0 - self.tokens) / self.rate_per_sec
            await asyncio.sleep(wait)


def get_domain_rate_limiter(domain: str) -> _TokenBucket:
    if domain not in _domain_rate_limiters:
        limits = getattr(Config, "DOMAIN_RATE_LIMITS", {}).get(domain)
        if limits is None:
            limits = (
                getattr(Config, "REQUESTS_PER_MINUTE", 30),
                getattr(Config, "MAX_BURST", 5),
            )
        rpm, burst = limits
        _domain_rate_limiters[domain] = _TokenBucket(float(rpm), float(burst))
    return _domain_rate_limiters[domain]


def get_domain_semaphore(domain: str) -> asyncio.Semaphore:
    if domain not in _domain_semaphores:
        limit = _domain_slot_limit(domain)
        _domain_semaphores[domain] = asyncio.Semaphore(limit)
    return _domain_semaphores[domain]


# ═══════════════════════════════════════════════════════════════
# CODE HISTORY
# ═══════════════════════════════════════════════════════════════
CODE_HISTORY_DIR = Path("logs/code_history")
CODE_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
_HISTORY_FIELDS = [
    "time", "event_type", "channel", "site", "account", "code", "source",
    "status", "telegram_delay", "submit_elapsed", "message", "screenshot",
]


def _write_history_rows(rows: list[dict]):
    if not rows:
        return
    try:
        csv_p = CODE_HISTORY_DIR / f"code_history_{_today_str()}.csv"
        jsonl_p = CODE_HISTORY_DIR / f"code_history_{_today_str()}.jsonl"
        header = not csv_p.exists()
        with csv_p.open("a", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=_HISTORY_FIELDS)
            if header:
                w.writeheader()
            w.writerows(rows)
        with jsonl_p.open("a", encoding="utf-8") as f:
            f.writelines(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    except Exception as e:
        logger.debug(f"⚠️ write_history: {e}")


def _write_history_row(row: dict):
    _write_history_rows([row])


async def _history_writer_loop():
    while True:
        rows = []
        try:
            row = await _history_queue.get()
            if row is None:
                _history_queue.task_done()
                break
            rows.append(row)
            # Drain the already queued burst without waiting. This preserves
            # low latency for the first row while reducing file-open overhead
            # when one Telegram message fans out to many accounts/codes.
            for _ in range(31):
                try:
                    extra = _history_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if extra is None:
                    _history_queue.put_nowait(None)
                    break
                rows.append(extra)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(_DB_EXECUTOR, _write_history_rows, rows)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.debug(f"⚠️ history_writer: {e}")
        finally:
            for _ in rows:
                try:
                    _history_queue.task_done()
                except Exception:
                    break


def start_history_writer():
    global _history_queue, _history_writer_task
    _history_queue = asyncio.Queue(maxsize=2000)
    _history_writer_task = asyncio.create_task(_history_writer_loop())


def append_code_history(event_type, code="", target_url="", account="", channel="", source="", status="", telegram_delay=None, submit_elapsed=None, message="", screenshot=""):
    try:
        row = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "event_type": event_type,
            "channel": channel or "",
            "site": normalize_domain(target_url),
            "account": account or "",
            "code": str(code or ""),
            "source": source or "",
            "status": status or "",
            "telegram_delay": "" if telegram_delay is None else f"{float(telegram_delay):.2f}",
            "submit_elapsed": "" if submit_elapsed is None else f"{float(submit_elapsed):.2f}",
            "message": str(message or "").replace("\n", " ")[:300],
            "screenshot": str(screenshot or ""),
        }
        if _history_queue is not None:
            try:
                _history_queue.put_nowait(row)
            except asyncio.QueueFull:
                logger.warning("⚠️ [History] queue đầy — bỏ 1 dòng lịch sử (%s)", event_type)
        else:
            _write_history_row(row)
        return row
    except Exception as e:
        logger.debug(f"⚠️ append_history: {e}")
        return None


def build_daily_summary():
    try:
        csv_p = CODE_HISTORY_DIR / f"code_history_{_today_str()}.csv"
        if not csv_p.exists():
            return None
        summary = {}
        with csv_p.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if row.get("event_type") != "RESULT":
                    continue
                k = (row.get("site", ""), row.get("account", ""))
                summary.setdefault(k, {"SUCCESS": 0, "FAILED": 0, "UNKNOWN": 0})
                s = row.get("status") or "UNKNOWN"
                summary[k][s] = summary[k].get(s, 0) + 1
        out = CODE_HISTORY_DIR / f"daily_summary_{_today_str()}.csv"
        with out.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["date", "site", "account", "success", "failed", "unknown", "total"])
            w.writeheader()
            for (site, acc), c in sorted(summary.items()):
                s, fa, u = c.get("SUCCESS", 0), c.get("FAILED", 0), c.get("UNKNOWN", 0)
                w.writerow({"date": _today_str(), "site": site, "account": acc, "success": s, "failed": fa, "unknown": u, "total": s + fa + u})
        return str(out)
    except Exception as e:
        logger.warning(f"⚠️ daily_summary: {e}")
        return None


def measure_telegram_delay_fast(msg_ts):
    try:
        if msg_ts.tzinfo is None:
            msg_ts = msg_ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - msg_ts).total_seconds()
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════
# DOMAIN WORKER
# ═══════════════════════════════════════════════════════════════
def build_domain_accounts_map() -> dict:
    domains = {
        normalize_domain(cfg.get("url", ""))
        for cfg in Config.CHANNEL_CONFIG.values()
        if cfg.get("url")
    }
    domains.update(
        normalize_domain(domain)
        for domain in getattr(Config, "DOMAIN_ACCOUNT_OVERRIDES", {})
    )
    result = {}
    for domain in domains:
        if not domain or not is_domain_enabled(domain):
            continue
        accounts = get_effective_domain_accounts(domain)
        if accounts:
            result[domain] = accounts
    return result


def get_domain_queue(domain: str) -> asyncio.Queue:
    if domain not in _domain_queues:
        q = asyncio.Queue(maxsize=int(getattr(Config, "DOMAIN_QUEUE_MAXSIZE", 500)))
        _domain_queues[domain] = q
        qm = get_queue_manager()
        if qm is not None:
            qm.register(f"domain:{domain}", q, on_drop=_on_domain_queue_drop)
    return _domain_queues[domain]




def _fanout_code_item(item: dict, count: int) -> list[dict]:
    """Tạo dispatch item; chỉ đánh dấu fanout khi thật sự nhân một code."""
    count = max(1, int(count))
    return [
        {**item, "fanout": count > 1, "fanout_index": index}
        for index in range(count)
    ]


def _build_spoiler_dispatch_items(items: list[dict]) -> list[dict]:
    """Send every code to the configured number of accounts for its site."""
    account_cap = max(1, int(getattr(Config, "ACCOUNTS_PER_CODE", 3)))
    result: list[dict] = []
    for item in items:
        domain = str(item.get("domain") or "")
        account_count = len(get_effective_domain_accounts(domain))
        count = min(account_cap, _domain_slot_limit(domain), account_count)
        if count > 0:
            result.extend(_fanout_code_item(item, count))
    return result


async def _submit_code_with_account_retries(
    account,
    code,
    target_url,
    domain,
) -> str:
    user = account["username"]

    max_r = max(
        1,
        int(
            getattr(
                Config,
                "MAX_RETRIES_PER_ACCOUNT",
                1,
            )
        ),
    )

    retry = bool(
        getattr(
            Config,
            "RETRY_ON_TIMEOUT",
            True,
        )
    )

    for attempt in range(1, max_r + 1):
        result = (
            await submit_code_with_delay(
                user,
                code,
                target_url,
                _systems,
            )
            or {}
        )

        success = bool(result.get("success", False))
        infra_failure = bool(result.get("_infra_failure", False))
        has_pts = bool(result.get("has_points", False))
        wrong = bool(result.get("is_wrong_code", False))
        blocked = bool(result.get("is_account_blocked", False))

        msg = str(
            result.get("message", "")
            or ""
        )[:120]

        result_code = str(
            result.get("code", "")
            or ""
        ).upper().strip()

        msg_upper = msg.upper()
        failure_kind = str(result.get("failure_kind", "") or "").upper()

        if failure_kind == "PENDING_VERIFICATION" or result.get("status") == "PENDING_VERIFICATION":
            logger.warning(
                "⏸️ [%s] %s — PENDING_VERIFICATION, giữ nguyên page và kết thúc item để nhả worker",
                domain, msg or "Turnstile verification pending",
            )
            append_code_history(
                event_type="SUBMIT_ATTEMPT", code=code, target_url=target_url,
                account=user, status="PENDING_VERIFICATION", message=msg,
            )
            return "PENDING_VERIFICATION"

        if failure_kind == "CAPTCHA_EXPIRED" or result_code == "CAPTCHA_EXPIRED":
            # Token captcha quá hạn: mã CHƯA bị site từ chối → không đánh dấu
            # used. Thử lại một lần trên trang đã được làm mới; nếu vẫn hết
            # hạn thì coi như chờ xác minh lại (không mất mã).
            append_code_history(
                event_type="SUBMIT_ATTEMPT", code=code, target_url=target_url,
                account=user, status="CAPTCHA_EXPIRED", message=msg,
            )
            if retry and attempt < max_r:
                logger.warning(
                    "⌛ [%s] %s — captcha hết hạn, thử lại %s/%s", domain, code, attempt, max_r,
                )
                await asyncio.sleep(0.5)
                continue
            logger.warning("⌛ [%s] %s — captcha vẫn hết hạn sau retry → chờ xác minh lại", domain, code)
            return "PENDING_VERIFICATION"

        if failure_kind == "SITE_UI_CHANGED":
            logger.error("🧩 [%s] %s — SITE_UI_CHANGED, không retry mù", domain, msg)
            append_code_history(
                event_type="SUBMIT_ATTEMPT", code=code, target_url=target_url,
                account=user, status="SITE_UI_CHANGED", message=msg,
            )
            return "SITE_UI_CHANGED"

        # A busy/unavailable browser tab is not a site result. Keep it
        # retryable and never classify it as a false NO_RESULT.
        if infra_failure and not result.get("keep_page") and "CLOUDFLARE" not in msg_upper and "TURNSTILE" not in msg_upper:
            if retry and attempt < max_r:
                logger.warning(
                    "🔁 [%s] %s — browser infrastructure retry %s/%s — %s",
                    domain, code, attempt, max_r, msg or "tab unavailable",
                )
                await asyncio.sleep(min(0.25 * attempt, 1.0))
                continue
            append_code_history(
                event_type="SUBMIT_ATTEMPT",
                code=code,
                target_url=target_url,
                account=user,
                status="INFRA_FAILURE",
                message=msg or "browser infrastructure unavailable",
            )
            return "INFRA_FAILURE"

        # Không retry/reload cùng widget khi Cloudflare/Turnstile đang chờ
        # người dùng. Retry ở đây sẽ mở lại form và làm mới captcha hiện tại.
        if result.get("keep_page") or "CLOUDFLARE" in msg_upper or "TURNSTILE" in msg_upper:
            logger.warning(
                "⏸️ [%s] %s — giữ nguyên page, không retry captcha",
                domain,
                msg or "Cloudflare verification pending",
            )
            return "FAILED"

        # Một số site trả đồng thời thông báo thành công và "đã đạt giới hạn".
        # Không đánh dấu code đã dùng trong trường hợp này; đánh dấu tài khoản
        # hết lượt để worker requeue code cho tài khoản kế tiếp.
        if blocked or _result_indicates_account_limit(msg_upper):
            await _mark_account_done_today(domain, user)
            append_code_history(
                event_type="SUBMIT_ATTEMPT",
                code=code,
                target_url=target_url,
                account=user,
                status="ACCOUNT_BLOCKED",
                message=msg or result_code or "Account limit reached",
            )
            logger.warning("⏭️ [%s] %s đạt giới hạn — chuyển tài khoản kế tiếp", domain, user)
            return "ACCOUNT_BLOCKED"

        terminal_failure = (
            result_code in {
                "CAPTCHA_INVALID",
                "CODE_NOT_USED",
                "CODE_USED",
                "INVALID_CODE",
                "CAPTCHA_FAILED",
            }
            or "CAPTCHA_INVALID" in msg_upper
            or "CAPTCHA XÁC THỰC KHÔNG HỢP LỆ" in msg_upper
            or "CAPTCHA KHÔNG HỢP LỆ" in msg_upper
            or "CODE ĐÃ ĐƯỢC SỬ DỤNG" in msg_upper
            or "MÃ ĐÃ ĐƯỢC SỬ DỤNG" in msg_upper
            or "CODE NOT USED" in msg_upper
        )

        # CAPTCHA lỗi hoặc code đã được sử dụng:
        # không retry cùng code để tránh mất thêm thời gian.
        if terminal_failure:
            logger.warning(
                "⏭️ [%s] %s — %s — không retry",
                domain,
                code,
                msg or result_code or "terminal failure",
            )

            return "FAILED"

        if success and has_pts:
            await _mark_code_used_if_final(
                domain,
                code,
                "SUCCESS_POINTS",
            )

            append_code_history(
                event_type="FINAL_RESULT",
                code=code,
                target_url=target_url,
                account=user,
                status="SUCCESS_POINTS",
                message="OK có điểm",
            )

            return "SUCCESS_POINTS"

        if success and not has_pts:
            await _mark_code_used_if_final(
                domain,
                code,
                "SUCCESS_NO_POINTS",
            )

            append_code_history(
                event_type="FINAL_RESULT",
                code=code,
                target_url=target_url,
                account=user,
                status="SUCCESS_NO_POINTS",
                message="OK không điểm",
            )

            return "SUCCESS_NO_POINTS"

        if wrong:
            await _mark_code_used_if_final(
                domain,
                code,
                "FAILED",
            )

            append_code_history(
                event_type="SUBMIT_ATTEMPT",
                code=code,
                target_url=target_url,
                account=user,
                status="FAILED",
                message=msg,
            )

            return "FAILED"

        # Chỉ retry lỗi tạm thời như timeout hoặc lỗi mạng.
        if retry and attempt < max_r:
            logger.warning(
                "🔁 [%s] %s — retry %s/%s — %s",
                domain,
                code,
                attempt,
                max_r,
                msg or "temporary error",
            )

            await asyncio.sleep(
                min(
                    0.3 * attempt,
                    1.0,
                )
            )

            continue

        append_code_history(
            event_type="FINAL_RESULT",
            code=code,
            target_url=target_url,
            account=user,
            status="NO_RESULT",
            message=(
                f"NO_RESULT sau {attempt} lần"
                f"{': ' + msg if msg else ''}"
            ),
        )

        return "NO_RESULT"

    return "NO_RESULT"


async def domain_code_worker(domain: str, target_url: str, worker_id: int = 1):
    queue = get_domain_queue(domain)
    accounts = _domain_accounts.get(domain, [])
    site_tag = get_site_log_tag(target_url)
    log_tok = set_log_context(site_tag)
    try:
        logger.info(f"👷 [Domain-Worker#{worker_id}] '{domain}' ready — {len(accounts)} accounts")
        while bot_state.is_running:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            code = item["code"]
            inbox_id = item.get("inbox_id")
            claim_token = item.get("claim_token")
            work_item_id = item.get("work_item_id")
            durable_item = None
            item_target_url = item.get("target_url") or target_url
            item_domain = normalize_domain(item_target_url)


            item_tok = set_log_context(site_tag)
            account = None
            current_task = asyncio.current_task()
            try:
                if current_task is not None:
                    _active_submit_tasks.add(current_task)
                # Mã quá cũ không được chiếm tab (QQ88/HI88 chỉ có 1 tab):
                # bỏ qua để mã mới phía sau được xử lý ngay.
                if work_item_id:
                    bot_state._work_item_enqueued_ids.discard(int(work_item_id))
                if not is_domain_enabled(item_domain):
                    logger.info("⏸️ [%s] Site đã tắt — bỏ item RAM, giữ dữ liệu bền", item_domain)
                    continue
                if work_item_id and _durable_inbox is not None:
                    durable_item = await asyncio.get_running_loop().run_in_executor(
                        _INBOX_STATE_EXECUTOR,
                        _durable_inbox.claim_work_item,
                        int(work_item_id),
                    )
                    if not durable_item:
                        continue
                    code = durable_item["code"]
                    item["code"] = code
                    item["target_url"] = durable_item["target_url"]
                    item["domain"] = durable_item["domain"]
                    item_target_url = item["target_url"] or target_url
                    item_domain = normalize_domain(item_target_url)

                _max_age = float(getattr(Config, "CODE_MAX_AGE_SECONDS", 120.0))
                _stale, _age = first_known_age(
                    (item.get("msg_ts"), (durable_item or {}).get("created_at")),
                    _max_age,
                )
                if _stale:
                    if work_item_id and _durable_inbox is not None:
                        await asyncio.get_running_loop().run_in_executor(
                            _INBOX_STATE_EXECUTOR,
                            _durable_inbox.complete_work_item,
                            int(work_item_id),
                        )
                    elif inbox_id and _durable_inbox is not None:
                        await asyncio.get_running_loop().run_in_executor(
                            _INBOX_STATE_EXECUTOR,
                            _durable_inbox.complete_item,
                            int(inbox_id),
                            claim_token,
                        )
                    logger.warning(
                        "⏭️ [%s#%s] bỏ mã cũ %.0fs (> %.0fs): %s",
                        item_domain, worker_id, _age, _max_age, code,
                    )
                    continue

                db = _systems.get("db") if _systems else None
                if db is not None:
                    try:
                        loop = asyncio.get_running_loop()
                        # Fanout hiện mặc định là 1 item/code. Giữ nhánh
                        # fanout để tương thích nếu người dùng chủ động đặt
                        # ACCOUNTS_PER_CODE > 1.
                        if not item.get("fanout") and await loop.run_in_executor(_DB_EXECUTOR, db.is_code_used, item_domain, code):
                            if work_item_id and _durable_inbox is not None:
                                await loop.run_in_executor(
                                    _INBOX_STATE_EXECUTOR,
                                    _durable_inbox.complete_work_item,
                                    int(work_item_id),
                                )
                            elif inbox_id and _durable_inbox is not None:
                                await loop.run_in_executor(
                                    _INBOX_STATE_EXECUTOR,
                                    _durable_inbox.complete_item,
                                    int(inbox_id),
                                    claim_token,
                                )
                            continue
                    except Exception:
                        logger.warning(
                            "⚠️ [%s] lỗi khi đánh dấu item trùng hoàn tất (work_item=%s)",
                            item_domain, work_item_id, exc_info=True,
                        )

                account = await _get_next_available_account(
                    item_domain, _domain_accounts.get(item_domain, [])
                )
                if account is None:
                    append_code_history(event_type="FINAL_RESULT", code=code, target_url=item_target_url, account="", status="NO_ACCOUNT", message="Hết tài khoản")
                    account_temporarily_busy = _domain_has_unused_account(item_domain)
                    if account_temporarily_busy:
                        if work_item_id and _durable_inbox is not None:
                            await asyncio.get_running_loop().run_in_executor(
                                _INBOX_STATE_EXECUTOR,
                                _durable_inbox.retry_work_item,
                                int(work_item_id),
                                "all accounts temporarily busy",
                                1,
                            )
                        elif inbox_id and _durable_inbox is not None:
                            await asyncio.get_running_loop().run_in_executor(
                                _INBOX_EXECUTOR,
                                _durable_inbox.retry_or_fail,
                                int(inbox_id),
                                "all accounts temporarily busy",
                                1,
                                claim_token,
                            )
                    elif work_item_id and _durable_inbox is not None:
                        await asyncio.get_running_loop().run_in_executor(
                            _INBOX_STATE_EXECUTOR,
                            _durable_inbox.complete_work_item,
                            int(work_item_id),
                        )
                    elif inbox_id and _durable_inbox is not None:
                        await asyncio.get_running_loop().run_in_executor(
                            _INBOX_EXECUTOR,
                            _durable_inbox.complete_item,
                            int(inbox_id),
                            claim_token,
                        )
                    logger.warning(
                        "⏭️ [%s#%s] %s %s: %s",
                        item_domain,
                        worker_id,
                        "tạm hoãn" if account_temporarily_busy else "bỏ",
                        code,
                        "tài khoản đang bận" if account_temporarily_busy else "tất cả tài khoản đã đạt giới hạn",
                    )
                    continue

                if is_site_code_duplicate(item_domain, account["username"], code):
                    logger.info(
                        "⏭️ [%s#%s] duplicate domain-user-code: %s/%s/%s",
                        item_domain,
                        worker_id,
                        item_domain,
                        account["username"],
                        code,
                    )
                    if work_item_id and _durable_inbox is not None:
                        await asyncio.get_running_loop().run_in_executor(
                            _INBOX_STATE_EXECUTOR,
                            _durable_inbox.complete_work_item,
                            int(work_item_id),
                        )
                    elif inbox_id and _durable_inbox is not None:
                        await asyncio.get_running_loop().run_in_executor(
                            _INBOX_STATE_EXECUTOR,
                            _durable_inbox.complete_item,
                            int(inbox_id),
                            claim_token,
                        )
                    continue

                logger.info(f"⌨️ Đang nhập | {code} → {account['username']}")
                status = await _submit_code_with_account_retries(account, code, item_target_url, item_domain)

                if status == "PENDING_VERIFICATION":
                    # Không retry tự động: một widget Turnstile đang chờ người
                    # dùng không được giữ domain worker hoặc replay cùng mã.
                    # Giữ page cho xác minh thủ công, nhưng kết thúc item hiện
                    # tại để mã mới phía sau được xử lý ngay.
                    _clear_site_code_duplicate(item_domain, account["username"], code)
                    if work_item_id and _durable_inbox is not None:
                        await asyncio.get_running_loop().run_in_executor(
                            _INBOX_STATE_EXECUTOR,
                            _durable_inbox.complete_work_item,
                            int(work_item_id),
                        )
                    elif inbox_id and _durable_inbox is not None:
                        await asyncio.get_running_loop().run_in_executor(
                            _INBOX_EXECUTOR,
                            _durable_inbox.complete_item,
                            int(inbox_id),
                            claim_token,
                        )
                    logger.warning(
                        "⏸️ [%s#%s] kết thúc item %s vì Turnstile đang chờ xác minh; "
                        "nhả worker ngay, không retry mã cũ",
                        item_domain, worker_id, code,
                    )
                    continue

                if status == "SITE_UI_CHANGED":
                    if work_item_id and _durable_inbox is not None:
                        await asyncio.get_running_loop().run_in_executor(
                            _INBOX_STATE_EXECUTOR,
                            _durable_inbox.complete_work_item,
                            int(work_item_id),
                        )
                    elif inbox_id and _durable_inbox is not None:
                        await asyncio.get_running_loop().run_in_executor(
                            _INBOX_STATE_EXECUTOR,
                            _durable_inbox.complete_item,
                            int(inbox_id),
                            claim_token,
                        )
                    continue

                if status == "ACCOUNT_BLOCKED":
                    if work_item_id and _durable_inbox is not None:
                        await asyncio.get_running_loop().run_in_executor(
                            _INBOX_STATE_EXECUTOR,
                            _durable_inbox.retry_work_item,
                            int(work_item_id),
                            "account blocked; try next account",
                            0,
                        )
                    try:
                        queue.put_nowait({**item, "fanout": True})
                    except asyncio.QueueFull:
                        if work_item_id and _durable_inbox is not None:
                            await asyncio.get_running_loop().run_in_executor(
                                _INBOX_STATE_EXECUTOR,
                                _durable_inbox.retry_work_item,
                                int(work_item_id),
                                "account blocked and domain queue full",
                                15,
                            )
                        elif inbox_id and _durable_inbox is not None:
                            # ✅ FIX: bound bằng retry_or_fail thay vì retry() vô hạn.
                            await asyncio.get_running_loop().run_in_executor(
                                _INBOX_EXECUTOR,
                                _durable_inbox.retry_or_fail,
                                int(inbox_id),
                                "account blocked and domain queue full",
                                15,
                                claim_token,
                            )
                    continue

                if status == "INFRA_FAILURE":
                    if account is not None:
                        _clear_site_code_duplicate(item_domain, account["username"], code)
                    retry_state = "retried"
                    if work_item_id and _durable_inbox is not None:
                        retry_state = await asyncio.get_running_loop().run_in_executor(
                            _INBOX_STATE_EXECUTOR,
                            _durable_inbox.retry_work_item,
                            int(work_item_id),
                            f"browser infrastructure unavailable for {code}",
                            10,
                        )
                    elif inbox_id and _durable_inbox is not None:
                        retry_state = await asyncio.get_running_loop().run_in_executor(
                            _INBOX_EXECUTOR,
                            _durable_inbox.retry_or_fail,
                            int(inbox_id),
                            f"browser infrastructure unavailable for {code}",
                            10,
                            claim_token,
                        )
                    # DurableInbox sẽ tự đưa row pending trở lại queue sau
                    # backoff. Chỉ requeue trực tiếp khi item không có inbox
                    # row, tránh tạo hai bản sao của cùng một code.
                    if retry_state == "retried" and not inbox_id and not work_item_id:
                        try:
                            queue.put_nowait(dict(item))
                        except asyncio.QueueFull:
                            logger.warning(
                                "⚠️ [%s#%s] queue full while requeueing infrastructure failure for %s",
                                item_domain,
                                worker_id,
                                code,
                            )
                    continue

                # ✅ FIX RETRY-STORM + RESET TOÀN ROW:
                # _submit_code_with_account_retries() ĐÃ tự retry nội bộ
                # (theo MAX_RETRIES_PER_ACCOUNT) trước khi trả về đây, nên
                # mọi kết quả SUCCESS*/FAILED/NO_RESULT ở tầng này đều là
                # kết quả CUỐI CÙNG cho đúng 1 code trong dòng inbox (dòng
                # có thể chứa nhiều code nếu 1 tin nhắn Telegram có nhiều
                # mã). Trước đây:
                #   - FAILED  → mark_failed() ghi đè TOÀN BỘ dòng, xoá luôn
                #     tiến độ của các code anh em khác chưa xử lý xong.
                #   - còn lại (NO_RESULT) → retry() reset TOÀN BỘ dòng về
                #     'pending' + remaining_items=0, KHÔNG giới hạn số lần
                #     → bị replay lại (fetch lại message, extract lại code,
                #     submit lại) VÔ HẠN mỗi vài giây, làm nghẽn queue và
                #     trễ tin nhắn mới.
                # Giờ mọi outcome ở tầng code-đơn-lẻ này đều dùng
                # complete_item() để CHỈ giảm remaining_items của dòng
                # (không đụng tới code anh em khác, không replay lại toàn
                # bộ tin nhắn Telegram). Việc retry ở cấp submit đã được
                # _submit_code_with_account_retries() đảm nhiệm; muốn retry
                # nhiều hơn thì tăng MAX_RETRIES_PER_ACCOUNT trong .env.
                if work_item_id and _durable_inbox is not None:
                    await asyncio.get_running_loop().run_in_executor(
                        _INBOX_STATE_EXECUTOR,
                        _durable_inbox.complete_work_item,
                        int(work_item_id),
                    )
                elif inbox_id and _durable_inbox is not None:
                    await asyncio.get_running_loop().run_in_executor(
                        _INBOX_EXECUTOR, _durable_inbox.complete_item, int(inbox_id), claim_token
                    )

                icon = "✅" if status.startswith("SUCCESS") else ("❌" if status == "FAILED" else "⏰")
                logger.info(f"{icon} [{item_domain}#{worker_id}] {code} → {status}")
            except Exception as e:
                logger.error(f"❌ [{item_domain}#{worker_id}] {code}: {e}")
                if account is not None:
                    _clear_site_code_duplicate(item_domain, account["username"], code)
                if work_item_id and _durable_inbox is not None:
                    try:
                        await asyncio.get_running_loop().run_in_executor(
                            _INBOX_STATE_EXECUTOR,
                            _durable_inbox.retry_work_item,
                            int(work_item_id),
                            f"domain worker: {e}",
                            5,
                        )
                    except Exception:
                        logger.exception("❌ Không thể retry work item=%s", work_item_id)
                elif inbox_id and _durable_inbox is not None:
                    try:
                        # ✅ FIX: retry_or_fail — bound bởi MAX_INBOX_ATTEMPTS.
                        await asyncio.get_running_loop().run_in_executor(
                            _INBOX_EXECUTOR,
                            _durable_inbox.retry_or_fail,
                            int(inbox_id),
                            f"domain worker: {e}",
                            5,
                            claim_token,
                        )
                    except Exception:
                        logger.exception("❌ Không thể retry inbox row=%s", inbox_id)
                elif not work_item_id and not inbox_id:
                    # Defensive fallback cho chế độ chạy không có durable inbox:
                    # lỗi tạm thời không được làm rơi code khỏi domain queue.
                    volatile_attempts = int(item.get("_volatile_attempts", 0)) + 1
                    if volatile_attempts <= 3:
                        retry_item = dict(item)
                        retry_item["_volatile_attempts"] = volatile_attempts
                        try:
                            queue.put_nowait(retry_item)
                        except asyncio.QueueFull:
                            logger.error(
                                "❌ [%s#%s] mất item %s vì domain queue đầy và không có DurableInbox",
                                item_domain,
                                worker_id,
                                code,
                            )
                    else:
                        logger.error(
                            "🛑 [%s#%s] bỏ item %s sau %s lỗi khi không có DurableInbox",
                            item_domain,
                            worker_id,
                            code,
                            volatile_attempts - 1,
                        )
            finally:
                if account is not None:
                    _release_account_reservation(item_domain, account.get("username"))
                reset_log_context(item_tok)
                if current_task is not None:
                    _active_submit_tasks.discard(current_task)
                queue.task_done()
    finally:
        reset_log_context(log_tok)


def start_domain_workers():
    global _domain_accounts
    _domain_accounts = build_domain_accounts_map()
    dmap = {}
    for cfg in Config.CHANNEL_CONFIG.values():
        if not cfg.get("enabled", True):
            continue
        d = normalize_domain(cfg.get("url", ""))
        if d and d not in dmap:
            dmap[d] = cfg["url"]

    total = 0
    for d, url in dmap.items():
        if d in _domain_workers:
            continue
        n_acc = len(_domain_accounts.get(d, []))
        cap = _domain_slot_limit(d)
        wc = max(1, min(cap, n_acc or 1))
        ws = []
        for i in range(1, wc + 1):
            t = asyncio.create_task(domain_code_worker(d, url, worker_id=i), name=f"domain-{d}-{i}")
            ws.append(t)
            bot_state.bg_tasks.add(t)
        _domain_workers[d] = ws
        total += wc
    logger.info(f"🚀 {total} domain workers (browser-only) cho {len(dmap)} domain")


async def _mark_code_used_if_final(domain: str, code: str, status: str):
    if status not in ("SUCCESS_POINTS", "SUCCESS_NO_POINTS", "FAILED"):
        return
    db = _systems.get("db") if _systems else None
    if db is None:
        return
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_DB_EXECUTOR, db.mark_code_used, domain, code)
    except Exception as e:
        logger.debug(f"⚠️ mark_code_used: {e}")




# ═══════════════════════════════════════════════════════════════
# EXTRACT CODE
# ═══════════════════════════════════════════════════════════════
def validate_candidate(code: str, target_url: str, source: str = "normal"):
    try:
        return CodeValidator.validate_code(code, target_url, source=source)
    except TypeError:
        return CodeValidator.validate_code(code, target_url)


def get_filter_group_name(target_url: str) -> str:
    name, _ = CodeValidator.get_filter_group(target_url)
    return name


def unique_keep_order(items):
    seen, result = set(), []
    for it in items:
        c = CodeValidator.clean_code(it)
        if not c:
            continue
        u = c.upper()
        if u not in seen:
            seen.add(u)
            result.append(c)
    return result


def remove_noise_from_text(text: str) -> str:
    t = text or ""
    t = _URL_RE.sub(" ", t)
    t = _WWW_RE.sub(" ", t)
    t = _DOMAIN_RE.sub(" ", t)
    t = _HASHTAG_RE.sub(" ", t)
    return t.replace("：", ":").replace("|", " ").replace("•", " ")


def line_has_code_marker(line: str) -> bool:
    return bool(_CODE_MARKER_RE.search(line or ""))


def line_is_noise(line: str) -> bool:
    u = line.strip()
    if not u:
        return True
    if _NOISE_RE.search(u):
        return True
    if _CURRENCY_RE.search(u):
        return True
    return False


_VIETNAMESE_DIACRITIC_RE = re.compile(r"[àáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵđ]", re.IGNORECASE)


def _line_has_vietnamese(line: str) -> bool:
    if not line:
        return False
    if _VIETNAMESE_DIACRITIC_RE.search(line):
        return True
    lower = line.lower()
    return any(w in lower for w in CodeValidator.VIETNAMESE_TEXT_WORDS)


def extract_tokens_from_line(line: str):
    mn = getattr(Config, "CODE_MIN_LENGTH", 6)
    mx = getattr(Config, "CODE_MAX_LENGTH", 15)
    return [c for c in _TOKEN_RE.findall(line or "") if mn <= len(CodeValidator.clean_code(c)) <= mx]


def extract_spoiler_codes(event, target_url: str):
    codes = []
    message = getattr(event, "message", None)
    if message is None:
        return codes
    entities = getattr(message, "entities", None) or []
    full = getattr(message, "message", None) or getattr(message, "text", None) or ""
    if not full:
        return codes
    try:
        seen_codes = set()
        seen_candidates = set()
        for sp in _iter_spoiler_texts(full, entities):
            for line in (sp.splitlines() if "\n" in sp else [sp]):
                line = line.strip()
                if not line:
                    continue
                for tok in (extract_tokens_from_line(line) or [line]):
                    candidate_key = CodeValidator.clean_code(tok).upper()
                    if not candidate_key or candidate_key in seen_candidates:
                        continue
                    seen_candidates.add(candidate_key)
                    v = validate_candidate(tok, target_url, source="spoiler")
                    clean_code = v.get("clean_code")
                    normalized = str(clean_code or "").upper()
                    if v.get("valid") and normalized and normalized not in seen_codes:
                        seen_codes.add(normalized)
                        codes.append(clean_code)
                        logger.info(f"🔒 Spoiler: {v['clean_code']}")
    except Exception as e:
        logger.warning(f"⚠️ spoiler: {e}")
    return unique_keep_order(codes)


def extract_marker_near_codes(text: str, target_url: str):
    lines = [l.strip() for l in remove_noise_from_text(text).splitlines()]
    codes = []
    _, group_config = CodeValidator.get_filter_group(target_url)
    marker_scan_lines = max(
        0,
        int(group_config.get("marker_scan_lines", 3) or 0),
    )
    for i, line in enumerate(lines):
        if not line_has_code_marker(line):
            continue
        scan = [line] if line else []
        for off in range(1, marker_scan_lines + 1):
            if i + off < len(lines):
                scan.append(lines[i + off])
        for sl in scan:
            if line_is_noise(sl):
                continue
            for tok in extract_tokens_from_line(sl):
                v = validate_candidate(CodeValidator.clean_code(tok), target_url, source="marker")
                if v["valid"]:
                    codes.append(v["clean_code"])
                    logger.info(f"🎯 Marker: {v['clean_code']}")
    return unique_keep_order(codes)


_PLAIN_TEXT_ORDER_PHONE_CONTEXT_RE = re.compile(
    r"(?:mã\s*(?:đơn|don|dh)|đơn\s*hàng|don\s*hang|order|invoice|"
    r"transaction|tracking|mã\s*giao\s*dịch|ma\s*giao\s*dich|"
    r"sđt|sdt|điện\s*thoại|dien\s*thoai|phone|hotline|liên\s*hệ|lien\s*he)",
    re.IGNORECASE,
)


def _plain_text_token_is_safe(token: str, line: str) -> bool:
    """Conservative guard for unmarked text; markers/spoilers use stricter paths."""
    clean = CodeValidator.clean_code(token)
    if not clean or not any(ch.isalpha() for ch in clean):
        # Không tự suy đoán số thuần trong text thường: tránh số điện thoại,
        # mã OTP, ngày tháng và mã đơn chỉ gồm chữ số.
        return False

    digit_count = sum(ch.isdigit() for ch in clean)
    if digit_count >= 7 or (len(clean) >= 10 and digit_count / len(clean) >= 0.7):
        return False

    context = line[max(0, line.find(token) - 40): line.find(token) + len(token) + 40]
    if _PLAIN_TEXT_ORDER_PHONE_CONTEXT_RE.search(context):
        return False

    return True


def extract_plain_text_codes(text: str, target_url: str):
    """Scan ordinary text conservatively when no spoiler/marker code was found."""
    if not text:
        return []

    codes = []
    # HI88: mọi mã thật trong log đều có chữ số khi đi qua đường plain-text
    # (mã chỉ gồm chữ như DARKEST/CoinDesk/MONSTER/BORDEN là từ thường).
    hi88_requires_digit = get_filter_group_name(target_url) == "hi88"
    for raw_line in remove_noise_from_text(text).splitlines():
        line = raw_line.strip()
        if not line or line_has_code_marker(line) or line_is_noise(line):
            continue
        for token in extract_tokens_from_line(line):
            if not _plain_text_token_is_safe(token, line):
                continue
            if hi88_requires_digit and not any(ch.isdigit() for ch in token):
                continue
            result = validate_candidate(
                CodeValidator.clean_code(token), target_url, source="plain_text"
            )
            if result.get("valid"):
                codes.append(result.get("clean_code") or token)
                logger.info("🎯 Plain-text: %s", result.get("clean_code") or token)
    return unique_keep_order(codes)


def extract_hi88_near_link_codes(text: str, target_url: str) -> list:
    if not text:
        return []
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    codes = []
    for i, line in enumerate(lines):
        if "hi88-freecode.pages.dev" not in line.lower():
            continue
        nearby = []
        for off in range(1, 4):
            if i - off >= 0:
                nearby.append(lines[i - off])
            if i + off < len(lines):
                nearby.append(lines[i + off])
        for candidate_line in nearby:
            if line_is_noise(candidate_line):
                continue
            if _line_has_vietnamese(candidate_line):
                continue
            for tok in extract_tokens_from_line(candidate_line):
                v = validate_candidate(tok, target_url, source="marker")
                if v["valid"]:
                    codes.append(v["clean_code"])
                    logger.info(f"🎯 [HI88-LINK] {v['clean_code']}")
    return unique_keep_order(codes)


def extract_codes_by_regex(text: str, site_type: str = "qq88") -> list:
    if not text:
        return []
    codes = []
    if site_type == "qq88":
        BL = {"QQ88", "CODE", "DANGNHAP", "GAMEBAI", "NOHU", "CASINO", "REVIEWPHIM", "TINTUC", "KHUYENMAI", "GIFTCODE", "FREECODE", "CAMERA", "TROLL", "BONGDA", "THETHAO", "MINIGAME"}
        for m in _ALNUM_TOKEN_RE.findall(text):
            if any(k in m.upper() for k in BL):
                continue
            hl = any(c.isalpha() for c in m)
            hd = any(c.isdigit() for c in m)
            hll = any(c.islower() for c in m)
            hu = any(c.isupper() for c in m)
            if hl and (hd or (hll and hu)):
                codes.append(m)
    return list(dict.fromkeys(codes))


def extract_codes_from_message(
    event,
    raw_text: str,
    target_url: str,
    channel_name: str = "",
    include_text_after_spoiler: bool = False,
    spoiler_codes: list[str] | None = None,
):
    # Contract: spoiler is always attempted before marker/regex/OCR paths.
    group = get_filter_group_name(target_url)
    logger.debug(f"[EXTRACT] group={group} | url={target_url}")

    spoiler = (
        extract_spoiler_codes(event, target_url)
        if spoiler_codes is None
        else list(spoiler_codes)
    )
    if spoiler:
        logger.warning(f"🎯 [SPOILER] {len(spoiler)}: {spoiler}")
        # HI88/QQ88 có thể có nhiều mã ở spoiler và caption; phải ưu tiên
        # merge trước fast path để không bỏ sót caption khi cờ bật mặc định.
        merge_spoiler_text = group in {"qq88", "hi88"}
        if merge_spoiler_text:
            collected = list(spoiler)
        elif getattr(Config, "SPOILER_FAST_PATH", True):
            return unique_keep_order(spoiler)
        elif not include_text_after_spoiler:
            return unique_keep_order(spoiler)
        else:
            collected = list(spoiler)
    else:
        collected = []


    if group == "qq88":
        caption = (
            getattr(event.message, "message", None)
            or getattr(event.message, "text", None)
            or raw_text
            or ""
        ).strip().lower()
        has_media = bool(getattr(event, "media", None))
        has_text = bool((raw_text or caption).strip())
        if collected:
            logger.info("✅ [QQ88] spoiler/text đã nhận; không yêu cầu link khi đã có mã hợp lệ")
        elif has_text:
            logger.info("✅ [QQ88] text")
        elif has_media:
            if "tangquaqq88.com" in caption:
                logger.info("⏭️ [QQ88] chỉ có caption/link, không có spoiler code → bỏ qua, không OCR")
            else:
                logger.info("⏭️ [QQ88] no link → skip")
                return []
        else:
            logger.info("⏭️ [QQ88] nothing → skip")
            return []


    if group == "hi88":
        hc = extract_hi88_near_link_codes(raw_text, target_url)
        if hc:
            logger.warning(f"🎯 [HI88-LINK] {hc}")
            collected.extend(hc)

    mc = extract_marker_near_codes(raw_text, target_url)
    if mc:
        logger.warning(f"🎯 [MARKER] {len(mc)}: {mc}")
        collected.extend(mc)

    # QQ88/HI88 có thể phát nhiều mã trong cùng tin: một mã ở spoiler/marker
    # và mã khác ở caption/text thường. Vì vậy hai nhóm này vẫn phải quét
    # plain-text rồi merge; các nhóm khác giữ fast path chống nhận nhầm cũ.
    if not collected or group in {"qq88", "hi88"}:
        pc = extract_plain_text_codes(raw_text, target_url)
        if pc:
            logger.warning(f"🎯 [PLAIN-TEXT] {len(pc)}: {pc}")
            collected.extend(pc)

    if collected:
        merged = unique_keep_order(collected)
        logger.warning(f"🎯 [EXTRACT-MERGED] {len(merged)}: {merged}")
        return merged


    if group == "qq88" and "tangquaqq88.com" in raw_text.lower():
        cleaned = _URL_RE.sub("", raw_text)
        cleaned = _TME_RE.sub("", cleaned)
        rq = []
        for raw_code in extract_codes_by_regex(cleaned, "qq88"):
            # Mỗi candidate chỉ cần validate một lần; bản cũ gọi validate
            # hai lần (điều kiện và giá trị), gây thêm CPU trên fallback.
            result = validate_candidate(raw_code, target_url, source="regex")
            if result.get("valid"):
                rq.append(result.get("clean_code") or raw_code)
        if rq:
            logger.info(f"🎯 [QQ88-REGEX] {rq}")
            return list(dict.fromkeys(rq))

    return []


# ═══════════════════════════════════════════════════════════════
# SUBMIT CODE — BROWSER-ONLY
# ═══════════════════════════════════════════════════════════════
async def submit_code_safe(user: str, code: str, target_url: str, systems: dict):
    domain = normalize_domain(target_url)
    if not is_domain_enabled(domain):
        return {"success": False, "message": "Site disabled", "status": "SITE_DISABLED"}
    logger.info(f"🚀 [Browser] SUBMIT | {user} | {code} | {domain}")
    started = time.monotonic()
    try:
        adapter = _get_browser_adapter()
        result = await adapter.submit_for_target(user, code, target_url, systems)
        raw = dict(result.raw)
        raw.setdefault("success", result.success)
        raw.setdefault("message", result.message)
        if result.kind.value == "TECHNICAL_FAILURE":
            raw["_infra_failure"] = True
        raw.update({
            "route": "browser",
            "browser_kind": result.kind.value,
            "elapsed_seconds": result.elapsed_seconds or (time.monotonic() - started),
        })
        if raw.get("latency_ms"):
            logger.info(
                "[METRIC] submit domain=%s account=%s code=%s latency=%s",
                domain,
                user,
                code,
                raw["latency_ms"],
            )
        if raw.get("success") and domain in {"hi88-freecode.pages.dev", "tangquaqq88.com"}:
            schedule_tracked_task(
                send_submit_success_notification(
                    domain=domain,
                    user=user,
                    code=code,
                    has_points=bool(raw.get("has_points", False)),
                    result_message=str(raw.get("message", "") or ""),
                ),
                bot_state.bg_tasks,
                name=f"submit-success-notify-{domain}-{user}-{code}",
            )
        # record_outcome() owns normal SUCCESS/FAILED/UNKNOWN rows. Browser
        # infrastructure failures return before that function, so explicitly
        # add those attempts here; otherwise accounts with no available tab
        # disappear from the live dashboard entirely.
        if raw.get("_infra_failure") or raw.get("failure_kind") or raw.get("status") == "PENDING_VERIFICATION":
            elapsed_ms = float(raw.get("elapsed_seconds") or (time.monotonic() - started)) * 1000.0
            dashboard_status = str(raw.get("status") or raw.get("failure_kind") or "INFRA_FAILURE")
            update_dashboard(
                domain=domain,
                account=user,
                code=code,
                status=dashboard_status,
                rtt_ms=elapsed_ms,
                raw_response=str(raw.get("message") or result.message or "browser infrastructure failure"),
                latency_ms=raw.get("latency_ms"),
            )
        return raw
    except Exception as exc:
        logger.error(f"❌ [Browser|{domain}] submit lỗi: {exc}")
        return {
            "success": False,
            "message": str(exc),
            "route": "browser",
            "_infra_failure": True,
            "elapsed_seconds": time.monotonic() - started,
        }

async def submit_code_with_delay(user: str, code: str, target_url: str, systems: dict):
    domain = normalize_domain(target_url)
    timer = RequestTimer(request_id=f"submit:{domain}:{user}:{code}", kind="submit_request")

    ik = (domain, user, code.upper())
    if ik in bot_state._inflight_codes:
        timer.finish("duplicate")
        return {"success": False, "message": "In-flight duplicate"}
    bot_state._inflight_codes.add(ik)

    try:
        async with timer.stage_async("rate_limit_wait"):
            await get_domain_rate_limiter(domain).acquire()

        result = {"success": False, "message": "Not started"}
        submit_started = time.monotonic()
        # Global cap bảo vệ CPU/RAM Edge; semaphore domain vẫn giữ cân bằng
        # giữa các site bên trong global budget.
        async with _get_submit_semaphore():
            async with get_domain_semaphore(domain):
                try:
                    async with timer.stage_async("browser_submit"):
                        submit_timeout = max(5.0, float(getattr(Config, "SUBMIT_TIMEOUT_SECONDS", 45.0)))
                        result = await asyncio.wait_for(
                            submit_code_safe(user, code, target_url, systems),
                            timeout=submit_timeout,
                        )
                except asyncio.TimeoutError:
                    result = {
                        "success": False,
                        "message": f"Timeout {submit_timeout:.0f}s",
                        "_infra_failure": True,
                    }
                    update_dashboard(
                        domain=domain,
                        account=user,
                        code=code,
                        status="INFRA_FAILURE",
                        rtt_ms=submit_timeout * 1000.0,
                        raw_response=result["message"],
                    )
                except Exception as e:
                    result = {"success": False, "message": str(e), "_infra_failure": True}
                    update_dashboard(
                        domain=domain,
                        account=user,
                        code=code,
                        status="INFRA_FAILURE",
                        rtt_ms=(time.monotonic() - submit_started) * 1000.0,
                        raw_response=str(e),
                    )

        # ✅ RTT FIX: không còn sleep nhân tạo sau submit.
        # Một sleep ở đây chạy SAU KHI domain semaphore đã được release — tức là
        # KHÔNG throttle được gì (task kế tiếp trên cùng domain đã có thể
        # acquire semaphore ngay lập tức), chỉ cộng thêm ~150ms vào MỌI lần
        # đo RTT một cách vô ích. Việc giới hạn tốc độ submit trên mỗi
        # domain đã được đảm nhiệm đầy đủ bởi get_domain_rate_limiter()
        # (token bucket, acquire ở đầu hàm) + get_domain_semaphore()
        # (giới hạn concurrency) — không cần thêm sleep thừa ở cuối.
        return result
    finally:
        timer.finish("done")
        bot_state._inflight_codes.discard(ik)




# ═══════════════════════════════════════════════════════════════
# ĐĂNG KÝ KÊNH/TÀI KHOẢN
# ═══════════════════════════════════════════════════════════════
def build_unique_account_targets():
    items, seen = [], set()
    for cid, cfg in sorted(Config.CHANNEL_CONFIG.items(), key=lambda x: x[1].get("priority", 999)):
        if not cfg.get("enabled", True):
            continue
        url = cfg["url"]
        d = normalize_domain(url)
        accs = get_effective_domain_accounts(d)
        if not accs:
            continue
        for a in sorted(accs, key=lambda x: x.get("priority", 999)):
            k = (d, a["username"])
            if k in seen:
                continue
            seen.add(k)
            if _is_account_done_today(d, a["username"]):
                continue
            items.append({"chat_id": cid, "channel_name": cfg.get("name", ""), "target_url": url, "domain": d, "key": f"{d}|{a['username']}", "accounts": [a]})
    return items


async def init_channels_and_accounts():
    bot_state._site_code_seen.clear()
    targets = build_unique_account_targets()
    if not targets:
        logger.error("❌ No channels")
        return
    logger.info(
        "✅ %s target domain+tài khoản duy nhất đăng ký từ %s channel cấu hình",
        len(targets),
        len(Config.CHANNEL_CONFIG),
    )
    logger.info("🤖 BOT RUNNING (BROWSER-ONLY)")


# ═══════════════════════════════════════════════════════════════
# INIT SYSTEMS
# ═══════════════════════════════════════════════════════════════
async def init_systems():
    print_version_info()
    db = init_database(Config.DATABASE_PATH)
    hm, pm = init_monitoring()
    try:
        hm.set_high_memory_callback(_emergency_ram_cleanup)
    except Exception:
        pass
    get_shutdown_handler().setup(bot_state)
    start_history_writer()
    global _durable_inbox
    _durable_inbox = DurableInbox(
        getattr(Config, "TELEGRAM_INBOX_DB_PATH", "data/telegram_inbox.db"),
        lease_seconds=getattr(Config, "TELEGRAM_INBOX_LEASE_SECONDS", 300),
        # ✅ FIX retry-storm: giới hạn số lần thử + backoff tăng dần —
        # xem retry_or_fail() trong durable_inbox.py.
        max_attempts=getattr(Config, "MAX_INBOX_ATTEMPTS", 5),
        retry_base_delay=getattr(Config, "INBOX_RETRY_BASE_DELAY", 2.0),
        retry_max_delay=getattr(Config, "INBOX_RETRY_MAX_DELAY", 120.0),
    )
    recovery_mode = getattr(
        Config,
        "TELEGRAM_INBOX_RECOVERY_MODE",
        "at_least_once",
    )
    loop = asyncio.get_running_loop()
    if recovery_mode == "live_only":
        saved_checkpoints = await loop.run_in_executor(
            _INBOX_STATE_EXECUTOR,
            _durable_inbox.load_poll_checkpoints,
        )
        if saved_checkpoints:
            logger.warning(
                "⚠️ [Inbox] live_only không tương thích với checkpoint bền; "
                "chuyển sang at_least_once để không bỏ row đã được checkpoint"
            )
            recovery_mode = "at_least_once"
    if recovery_mode == "live_only":
        discarded = await loop.run_in_executor(
            _INBOX_STATE_EXECUTOR,
            _durable_inbox.discard_unfinished,
            "startup_live_only_discard_unfinished",
        )
        logger.warning(
            "🧹 [Inbox] live-only: bỏ qua %s row pending/processing từ phiên trước",
            discarded,
        )
    else:
        reclaimed = await loop.run_in_executor(
            _INBOX_STATE_EXECUTOR,
            _durable_inbox.reclaim_stale,
        )
        if reclaimed:
            logger.warning("♻️ [Inbox] at-least-once: thu hồi %s row bị kẹt", reclaimed)
    logger.info("📥 [Inbox] recovery_mode=%s", recovery_mode)
    return {"db": db, "inbox": _durable_inbox, "performance_monitor": pm, "health_monitor": hm}


def _emergency_ram_cleanup():
    try:
        n2 = len(bot_state._site_code_seen)
        bot_state._site_code_seen.clear()
        gc.collect()
        logger.warning(f"🧹 [RAM] seen={n2}")
    except Exception:
        pass


async def verify_telegram_session():
    try:
        me = await client.get_me()
        dc = client.session.dc_id
        logger.info(f"✅ Session: @{me.username or me.id} | DC{dc}")
        return True
    except AuthKeyDuplicatedError:
        raise
    except Exception as e:
        logger.error(f"❌ session: {e}")
        return False


async def sync_telegram_dialogs() -> bool:
    """Load all dialogs so Telethon caches channel entities/access_hashes."""
    if not getattr(Config, "TELEGRAM_SYNC_DIALOGS", True):
        logger.info("⏭️ Skip Telegram dialog sync (TELEGRAM_SYNC_DIALOGS=false)")
        return True

    timeout = max(
        10.0,
        float(getattr(Config, "TELEGRAM_DIALOG_SYNC_TIMEOUT", 120.0)),
    )
    try:
        logger.info("📥 Syncing dialogs để kích hoạt update stream (limit=None)...")
        dialogs = await asyncio.wait_for(
            client.get_dialogs(limit=None),
            timeout=timeout,
        )
        dialog_ids = {
            int(get_peer_id(dialog.entity))
            for dialog in dialogs
            if getattr(dialog, "entity", None) is not None
            and getattr(dialog.entity, "id", None) is not None
        }
        configured = {int(chat_id) for chat_id in Config.CHANNEL_CONFIG}
        cached = len(configured & dialog_ids)
        logger.info(
            "✅ Telegram dialogs synced | total=%s configured_cached=%s/%s",
            len(dialogs),
            cached,
            len(configured),
        )
        missing = sorted(configured - dialog_ids)
        if missing:
            logger.warning(
                "⚠️ Dialog cache còn thiếu %s channel; thử resolve trực tiếp "
                "để làm nóng entity cache",
                len(missing),
            )
            sem = asyncio.Semaphore(8)

            async def resolve_missing(chat_id: int) -> bool:
                async with sem:
                    try:
                        entity = await asyncio.wait_for(
                            client.get_entity(chat_id),
                            timeout=float(
                                getattr(Config, "TELEGRAM_CHANNEL_TIMEOUT", 10.0)
                            ),
                        )
                        return getattr(entity, "id", None) is not None
                    except AuthKeyDuplicatedError:
                        raise
                    except Exception as exc:
                        logger.warning(
                            "⚠️ Không resolve được channel %s trong sync: %s",
                            chat_id,
                            exc,
                        )
                        return False

            resolved = await asyncio.gather(
                *(resolve_missing(chat_id) for chat_id in missing)
            )
            resolved_count = sum(bool(item) for item in resolved)
            logger.info(
                "✅ Direct entity resolve trong sync: %s/%s",
                resolved_count,
                len(missing),
            )

            if resolved_count < len(missing):
                logger.warning(
                    "⚠️ Dialog cache chưa đủ sau direct resolve; tiếp tục khởi động "
                    "để bước verify_channels kiểm tra quyền truy cập thực tế"
                )
        return True
    except Exception as exc:
        logger.error("❌ Telegram dialog sync lỗi: %s", exc)
        return False


async def verify_channels_and_get_ids():
    sem = asyncio.Semaphore(8)
    valid = {}

    async def chk(cid, cfg):
        async with sem:
            try:
                entity = await asyncio.wait_for(
                    client.get_entity(cid),
                    timeout=float(
                        getattr(Config, "TELEGRAM_CHANNEL_TIMEOUT", 10.0)
                    ),
                )
                logger.info(
                    "✅ [Telegram channel] id=%s name=%s entity=%s",
                    cid,
                    cfg.get("name", ""),
                    getattr(entity, "title", None) or getattr(entity, "username", None) or type(entity).__name__,
                )
                return cid, cfg
            except AuthKeyDuplicatedError:
                raise
            except Exception as e:
                logger.error(
                    "❌ [Telegram channel INVALID/NO ACCESS] id=%s name=%s error=%s",
                    cid,
                    cfg.get("name", ""),
                    e,
                )
                return cid, None

    res = await asyncio.gather(*[chk(c, cfg) for c, cfg in Config.CHANNEL_CONFIG.items()])
    for cid, cfg in res:
        if cfg is not None:
            valid[cid] = cfg
    logger.info(
        "📋 %s/%s channels valid | handler will subscribe by numeric chat_id (not name)",
        len(valid),
        len(Config.CHANNEL_CONFIG),
    )
    return valid


# ═══════════════════════════════════════════════════════════════
# OCR
# ═══════════════════════════════════════════════════════════════
def _strip_promo_label_lines(t: str) -> str:
    lines = t.split("\n")
    kept = []
    skip = False
    for ln in lines:
        s = ln.strip()
        if skip:
            skip = False
            continue
        if _PROMO_LABEL_INLINE_RE.match(s):
            continue
        if _PROMO_LABEL_ONLY_RE.match(s):
            skip = True
            continue
        kept.append(ln)
    return "\n".join(kept)


def _ocr_candidates_for_line(line: str, *, strict_xx88: bool = False) -> list[str]:
    """Return code-shaped candidates without concatenating banner text.

    OCR commonly returns lines such as ``CODE: ABC123`` or ``MÃ KHUYẾN MÃI
    ABC123``. Validating the whole line would either join label+code or allow
    a long banner to reach the validator. XX88 therefore validates each
    alphanumeric token independently; the normal path keeps the legacy
    line-oriented behavior for sites that may use special characters.
    """
    raw = (line or "").strip()
    if not raw:
        return []
    if not strict_xx88:
        return [raw]
    candidates = _ALNUM_TOKEN_RE.findall(raw)
    if not candidates and raw.isalnum():
        candidates = [raw]
    return list(dict.fromkeys(candidates))


def _is_xx88_ocr_banner_token(candidate: str) -> bool:
    """Reject known XX88 promo/banner words before generic code validation."""
    return str(candidate or "").strip().upper() in _XX88_OCR_BANNER_TOKENS


def _is_qq88_ocr_noise_token(candidate: str) -> bool:
    """Reject QQ88 game names and UI labels after OCR normalization.

    OCR may split labels (``Bet VND``) or join them (``BetVND``), so matching
    is performed on a compact uppercase representation before applying the
    regex. This is scoped to QQ88 image OCR only.
    """
    compact = re.sub(r"[^A-Z0-9]", "", str(candidate or "").upper())
    return bool(compact and _QQ88_OCR_NOISE_RE.search(compact))


async def process_image_from_telegram(event, channel_config, systems):
    if not _is_ocr_allowed_channel(getattr(event, "chat_id", None)):
        logger.info("⏭️ [OCR] Bỏ qua: channel không nằm trong OCR_ALLOWED_CHANNEL_IDS")
        return {"success": False, "codes": [], "message": "OCR channel not allowed", "text": ""}
    if getattr(Config, "PAUSE_OCR_ON_HIGH_CPU", True):
        try:
            import monitoring as mon

            while True:
                hm = getattr(mon, "_health_monitor", None)
                if not hm or not getattr(hm, "pause_ocr", False):
                    break
                await asyncio.sleep(0.5)
        except Exception:
            pass
    async with _get_ocr_semaphore():
        return await _process_image_inner(event, channel_config, systems)


async def _process_image_inner(event, channel_config, systems):
    target_url = channel_config.get("url", "")
    req_t = get_current_timer()
    image_started = time.perf_counter()
    ocr_started = None
    size_bytes = None
    dl = None
    img_path = None
    ocr_cache_status = "not_checked"
    telegram_media_id = None

    try:
        logger.info("📸 [OCR] processing")
        tmp = tempfile.mkdtemp(prefix="ocr_")
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            report_download_started()
            try:
                if req_t:
                    async with req_t.stage_async("telegram_download"):
                        dl = await _media_download_manager.download(event, tmp)
                else:
                    dl = await _media_download_manager.download(event, tmp)
            finally:
                report_download_finished()

            img_path = dl.path
            size_bytes = dl.size_bytes

            if dl.dedup_hit:
                return {"success": False, "codes": [], "message": "dedup", "text": ""}
            if not img_path:
                return {"success": False, "codes": [], "message": "download fail", "text": ""}

            orig_ext = Path(img_path).suffix or ".jpg"
            uniq = Path(tmp) / f"ocr_{ts}{orig_ext}"
            Path(img_path).rename(uniq)
            img_path = str(uniq)

            loop = asyncio.get_running_loop()
            # RapidOCR/ONNXRuntime có thể mất 1–3 giây khi khởi tạo lần đầu.
            # Không gọi đồng bộ trên event loop vì sẽ làm trễ Telegram updates.
            ext = await loop.run_in_executor(_OCR_EXECUTOR, get_image_extractor)
            if ext is None:
                return {"success": False, "codes": [], "message": "OCR not init", "text": ""}

            crop = channel_config.get("ocr_crop")
            ocr_crops = channel_config.get("ocr_crops") or ([crop] if crop else [None])
            is_video = orig_ext.lower() in VIDEO_FORMATS
            telegram_media_id = _telegram_media_id(event)
            cache_key = await loop.run_in_executor(
                _MEDIA_EXECUTOR,
                _ocr_file_cache_key,
                img_path,
                target_url,
                ocr_crops,
                is_video,
                bool((channel_config or {}).get("ocr_single_code_mode", False)),
                telegram_media_id,
            )
            cached_result = _ocr_cache_get(cache_key)
            if cached_result is not None:
                ocr_cache_status = "hit"
                logger.info(
                    "⚡ [OCR] cache HIT media_id=%s file=%s",
                    telegram_media_id or "file-hash-fallback",
                    Path(img_path).name,
                )
                return cached_result
            ocr_cache_status = "miss"
            logger.debug(
                "🧊 [OCR] cache MISS media_id=%s file=%s",
                telegram_media_id or "file-hash-fallback",
                Path(img_path).name,
            )

            fs_fallback = []
            if is_video:
                fs = channel_config.get("ocr_frame_seconds")
                # Nhiều mốc giây = thứ tự ưu tiên, KHÔNG phải OCR đồng thời:
                # chỉ lấy 1 frame ở mốc đầu; các mốc sau chỉ dùng khi mốc đầu
                # không ra code hợp lệ (mỗi lần vẫn chỉ 1 frame).
                if fs and len(fs) > 1:
                    fs_fallback = list(fs[1:])
                    fs = [fs[0]]

                def _frames():
                    return extract_frames_from_video(
                        img_path,
                        tmp,
                        max_frames=max(2, int(getattr(Config, "VIDEO_OCR_MAX_FRAMES", 3))),
                        # Khi có nhiều vùng, giữ frame đầy đủ rồi crop riêng
                        # từng vùng ở bước OCR bên dưới.
                        crop_box=None if len(ocr_crops) > 1 else crop,
                        frame_seconds=fs,
                    )

                if req_t:
                    async with req_t.stage_async("video_frame_extraction"):
                        frame_paths = await loop.run_in_executor(_MEDIA_EXECUTOR, _frames)
                else:
                    frame_paths = await loop.run_in_executor(_MEDIA_EXECUTOR, _frames)

                if not frame_paths and not fs_fallback:
                    return {"success": False, "codes": [], "message": "no frames", "text": ""}
                targets = frame_paths
            else:
                targets = [img_path]

            group = get_filter_group_name(target_url)
            is_xx88 = normalize_domain(target_url) == "xx88code.com"
            is_qq88 = normalize_domain(target_url) == "tangquaqq88.com"
            single_code_mode = bool((channel_config or {}).get("ocr_single_code_mode", False))
            require_consensus = bool((channel_config or {}).get("ocr_require_consensus", True))

            def text_has_valid_code(text: str) -> bool:
                """Cheap early-exit predicate; full extraction still validates later."""
                for line in (text or "").splitlines():
                    line = _strip_promo_label_lines(line).strip()
                    if len(line) < 4:
                        continue
                    for candidate in _ocr_candidates_for_line(line, strict_xx88=is_xx88):
                        c = CodeValidator.clean_code(candidate)
                        if not c or (is_xx88 and _is_xx88_ocr_banner_token(c)):
                            continue
                        if is_qq88 and _is_qq88_ocr_noise_token(c):
                            continue
                        if group == "multi_site_strict":
                            c = c.upper()
                        try:
                            valid = CodeValidator.validate_code(c, target_url=target_url, source="image_ocr")
                        except TypeError:
                            valid = CodeValidator.validate_code(c, target_url)
                        if valid.get("valid"):
                            return True
                return False

            async def run_ocr(paths, already_cropped, stop_early=False):
                crop_boxes = [None] if already_cropped else ocr_crops
                async def one(index, path, crop_box):
                    result = await loop.run_in_executor(
                        _OCR_EXECUTOR, ext.extract_code_from_image, path, "eng", crop_box
                    )
                    return index, result.strip() if isinstance(result, str) else ""

                # Không tạo task cho toàn bộ frame×crop cùng lúc. Khi có burst
                # media, danh sách task lớn sẽ đẩy inference dư thừa vào
                # ThreadPoolExecutor; cancel() sau early-exit không dừng được
                # inference đã bắt đầu trong ONNXRuntime. Giữ cửa sổ bằng số
                # worker OCR để task mới chỉ được nạp khi có slot rảnh.
                jobs = [
                    (index, path, crop_box)
                    for index, (path, crop_box) in enumerate(
                        ( (path, crop_box) for path in paths for crop_box in crop_boxes )
                    )
                ]
                window = max(1, int(getattr(Config, "MAX_CONCURRENT_OCR", 2)))
                active = set()
                next_job = 0

                def schedule_next() -> None:
                    nonlocal next_job
                    while next_job < len(jobs) and len(active) < window:
                        active.add(asyncio.create_task(one(*jobs[next_job])))
                        next_job += 1

                results = []
                schedule_next()
                try:
                    while active:
                        done, active = await asyncio.wait(
                            active, return_when=asyncio.FIRST_COMPLETED
                        )
                        early_exit = False
                        for completed in done:
                            try:
                                index, result = await completed
                            except Exception:
                                continue
                            if result:
                                results.append((index, result))
                            if stop_early and result and text_has_valid_code(result):
                                logger.info(
                                    "⚡ [OCR-EarlyExit] Tìm thấy code hợp lệ, "
                                    "hủy các tác vụ OCR còn lại"
                                )
                                early_exit = True
                                break
                        if early_exit:
                            for task in active:
                                task.cancel()
                            await asyncio.gather(*active, return_exceptions=True)
                            active.clear()
                            break
                        schedule_next()
                finally:
                    if active:
                        for task in active:
                            task.cancel()
                        await asyncio.gather(*active, return_exceptions=True)
                return [text for _, text in sorted(results)]

            use_fast = (
                bool(getattr(Config, "OCR_FAST_PATH", True))
                and bool((channel_config or {}).get("ocr_single_code_mode", False))
                and len(targets) > 1
                and len(ocr_crops) == 1
            )

            ocr_started = time.perf_counter()
            if use_fast:
                min_chars = max(1, int(getattr(Config, "OCR_FAST_MIN_CHARS", 8)))
                crop_for_ocr = None if is_video else crop

                _, text = await ext.extract_first_valid_code(
                    targets,
                    crop_box=crop_for_ocr,
                    already_cropped=is_video,
                    is_valid_fn=lambda value: len((value or "").replace("\n", "")) >= min_chars,
                )

                per_frame = [text] if text else []
            else:
                if req_t:
                    async with req_t.stage_async("ocr"):
                        per_frame = await run_ocr(
                            targets,
                            is_video,
                            stop_early=(not require_consensus or single_code_mode),
                        )
                else:
                    per_frame = await run_ocr(
                        targets,
                        is_video,
                        stop_early=(not require_consensus or single_code_mode),
                    )

            if is_video and fs_fallback and not any(text_has_valid_code(t) for t in per_frame):
                for fallback_sec in fs_fallback:
                    logger.info(
                        "↩️ [OCR] Frame giây %s không có code — thử giây %s (mỗi lần 1 frame)",
                        fs[0], fallback_sec,
                    )

                    def _frames_fallback(sec=fallback_sec):
                        return extract_frames_from_video(
                            img_path,
                            tmp,
                            max_frames=1,
                            crop_box=None if len(ocr_crops) > 1 else crop,
                            frame_seconds=[sec],
                        )

                    fallback_paths = await loop.run_in_executor(_MEDIA_EXECUTOR, _frames_fallback)
                    if not fallback_paths:
                        continue
                    per_frame = await run_ocr(
                        fallback_paths,
                        is_video,
                        stop_early=(not require_consensus or single_code_mode),
                    )
                    if any(text_has_valid_code(t) for t in per_frame):
                        break

            extracted = "\n".join(per_frame)

            if not extracted:
                return {"success": False, "codes": [], "message": "no text", "text": ""}

            logger.info(f"✅ [OCR] {len(extracted)} chars")

            codes = []

            if not codes:
                use_cons = (
                    len(per_frame) >= 2
                    and bool((channel_config or {}).get("ocr_require_consensus", True))
                )
                seen_idx = {}
                sample = {}
                for i, ft in enumerate(per_frame):
                    ft = _strip_promo_label_lines(ft)
                    for ln in ft.split("\n"):
                        ln = ln.strip()
                        if len(ln) < 4:
                            continue
                        for candidate in _ocr_candidates_for_line(ln, strict_xx88=is_xx88):
                            c = CodeValidator.clean_code(candidate)
                            if not c or len(c) < 4:
                                continue
                            if is_xx88 and _is_xx88_ocr_banner_token(c):
                                logger.debug("⏭️ [OCR|XX88] bỏ token banner/promo: %s", c)
                                continue
                            if is_qq88 and _is_qq88_ocr_noise_token(c):
                                logger.debug("⏭️ [OCR|QQ88] bỏ tên game/nhãn giao diện: %s", c)
                                continue
                            if group == "multi_site_strict":
                                c = c.upper()
                            try:
                                v = CodeValidator.validate_code(c, target_url=target_url, source="image_ocr")
                            except TypeError:
                                v = CodeValidator.validate_code(c, target_url)
                            if not v["valid"]:
                                continue
                            k = c.upper()
                            seen_idx.setdefault(k, set()).add(i)
                            sample.setdefault(k, (c, ln))
                for k, idxs in seen_idx.items():
                    c, ln = sample[k]
                    n = len(idxs)
                    if use_cons and n < 2:
                        continue
                    codes.append({"code": c, "raw": ln, "confidence": 0.9 if n < 2 else 0.97})
                    logger.info(f"✅ [OCR] {c}")

            single = (channel_config or {}).get("ocr_single_code_mode", False)
            if single and len(codes) > 1:
                codes = [codes[0]]

            if codes:
                max_ocr = int(
                    (channel_config or {}).get(
                        "max_ocr_codes_per_batch",
                        getattr(Config, "MAX_OCR_CODES_PER_BATCH", 4),
                    )
                )
                if len(codes) > max_ocr:
                    codes = codes[:max_ocr]
                if not codes:
                    return {"success": False, "codes": [], "message": "too many", "text": extracted}
                result = {"success": True, "codes": codes, "message": f"{len(codes)} codes", "text": extracted}
                _ocr_cache_put(cache_key, result)
                return result
            result = {"success": False, "codes": [], "message": "no valid", "text": extracted}
            _ocr_cache_put(cache_key, result)
            return result
        finally:
            total_elapsed_ms = (time.perf_counter() - image_started) * 1000.0
            ocr_elapsed_ms = (
                (time.perf_counter() - ocr_started) * 1000.0
                if ocr_started is not None
                else None
            )
            logger.info(
                "⏱️ [OCR-TIMING] image=%s domain=%s total_ms=%.1f ocr_ms=%s "
                "download_ms=%s frames=%s size_bytes=%s cache_status=%s media_id=%s",
                Path(img_path).name if img_path else "(download-failed)",
                normalize_domain(target_url),
                total_elapsed_ms,
                f"{ocr_elapsed_ms:.1f}" if ocr_elapsed_ms is not None else "n/a",
                f"{getattr(dl, 'elapsed_ms', 0.0):.1f}" if dl is not None else "n/a",
                len(locals().get("targets", []) or []),
                size_bytes if size_bytes is not None else "n/a",
                ocr_cache_status,
                telegram_media_id or "none",
            )
            if req_t:
                req_t.emit("media_complete", media=True, file_size_bytes=size_bytes, media_extension=Path(img_path).suffix.lower() if img_path else None, download_elapsed_ms=getattr(dl, "elapsed_ms", None), download_bytes_per_sec=getattr(dl, "bytes_per_sec", None), download_attempts=getattr(dl, "attempts", None), download_dedup_hit=getattr(dl, "dedup_hit", None))
            try:
                shutil.rmtree(tmp)
            except Exception:
                pass
    except Exception as e:
        logger.error(f"❌ [OCR] {e}\n{traceback.format_exc()}")
        return {"success": False, "codes": [], "message": f"OCR error: {e}", "text": ""}





async def submit_codes_from_image(
    user, codes_data, target_url, channel_config, systems, inbox_id=None, claim_token=None,
):
    domain = normalize_domain(target_url)
    if not is_domain_enabled(domain):
        return
    codes = unique_keep_order([item.get("code", "") for item in codes_data or []])
    db = systems.get("db") if systems else None
    if db is not None and codes:
        unused = await asyncio.get_running_loop().run_in_executor(_DB_EXECUTOR, db.unused_codes, domain, codes)
        codes = [code for code in codes if code.upper() in unused]
    row = None
    if inbox_id and _durable_inbox is not None:
        row = await asyncio.get_running_loop().run_in_executor(_INBOX_STATE_EXECUTOR, _durable_inbox.get, int(inbox_id))
    items = [{"code": code, "domain": domain, "target_url": target_url,
              "source": "image_ocr", "channel_name": channel_config.get("name", ""),
              "inbox_id": inbox_id, "claim_token": claim_token,
              "msg_ts": (row or {}).get("message_date"), "fanout_index": 0, "fanout": False}
             for code in codes]
    items = _build_spoiler_dispatch_items(items)
    if inbox_id and _durable_inbox is not None:
        if not items:
            await asyncio.get_running_loop().run_in_executor(
                _INBOX_STATE_EXECUTOR, _durable_inbox.mark_ignored, int(inbox_id), "ocr_no_code", claim_token)
            return
        ids = await asyncio.get_running_loop().run_in_executor(
            _INBOX_STATE_EXECUTOR, _durable_inbox.create_work_items, int(inbox_id), items, claim_token)
        items = [{**item, "work_item_id": item_id} for item, item_id in zip(items, ids) if item_id]
    queue = get_domain_queue(domain)
    for item in items:
        item_id = item.get("work_item_id")
        if item_id:
            bot_state._work_item_enqueued_ids.add(item_id)
        try:
            await queue.put(item)
        except BaseException:
            if item_id:
                bot_state._work_item_enqueued_ids.discard(item_id)
            raise
    logger.info("📥 [IMG] %s mã → domain queue %s (retry độc lập)", len(items), domain)


# ═══════════════════════════════════════════════════════════════
# MESSAGE PROCESSING
# ═══════════════════════════════════════════════════════════════
async def _enqueue_ocr_media_job(event, cfg, systems, account: str | None) -> bool:
    """Đẩy media sang queue riêng để message worker được giải phóng sớm."""
    global _ocr_media_queue
    if _ocr_media_queue is None:
        start_ocr_media_workers()
    inbox_id = getattr(event, "inbox_id", None)
    claim_token = getattr(event, "claim_token", None)
    if inbox_id and _durable_inbox is not None:
        await asyncio.get_running_loop().run_in_executor(
            _INBOX_STATE_EXECUTOR,
            _durable_inbox.set_remaining,
            int(inbox_id),
            1,
            claim_token,
        )
    try:
        _ocr_media_queue.put_nowait((event, cfg, systems, account))
        logger.info(
            "📥 [OCR-QUEUE] enqueue chat=%s message=%s q=%s/%s",
            getattr(event, "chat_id", "?"),
            getattr(getattr(event, "message", None), "id", "?"),
            _ocr_media_queue.qsize(),
            _ocr_media_queue.maxsize,
        )
        return True
    except asyncio.QueueFull:
        if inbox_id and _durable_inbox is not None:
            await asyncio.get_running_loop().run_in_executor(
                _INBOX_STATE_EXECUTOR,
                _durable_inbox.retry_or_fail,
                int(inbox_id),
                "ocr_media_queue_full",
                2.0,
                claim_token,
            )
        logger.warning("⚠️ [OCR-QUEUE] đầy — giữ row=%s trong durable inbox để retry", inbox_id)
        return False


async def ocr_media_worker(worker_id: int):
    logger.info("👷 OCR/media worker #%s ready", worker_id)
    while bot_state.is_running or (_ocr_media_queue is not None and not _ocr_media_queue.empty()):
        if _ocr_media_queue is None:
            await asyncio.sleep(0.2)
            continue
        job = None
        try:
            job = await asyncio.wait_for(_ocr_media_queue.get(), timeout=1.0)
            event, cfg, systems, account = job
            try:
                result = await process_image_from_telegram(event, cfg, systems)
                if result.get("success"):
                    await submit_codes_from_image(
                        account,
                        result["codes"],
                        cfg["url"],
                        cfg,
                        systems,
                        getattr(event, "inbox_id", None),
                        getattr(event, "claim_token", None),
                    )
                else:
                    await _ignore_inbox_event(event, "ocr_no_result")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("❌ [OCR-QUEUE#%s] %s: %s", worker_id, type(exc).__name__, exc)
                inbox_id = getattr(event, "inbox_id", None)
                if inbox_id and _durable_inbox is not None:
                    await asyncio.get_running_loop().run_in_executor(
                        _INBOX_STATE_EXECUTOR,
                        _durable_inbox.mark_failed,
                        int(inbox_id),
                        f"ocr_media_worker: {exc}",
                        getattr(event, "claim_token", None),
                    )
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            break
        finally:
            if job is not None and _ocr_media_queue is not None:
                _ocr_media_queue.task_done()


def start_ocr_media_workers():
    global _ocr_media_queue
    if _ocr_media_queue is None:
        _ocr_media_queue = asyncio.Queue(
            maxsize=max(1, int(getattr(Config, "OCR_MEDIA_QUEUE_MAXSIZE", 32)))
        )
    if _ocr_media_workers:
        return
    count = max(1, int(getattr(Config, "OCR_MEDIA_WORKERS", 2)))
    for i in range(1, count + 1):
        _ocr_media_workers.append(
            asyncio.create_task(ocr_media_worker(i), name=f"ocr-media-{i}")
        )
    logger.info("🚀 %s OCR/media workers started (queue=%s)", count, _ocr_media_queue.maxsize)


async def _ignore_inbox_event(event, reason: str) -> None:
    inbox_id = getattr(event, "inbox_id", None)
    claim_token = getattr(event, "claim_token", None)
    if inbox_id and _durable_inbox is not None:
        await asyncio.get_running_loop().run_in_executor(
            _INBOX_EXECUTOR,
            _durable_inbox.mark_ignored,
            int(inbox_id),
            reason,
            claim_token,
        )


async def _process_telegram_message_impl(event):
    if not _systems:
        await _ignore_inbox_event(event, "systems_not_ready")
        return
    if event.chat_id not in Config.CHANNEL_CONFIG:
        await _ignore_inbox_event(event, "channel_not_configured")
        return
    cfg = Config.CHANNEL_CONFIG.get(event.chat_id)
    if not cfg:
        await _ignore_inbox_event(event, "channel_config_missing")
        return
    if not cfg.get("enabled", True):
        await _ignore_inbox_event(event, "channel_disabled")
        return

    msg_date = getattr(event.message, "date", None)
    if msg_date is None:
        await _ignore_inbox_event(event, "message_date_missing")
        return

    if msg_date.tzinfo is None:
        msg_date = msg_date.replace(tzinfo=timezone.utc)

    target_url = cfg["url"]
    accounts = get_effective_domain_accounts(target_url)
    raw_text = "\n".join(
        dict.fromkeys(
            str(value).strip()
            for value in (
                getattr(event.message, "text", None),
                getattr(event.message, "message", None),
            )
            if str(value or "").strip()
        )
    )
    qq88_spoiler_only = normalize_domain(target_url) == "tangquaqq88.com"

    logger.info(f"📨 Nhận tin | {cfg['name']} | {len(raw_text)} ký tự")

    # KJC SPECIAL MODE
    if (
        getattr(Config, "KJC_SPECIAL_MODE", True)
        and is_kjc_special_channel(event.chat_id)
    ):
        kjc_codes = extract_kjc_spoiler_codes(event)
        if not kjc_codes:
            logger.info("⏭️ [KJC] Không phát hiện code spoiler hợp lệ")
            # KJC có thể đăng ảnh/video chỉ chứa code. Nếu có media thì
            # chuyển tiếp xuống OCR fallback; chỉ bỏ qua khi hoàn toàn không
            # có media để OCR.
            if not (getattr(event, "media", None) or getattr(getattr(event, "message", None), "media", None)):
                await _ignore_inbox_event(event, "kjc_no_code")
                return
        else:
            channel_name = cfg.get("name", "")
            labeled_domains = detect_kjc_labeled_domains(event, raw_text)
            kjc_items = build_kjc_broadcast_items(kjc_codes, channel_name, labeled_domains)

            logger.info(
                "📡 [KJC] phát hiện %s code, route=%s, sang %s domain",
                len(kjc_codes),
                ",".join(labeled_domains) if labeled_domains else "broadcast-all",
                len(labeled_domains) if labeled_domains else len(_KJC_BROADCAST_DOMAINS),
            )

            eligible_kjc = []
            for item in kjc_items:
                item["inbox_id"] = getattr(event, "inbox_id", None)
                item["claim_token"] = getattr(event, "claim_token", None)
                item["msg_ts"] = msg_date
                eligible_kjc.append(item)

            inbox_id = getattr(event, "inbox_id", None)
            fanout_kjc = _build_spoiler_dispatch_items(eligible_kjc)

            if inbox_id and fanout_kjc and _durable_inbox is not None:
                item_ids = await asyncio.get_running_loop().run_in_executor(
                    _INBOX_EXECUTOR,
                    _durable_inbox.create_work_items,
                    int(inbox_id),
                    fanout_kjc,
                    getattr(event, "claim_token", None),
                )
                fanout_kjc = [
                    {**item, "work_item_id": item_id}
                    for item, item_id in zip(fanout_kjc, item_ids) if item_id
                ]
            
            queued_kjc = len(fanout_kjc)
            for item in fanout_kjc:
                item_domain = item["domain"]

                q = get_domain_queue(item_domain)
                work_item_id = item.get("work_item_id")
                if work_item_id:
                    bot_state._work_item_enqueued_ids.add(int(work_item_id))
                try:
                    await q.put(item)
                except BaseException:
                    if work_item_id:
                        bot_state._work_item_enqueued_ids.discard(int(work_item_id))
                    raise
                logger.info("📥 [KJC] %s -> %s", item["code"], item_domain)

            if inbox_id and not queued_kjc:
                await _ignore_inbox_event(event, "kjc_queue_empty")
            return

    cached_codes = None

    event_media = getattr(event, "media", None) or getattr(
        getattr(event, "message", None), "media", None
    )
    if qq88_spoiler_only:
        # QQ88 intentionally accepts ONLY codes hidden by Telegram spoiler.
        # Do this before every caption/plain-text/OCR branch so ordinary
        # promotional words and random alphanumeric text cannot become codes.
        spoiler_codes = extract_spoiler_codes(event, target_url)
        if not spoiler_codes:
            logger.info("⏭️ [QQ88] Không có code spoiler hợp lệ — bỏ qua caption/text/media")
            await _ignore_inbox_event(event, "qq88_spoiler_only_no_code")
            return
        cached_codes = unique_keep_order(spoiler_codes)
        raw_text = ""
        event_media = None
        logger.info("🔒 [QQ88] Chỉ nhận spoiler: %s", cached_codes)
    if event_media:
        raw_text = ""
        is_video_ch = bool(cfg.get("has_video", False))
        _m = getattr(event.message, "media", None)
        is_vid = (
            isinstance(_m, MessageMediaDocument)
            and any(isinstance(a, DocumentAttributeVideo) for a in getattr(getattr(_m, "document", None), "attributes", []))
        )
        # Ảnh/video + Telegram spoiler: bắt entity spoiler trước mọi
        # caption/text và trước khi xét OCR. Đây là fast path cho mọi media
        # có mã che spoiler, không phân biệt loại media.
        media_spoiler_started = time.perf_counter()
        media_spoiler_codes = extract_spoiler_codes(event, target_url)
        if media_spoiler_codes:
            cached_codes = unique_keep_order(media_spoiler_codes)
            media_spoiler_elapsed_ms = (time.perf_counter() - media_spoiler_started) * 1000.0
            logger.warning(
                "🎯 [MEDIA-SPOILER FAST] %.1f ms | %s: %s codes %s — tiếp tục quét caption, bỏ qua OCR",
                media_spoiler_elapsed_ms,
                cfg.get("name", ""),
                len(cached_codes),
                cached_codes,
            )
            # Vẫn đọc caption/text để gộp các mã khác nằm ngoài spoiler.
            event_media = None

        if event_media is None and cached_codes is not None:
            caption = "\n".join(
                dict.fromkeys(
                    str(value).strip()
                    for value in (
                        getattr(event.message, "message", None),
                        getattr(event.message, "text", None),
                    )
                    if str(value or "").strip()
                )
            )
            if caption:
                caption_codes = extract_codes_from_message(
                    event, caption, target_url, channel_name=cfg.get("name", ""),
                    spoiler_codes=cached_codes,
                )
                cached_codes = unique_keep_order(cached_codes + (caption_codes or []))
        elif event_media:

            # Telethon có thể đặt caption ở .text thay vì .message, nhất là
            # media/channel post có spoiler entity. Chỉ đọc caption khi
            # fast path spoiler không tìm thấy code.
            caption = "\n".join(
                dict.fromkeys(
                    str(value).strip()
                    for value in (
                        getattr(event.message, "message", None),
                        getattr(event.message, "text", None),
                    )
                    if str(value or "").strip()
                )
            )
            found_caption = False
            if caption:
                ex = extract_codes_from_message(
                    event, caption, target_url, channel_name=cfg.get("name", ""),
                    spoiler_codes=[],
                )
                if ex:
                    raw_text = caption
                    found_caption = True
                    cached_codes = ex

        if cached_codes is None:
            # Mọi channel đều ưu tiên spoiler/text. OCR media chỉ chạy theo
            # danh sách channel fallback và điều kiện link riêng của HI88.
            ocr_allowed = _is_ocr_allowed_channel(event.chat_id)
            if found_caption:
                pass
            elif "hi88-freecode.pages.dev" in target_url.lower() and not _has_hi88_code_link(event):
                logger.info(f"⏭️ [{cfg.get('name')}] HI88 media khuyến mãi không có link nhập code — bỏ qua OCR")
                await _ignore_inbox_event(event, "hi88_media_without_code_link")
                return
            elif not ocr_allowed:
                logger.info(f"⏭️ [{cfg.get('name')}] media/OCR không được phép — chỉ xử lý spoiler/text")
                await _ignore_inbox_event(event, "media_not_ocr_allowed")
                return
            else:
                # Ảnh của kênh OCR whitelist luôn được xử lý. Cờ video chỉ chặn
                # video, không được chặn ảnh thường của PHÁT CODE XX88.
                if is_vid and cfg.get("ocr_video_enabled") is False:
                    await _ignore_inbox_event(event, "ocr_video_disabled")
                    return
                acc = accounts[0]["username"] if accounts else None
                if not acc:
                    await _ignore_inbox_event(event, "ocr_no_account")
                    return
                if is_video_ch or is_vid:
                    await _enqueue_ocr_media_job(event, cfg, _systems, acc)
                    return
                if not raw_text:
                    c2 = "\n".join(
                        dict.fromkeys(
                            str(value).strip()
                            for value in (
                                getattr(event.message, "message", None),
                                getattr(event.message, "text", None),
                            )
                            if str(value or "").strip()
                        )
                    )
                    if c2:
                        ex = extract_codes_from_message(
                            event, c2, target_url, channel_name=cfg.get("name", ""),
                            spoiler_codes=[],
                        )
                        if ex:
                            raw_text = c2
                        else:
                            await _enqueue_ocr_media_job(event, cfg, _systems, acc)
                            return
                    else:
                        await _enqueue_ocr_media_job(event, cfg, _systems, acc)
                        return
    msg_ts = event.message.date
    delay = measure_telegram_delay_fast(msg_ts)
    mk = (event.chat_id, event.message.id)
    # Với tin spoiler/media, raw_text bị đặt "" nên hash cũ luôn giống nhau:
    # tin được edit để thêm/đổi mã spoiler bị coi là trùng và rơi mất trong
    # 5 phút. Gộp cả chữ ký spoiler và các mã đã tách vào hash.
    ch = _message_content_hash(
        f"{raw_text}|{'|'.join(cached_codes or [])}",
        _spoiler_entity_signature(event.message),
    )
    prev = bot_state._processed_message_hashes.get(mk)
    if prev and prev[0] == ch and not getattr(event, "inbox_id", None):
        return
    bot_state._processed_message_hashes[mk] = (ch, time.time())

    final_codes = (cached_codes if cached_codes is not None else extract_codes_from_message(event, raw_text, target_url, channel_name=cfg.get("name", "")))
    if not final_codes:
        await _ignore_inbox_event(event, "no_code")
        return

    logger.info(f"✅ Đã nhận code | {cfg['name']} | {', '.join(final_codes)}")
    for c in final_codes:
        append_code_history(event_type="DETECTED", code=c, target_url=target_url, channel=cfg.get("name", ""), source="telegram", status="PENDING", telegram_delay=delay)

    domain = normalize_domain(target_url)
    dedup = []
    db = _systems["db"] if _systems else None
    if db is not None:
        try:
            loop = asyncio.get_running_loop()
            unused = await loop.run_in_executor(_DB_EXECUTOR, db.unused_codes, domain, final_codes)
            dedup = [c for c in final_codes if str(c).strip().upper() in unused]
        except Exception as e:
            logger.error(
                "❌ Không kiểm tra được batch dedup, bỏ qua code để tránh submit trùng: %s",
                e,
            )
    else:
        dedup = list(final_codes)
    if not dedup:
        logger.info(
            f"⏭️ [{domain}] bỏ {', '.join(final_codes)}: code đã dùng/đã gửi trước đó (dedup vĩnh viễn)"
        )
        await _ignore_inbox_event(event, "duplicate_or_used_code")
        return

    avail = sorted(accounts, key=lambda a: a.get("priority", 999))
    if not avail:
        logger.warning(f"⏭️ [{domain}] bỏ {', '.join(dedup)}: không có tài khoản khả dụng")
        await _ignore_inbox_event(event, "no_account")
        return

    q = get_domain_queue(domain)
    cn = cfg.get("name", "")
    inbox_id = getattr(event, "inbox_id", None)
    dispatch_items = _build_spoiler_dispatch_items([
        {
            "code": c,
            "channel_name": cn,
            "domain": domain,
            "target_url": target_url,
            "inbox_id": inbox_id,
            "claim_token": getattr(event, "claim_token", None),
            "msg_ts": msg_date,
        }
        for c in dedup
    ])
    n = len(dispatch_items)
    if inbox_id and n and _durable_inbox is not None:
        item_ids = await asyncio.get_running_loop().run_in_executor(
            _INBOX_EXECUTOR,
            _durable_inbox.create_work_items,
            int(inbox_id),
            dispatch_items,
            getattr(event, "claim_token", None),
        )
        dispatch_items = [
            {**item, "work_item_id": item_id}
            for item, item_id in zip(dispatch_items, item_ids) if item_id
        ]
    for item in dispatch_items:
        # Mặc định mỗi code chỉ vào queue một lần. Nhiều code trong cùng
        # batch sẽ được reservation round-robin cấp cho các account khác nhau
        # đang rảnh, mỗi item chiếm một tab tại một thời điểm.
        work_item_id = item.get("work_item_id")
        if work_item_id:
            bot_state._work_item_enqueued_ids.add(int(work_item_id))
        try:
            await q.put(item)
        except BaseException:
            if work_item_id:
                bot_state._work_item_enqueued_ids.discard(int(work_item_id))
            raise
    logger.info(
        f"📥 Đã xếp hàng | {len(dedup)} code | {n} lượt nhập | {domain}"
    )
    if inbox_id and not n:
        # ✅ FIX: retry_or_fail (bound) thay vì retry() vô hạn.
        await asyncio.get_running_loop().run_in_executor(
            _INBOX_EXECUTOR,
            _durable_inbox.retry_or_fail,
            int(inbox_id),
            "domain queue full",
            None,
            getattr(event, "claim_token", None),
        )


async def process_telegram_message(event):
    timer = RequestTimer.from_event(event, kind="telegram_request")
    queue_wait_ms = getattr(event, "queue_wait_ms", None)
    if queue_wait_ms is not None:
        queue_wait_seconds = max(0.0, float(queue_wait_ms)) / 1000.0
        timer.started_perf -= queue_wait_seconds
        timer.stages["ingress_queue_wait"] = queue_wait_seconds
    tok = set_current_timer(timer)
    cfg = Config.CHANNEL_CONFIG.get(getattr(event, "chat_id", None))
    tag = get_site_log_tag(cfg.get("url", "")) if cfg else f"chat_{getattr(event,'chat_id','?')}"
    lt = set_log_context(tag)
    try:
        with timer.stage("process_message"):
            r = await _process_telegram_message_impl(event)
        timer.finish("ok")
        return r
    except asyncio.CancelledError:
        timer.finish("cancelled")
        raise
    except Exception as e:
        timer.finish("error", error_type=type(e).__name__)
        raise
    finally:
        reset_current_timer(tok)
        reset_log_context(lt)


# ═══════════════════════════════════════════════════════════════
# MESSAGE WORKERS
# ═══════════════════════════════════════════════════════════════
async def message_worker(wid: int):
    logger.info(f"👷 Worker #{wid}")

    # During graceful shutdown, finish items already present in the queue;
    # new Telegram ingress is stopped by the shutdown path.  A hard timeout
    # below still prevents shutdown from waiting forever on a stuck browser.
    while bot_state.is_running or (message_queue is not None and not message_queue.empty()):
        row_id = None
        claimed_row_id = None
        row = None
        claimed_token = None
        queue_age_ms = None
        queued_at_perf = None
        try:
            item = await asyncio.wait_for(message_queue.get(), timeout=1.0)
            if isinstance(item, tuple) and len(item) == 2:
                queued_at_perf, row_id = item
                queue_age_ms = (time.perf_counter() - queued_at_perf) * 1000
                if queue_age_ms > 500:
                    logger.warning(
                        "🐌 Telegram message queue delay %.0fms | qsize=%s/%s",
                        queue_age_ms,
                        message_queue.qsize(),
                        message_queue.maxsize,
                    )
            else:
                row_id = item
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"❌ worker#{wid} lấy message lỗi: {e}")
            await asyncio.sleep(0.2)
            continue

        try:
            async with _get_proc_semaphore():
                inbox = _durable_inbox
                if inbox is None:
                    bot_state._inbox_enqueued_ids.discard(int(row_id))
                    logger.warning(
                        "⚠️ [Worker #%s] DurableInbox chưa sẵn sàng — "
                        "giữ row=%s để drain lại sau khi inbox khởi tạo",
                        wid,
                        row_id,
                    )
                    continue
                bot_state._inbox_enqueued_ids.discard(int(row_id))
                row = await asyncio.get_running_loop().run_in_executor(_INBOX_EXECUTOR, inbox.claim, int(row_id))
                if not row:
                    continue
                claimed_row_id = int(row_id)
                claimed_token = row.get("claim_token")
                # Tin đã quá cũ (backlog sau restart / hàng đợi dồn): giftcode đã
                # hết hạn, không tốn client.get_messages hay tab nữa — nhường
                # chỗ cho tin mới đang chờ phía sau trong cùng queue.
                _stale, _age = first_known_age(
                    (row.get("message_date"), row.get("created_at")),
                    float(getattr(Config, "CODE_MAX_AGE_SECONDS", 120.0)),
                )
                if _stale:
                    _inbox_message_cache.pop(int(row_id), None)
                    await asyncio.get_running_loop().run_in_executor(
                        _INBOX_EXECUTOR,
                        inbox.mark_ignored,
                        int(row_id),
                        "stale_message",
                        claimed_token,
                    )
                    logger.info(
                        "⏭️ [Worker #%s] bỏ tin cũ %.0fs (> %.0fs) row=%s",
                        wid, _age, float(getattr(Config, "CODE_MAX_AGE_SECONDS", 120.0)), row_id,
                    )
                    continue
                # A row in DurableInbox is proof that this process already
                # accepted the event.  Process it even when
                # TELEGRAM_CATCH_UP=false; that flag only prevents replaying
                # Telegram history, not recovery of locally durable events.
                # Luồng realtime đã giữ message object từ event Telethon;
                # chỉ gọi lại Telegram API khi cache miss (recovery sau restart
                # hoặc item đã nằm quá lâu trong durable inbox).
                msg = _inbox_message_cache.pop(int(row_id), None)
                if msg is None:
                    if await asyncio.get_running_loop().run_in_executor(
                        _INBOX_STATE_EXECUTOR,
                        inbox.has_active_work_items,
                        int(row_id),
                    ):
                        # Recovery path: the item drain loop will resume pending
                        # domain/account work items; do not replay Telegram text.
                        continue
                    msg = await client.get_messages(int(row["chat_id"]), ids=int(row["message_id"]))
                if not msg:
                    # ✅ FIX: retry_or_fail (bound) thay vì retry() vô hạn —
                    # tránh 1 message_id không lấy lại được lặp mãi.
                    await asyncio.get_running_loop().run_in_executor(
                        _INBOX_EXECUTOR,
                        inbox.retry_or_fail,
                        int(row_id),
                        "Telegram message unavailable",
                        None,
                        claimed_token,
                    )
                    continue
                ev = SimpleNamespace(
                    chat_id=int(row["chat_id"]),
                    message=msg,
                    media=getattr(msg, "media", None),
                    inbox_id=int(row_id),
                    claim_token=claimed_token,
                    queue_wait_ms=(
                        (time.perf_counter() - queued_at_perf) * 1000
                        if queued_at_perf is not None else None
                    ),
                )
                await process_telegram_message(ev)
                await asyncio.get_running_loop().run_in_executor(
                    _INBOX_EXECUTOR,
                    inbox.mark_ignored_if_empty,
                    int(row_id),
                    "no_code_or_not_routed",
                    claimed_token,
                )
        except asyncio.CancelledError:
            # A claimed row is marked ``processing`` before browser/OCR work.
            # Requeue it before allowing cancellation to propagate; otherwise
            # a graceful stop can strand it until the lease expires.
            if claimed_row_id is not None and _durable_inbox is not None:
                try:
                    current = await asyncio.get_running_loop().run_in_executor(
                        _INBOX_EXECUTOR,
                        _durable_inbox.get,
                        claimed_row_id,
                    )
                    if current and current.get("status") == "processing":
                        await asyncio.get_running_loop().run_in_executor(
                            _INBOX_EXECUTOR,
                            _durable_inbox.retry,
                            claimed_row_id,
                            "worker cancelled during shutdown",
                            0,
                            claimed_token,
                        )
                        logger.warning(
                            "♻️ [Inbox] requeue row=%s vì worker bị hủy",
                            claimed_row_id,
                        )
                except Exception:
                    logger.exception(
                        "❌ [Inbox] không requeue được row=%s khi worker bị hủy",
                        claimed_row_id,
                    )
            raise
        except Exception as e:
            logger.error(f"❌ worker#{wid}: {e}")
            try:
                if _durable_inbox is not None:
                    # ✅ FIX: retry_or_fail (bound) thay vì retry() vô hạn.
                    await asyncio.get_running_loop().run_in_executor(
                        _INBOX_EXECUTOR,
                        _durable_inbox.retry_or_fail,
                        int(row_id),
                        str(e),
                        None,
                        claimed_token,
                    )
            except Exception:
                logger.exception("❌ [Worker #%s] retry_or_fail lỗi, row=%s", wid, row_id)
        finally:
            try:
                message_queue.task_done()
            except Exception:
                pass


async def _inbox_drain_loop():
    global _inbox_wakeup
    if _inbox_wakeup is None:
        _inbox_wakeup = asyncio.Event()
    while bot_state.is_running:
        try:
            inbox = _durable_inbox
            # New ingress wakes this loop immediately. During backlog, avoid
            # repeatedly querying SQLite when the in-memory queue is already
            # near capacity; workers will drain it before the next sweep.
            queue_has_capacity = (
                message_queue is not None
                and not message_queue.full()
                and (
                    message_queue.maxsize <= 1
                    or message_queue.qsize() < int(message_queue.maxsize * 0.90)
                )
            )
            if inbox is not None:
                due_items = await asyncio.get_running_loop().run_in_executor(
                    _INBOX_STATE_EXECUTOR,
                    inbox.due_work_items,
                    int(getattr(Config, "TELEGRAM_INBOX_DRAIN_BATCH", 250)),
                    tuple(domain for domain, queue in _domain_queues.items() if queue.full()),
                    tuple(Config.DOMAIN_TO_CHANNEL_URL),
                )
                for durable_item in due_items:
                    item_id = int(durable_item["id"])
                    if item_id in bot_state._work_item_enqueued_ids:
                        continue
                    domain = str(durable_item.get("domain") or "")
                    if not domain:
                        continue
                    item = {
                        "work_item_id": item_id,
                        "inbox_id": int(durable_item["inbox_id"]),
                        "claim_token": durable_item.get("claim_token"),
                        "code": durable_item["code"],
                        "msg_ts": durable_item.get("message_date"),
                        "domain": domain,
                        "target_url": durable_item.get("target_url") or "",
                        "fanout": True,
                        "fanout_index": int(durable_item.get("fanout_index") or 0),
                    }
                    q = get_domain_queue(domain)
                    try:
                        q.put_nowait(item)
                        bot_state._work_item_enqueued_ids.add(item_id)
                    except asyncio.QueueFull:
                        continue
                if queue_has_capacity:
                    ids = await asyncio.get_running_loop().run_in_executor(
                        _INBOX_EXECUTOR,
                        inbox.pending_ids,
                        int(getattr(Config, "TELEGRAM_INBOX_DRAIN_BATCH", 250)),
                        tuple(Config.CHANNEL_CONFIG),
                    )
                    for row_id in ids:
                        if message_queue.full():
                            break
                        if int(row_id) in bot_state._inbox_enqueued_ids:
                            continue
                        try:
                            message_queue.put_nowait((time.perf_counter(), int(row_id)))
                            bot_state._inbox_enqueued_ids.add(int(row_id))
                        except asyncio.QueueFull:
                            break
            # New-message ingress signals this event after the durable insert,
            # so normal traffic is drained immediately. The timeout remains as
            # a recovery sweep for rows left pending after a crash/reconnect.
            # Wakeup event xử lý traffic mới ngay lập tức; interval chỉ là recovery
            # sweep. Tôn trọng TELEGRAM_INBOX_DRAIN_INTERVAL (.env = 0.05s).
            interval = max(0.02, float(getattr(Config, "TELEGRAM_INBOX_DRAIN_INTERVAL", 0.05)))
            try:
                await asyncio.wait_for(_inbox_wakeup.wait(), timeout=interval)
                _inbox_wakeup.clear()
            except asyncio.TimeoutError:
                pass
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.debug("⚠️ [Inbox] drain lỗi: %s", exc)
            await asyncio.sleep(1.0)


def start_message_workers():
    global message_queue, _inbox_wakeup

    mx = int(getattr(Config, "MESSAGE_QUEUE_MAXSIZE", 2000))
    n = max(1, int(getattr(Config, "MESSAGE_WORKERS", 4)))

    if message_queue is None:
        message_queue = asyncio.Queue(maxsize=mx)
        qm = get_queue_manager()
        if qm is not None:
            qm.register("message", message_queue, on_drop=_on_message_queue_drop)

    if message_workers:
        return

    if _inbox_wakeup is None:
        _inbox_wakeup = asyncio.Event()

    for i in range(1, n + 1):
        task = asyncio.create_task(message_worker(i), name=f"worker-{i}")
        message_workers.append(task)

    global _inbox_drain_task
    if _inbox_drain_task is None or _inbox_drain_task.done():
        _inbox_drain_task = asyncio.create_task(_inbox_drain_loop(), name="inbox-drain")

    logger.info(f"🚀 {n} workers started")


def clear_old_message_queue() -> int:
    """
    Xóa các item đã nằm trong queue trước khi handler hoạt động.
    Không xóa tin trên Telegram.
    """
    if message_queue is None:
        return 0

    removed = 0
    while True:
        try:
            item = message_queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        try:
            if isinstance(item, tuple) and len(item) == 2:
                bot_state._inbox_enqueued_ids.discard(int(item[1]))
                _inbox_message_cache.pop(int(item[1]), None)
        except (TypeError, ValueError):
            pass
        try:
            message_queue.task_done()
        except Exception:
            pass

        removed += 1

    if removed:
        logger.warning(
            "🧹 Đã xóa %s tin cũ khỏi message queue",
            removed,
        )

    return removed


async def _clear_runtime_state(**ctx) -> str:
    """Dọn queue/cache đang chạy, giữ nguyên code_history.db."""
    lines = ["🧹 DỌN DẸP RUNTIME"]

    mq_removed = clear_old_message_queue()
    lines.append(f"• Message queue: đã xoá {mq_removed} item")

    domain_removed_total = 0
    for domain, q in list(_domain_queues.items()):
        removed = 0
        while True:
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                q.task_done()
            except Exception:
                pass
            removed += 1
        domain_removed_total += removed
        if removed:
            lines.append(f"  - domain '{domain}': {removed} item")
    lines.append(f"• Domain queues: đã xoá {domain_removed_total} item")

    counts = {
        "site_code_seen": len(bot_state._site_code_seen),
        "processed_hash": len(bot_state._processed_message_hashes),
        "inflight_codes": len(bot_state._inflight_codes),
        "inflight_accounts": len(bot_state._inflight_accounts),
        "inbox_ids": len(bot_state._inbox_enqueued_ids),
        "message_cache": len(_inbox_message_cache),
    }
    bot_state._site_code_seen.clear()
    bot_state._processed_message_hashes.clear()
    bot_state._inbox_enqueued_ids.clear()
    bot_state._work_item_enqueued_ids.clear()
    _inbox_message_cache.clear()
    lines.append(
        "• Cache RAM: " + ", ".join(f"{key}={value}" for key, value in counts.items())
    )

    if _durable_inbox is not None:
        n = await asyncio.get_running_loop().run_in_executor(
            _INBOX_EXECUTOR,
            _durable_inbox.discard_unfinished,
            "manual_clear_command",
        )
        lines.append(f"• Durable inbox: đã ignore {n} dòng pending/processing")
    else:
        lines.append("• Durable inbox: chưa sẵn sàng — bỏ qua")

    lines.append("✅ Xong — code_history.db được GIỮ NGUYÊN")
    return "\n".join(lines)


async def setup_telegram_handler():
    """Đăng ký đường nhận tin Telegram (xem ``tg_ingest``).

    Một ``TelegramReceiver`` duy nhất nhận cả push lẫn poll, loại trùng ở một
    chỗ, rồi ``IngressStage`` ghi hộp thư bền theo lô và đưa row vào
    ``message_queue`` như hợp đồng cũ (``(perf, inbox_id)`` + cache message).
    """
    global _receiver, _ingress_stage, _ingress_stage_task, _channel_poll_task

    if bot_state.handler_registered:
        return

    chats = []
    for k in Config.CHANNEL_CONFIG.keys():
        try:
            chats.append(int(k))
        except Exception:
            continue

    if not chats:
        logger.error("❌ Không có chat nào trong CHANNEL_CONFIG")
        return
    source_filter = bool(getattr(Config, "TELEGRAM_FILTER_AT_SOURCE", True))

    # Dọn queue trước khi tạo worker để không có worker lấy nhầm event cũ.
    clear_old_message_queue()
    start_message_workers()

    await asyncio.sleep(0)

    def accept(message) -> bool:
        """Chỉ nhận tin đăng sau lúc bot khởi động (chừa 30s lệch đồng hồ)."""
        message_date = getattr(message, "date", None)
        if message_date is None:
            return False
        if message_date.tzinfo is None:
            message_date = message_date.replace(tzinfo=timezone.utc)
        cutoff = BOT_START_TIME - timedelta(seconds=30)
        if getattr(Config, "TELEGRAM_CATCH_UP", False):
            cutoff = BOT_START_TIME - timedelta(minutes=5)
        return message_date >= cutoff

    def accept_backfill(message) -> bool:
        """Validate Telegram date, but don't reject durable recovery by boot age."""
        return getattr(message, "date", None) is not None

    def build_row(env) -> tuple:
        msg = env.message
        text = str(getattr(msg, "text", None) or getattr(msg, "message", None) or "")
        has_media = bool(getattr(msg, "media", None))
        content_hash = DurableInbox.content_hash(text, has_media, _spoiler_entity_signature(msg))
        return (
            env.chat_id,
            int(getattr(msg, "id", 0) or 0),
            getattr(msg, "date", None),
            env.edited,
            text,
            has_media,
            content_hash,
        )

    def persist(rows: list) -> list:
        inbox = _durable_inbox
        if inbox is None:
            return [None] * len(rows)
        return inbox.enqueue_many(rows)

    def persist_checkpointed(rows: list, chat_id: int, message_id: int, edit_ts: int):
        inbox = _durable_inbox
        if inbox is None:
            return None
        return inbox.enqueue_many_and_checkpoint(
            rows,
            chat_id=chat_id,
            message_id=message_id,
            edit_ts=edit_ts,
        )

    def on_row(env, inbox_id: int) -> None:
        """Sau khi đã ghi bền: giao row cho message worker."""
        global _queue_full_counter
        msg = env.message
        # Giữ message object để worker không phải gọi lại get_messages qua mạng.
        _inbox_message_cache[inbox_id] = msg
        if len(_inbox_message_cache) > _INBOX_MSG_CACHE_MAX:
            for cache_id in list(_inbox_message_cache)[: max(1, _INBOX_MSG_CACHE_MAX // 10)]:
                _inbox_message_cache.pop(cache_id, None)
        try:
            # FIFO: nếu RAM queue đầy, row vẫn nằm trong DurableInbox và
            # _inbox_drain_loop sẽ đưa lại vào queue sau.
            message_queue.put_nowait((time.perf_counter(), inbox_id))
        except asyncio.QueueFull:
            _queue_full_counter += 1
            _inbox_message_cache.pop(inbox_id, None)
            if _inbox_wakeup is not None:
                _inbox_wakeup.set()
            logger.warning(
                "⚠️ Telegram message queue full %s lần | row=%s giữ trong durable inbox | qsize=%s/%s",
                _queue_full_counter, inbox_id, message_queue.qsize(), message_queue.maxsize,
            )
            return
        bot_state._inbox_enqueued_ids.add(inbox_id)
        if _inbox_wakeup is not None:
            _inbox_wakeup.set()
        _queue_full_counter = 0
        bot_state.last_accepted_message_at = time.monotonic()
        bot_state.accepted_message_count += 1
        cfg = Config.CHANNEL_CONFIG.get(env.chat_id, {})
        logger.info(
            "📨 [Telegram accepted] chat_id=%s name=%s message_id=%s media=%s source=%s edited=%s "
            "age=%.1fs ingress_to_durable_ms=%.1f queue=%s/%s",
            env.chat_id,
            cfg.get("name", ""),
            getattr(msg, "id", "?"),
            bool(getattr(msg, "media", None)),
            env.source,
            env.edited,
            env.age_s,
            (time.perf_counter() - env.seen_perf) * 1000.0,
            message_queue.qsize(),
            message_queue.maxsize,
        )

    stage = IngressStage(
        build_row=build_row,
        persist=persist,
        on_row=on_row,
        persist_checkpointed=persist_checkpointed,
        executor=_INBOX_INGRESS_EXECUTOR,
        retries=int(getattr(Config, "TELEGRAM_INBOX_ENQUEUE_RETRIES", 3) or 3),
        log=logger,
    )
    receiver = TelegramReceiver(
        client,
        chats,
        stage.put,
        accept=accept,
        accept_backfill=accept_backfill,
        commit_backfill=stage.commit_backfill_batch,
        bootstrap_checkpoint=(
            _durable_inbox.initialize_poll_checkpoint
            if _durable_inbox is not None else None
        ),
        cfg=ReceiverConfig.from_config(Config),
        log=logger,
    )
    if _durable_inbox is not None:
        checkpoints = await asyncio.get_running_loop().run_in_executor(
            _INBOX_INGRESS_EXECUTOR, _durable_inbox.load_poll_checkpoints
        )
        receiver.restore_checkpoints(checkpoints)
        logger.info("📍 [Poll] đã nạp checkpoint bền cho %s channel", len(checkpoints))

    async def h_new(ev):
        receiver.on_push(ev, edited=False)

    async def h_edit(ev):
        receiver.on_push(ev, edited=True)

    # --- CHẨN ĐOÁN TẠM THỜI: vì sao push NewMessage hiếm khi về (xoá sau khi có kết luận) ---
    _diag_monitored = {-c - 1000000000000 for c in chats if c < -1000000000000}
    _diag = {"types": {}, "log_left": 60}

    async def h_raw(update):
        """Probe nhẹ cho heartbeat + chẩn đoán push: có update nào từ Telegram không."""
        bot_state.last_raw_update_at = time.monotonic()
        bot_state.raw_update_count += 1
        try:
            name = type(update).__name__
            _diag["types"][name] = _diag["types"].get(name, 0) + 1
            msg = getattr(update, "message", None)
            cid = getattr(getattr(msg, "peer_id", None), "channel_id", None) or getattr(update, "channel_id", None)
            if cid in _diag_monitored and _diag["log_left"] > 0:
                _diag["log_left"] -= 1
                mdate = getattr(msg, "date", None)
                age = (time.time() - mdate.timestamp()) if mdate else -1.0
                logger.info("🔬 [PushDiag] %s channel_id=%s msg_id=%s age=%.1fs",
                            name, cid, getattr(msg, "id", None), age)
            if bot_state.raw_update_count % 500 == 0:
                logger.info("🔬 [PushDiag] loại update (cộng dồn): %s", _diag["types"])
        except Exception:
            pass

    # Lọc tại nguồn chỉ là tối ưu; whitelist thật nằm trong receiver.offer().
    client.add_event_handler(h_new, events.NewMessage(chats=chats) if source_filter else events.NewMessage())
    client.add_event_handler(h_edit, events.MessageEdited(chats=chats) if source_filter else events.MessageEdited())
    client.add_event_handler(h_raw, events.Raw())

    for old in (_channel_poll_task, _ingress_stage_task):
        if old is not None and not old.done():
            old.cancel()
    _receiver = receiver
    _ingress_stage = stage
    _ingress_stage_task = asyncio.create_task(stage.run(), name="ingress-stage")
    poll_interval = float(getattr(Config, "CHANNEL_POLL_INTERVAL", 1.0) or 0.0)
    _channel_poll_task = (
        asyncio.create_task(receiver.run(), name="telegram-receiver")
        if poll_interval > 0 else None
    )

    bot_state.handler_registered = True

    domain_counts = {}
    for _cid, _cfg in Config.CHANNEL_CONFIG.items():
        _d = normalize_domain(_cfg.get("url", ""))
        domain_counts[_d] = domain_counts.get(_d, 0) + 1

    logger.info(
        "✅ Handler ready | %s channels | QQ88=%s HI88=%s | OCR-only=%s | push filter=%s | "
        "poll=%s",
        len(chats),
        domain_counts.get("tangquaqq88.com", 0),
        domain_counts.get("hi88-freecode.pages.dev", 0),
        sorted(getattr(Config, "OCR_ALLOWED_CHANNEL_IDS", set())),
        source_filter,
        f"mỗi {poll_interval:.2f}s, tuần tự" if poll_interval > 0 else "tắt",
    )


# ═══════════════════════════════════════════════════════════════
# WATCHDOGS
# ═══════════════════════════════════════════════════════════════
def _cleanup_stale_ocr_tmp() -> int:
    base = Path(tempfile.gettempdir())
    mx = float(getattr(Config, "OCR_TEMP_DIR_MAX_AGE_SECONDS", 3600))
    n = 0
    now = time.time()
    try:
        entries = list(base.iterdir())
    except OSError:
        return 0
    for e in entries:
        try:
            if not e.is_dir() or not e.name.startswith("ocr_"):
                continue
            if now - e.stat().st_mtime <= mx:
                continue
            cleanup_stale_files(e, max_age_seconds=0)
            shutil.rmtree(e, ignore_errors=True)
            n += 1
        except Exception:
            continue
    return n


async def _cleanup_scheduler():
    last_media_cleanup = 0.0
    media_cleanup_interval = max(
        3600.0,
        float(getattr(Config, "MEDIA_CLEANUP_INTERVAL_SECONDS", 3 * 24 * 60 * 60)),
    )
    # ✅ FIX: quét dọn định kỳ các dòng inbox 'pending' bị kẹt quá lâu (lỗi
    # không xác định, crash giữa chừng, hoặc sót lại sau khi mark_failed
    # không được gọi đúng chỗ...) — dùng đúng DurableInbox.ignore_pending_before()
    # vốn đã được viết sẵn nhưng trước đây KHÔNG hề được gọi ở đâu cả.
    # Giftcode gần như luôn hết hạn rất nhanh nên an toàn khi bỏ qua sau
    # INBOX_MAX_PENDING_AGE_SECONDS (mặc định 15 phút).
    last_inbox_sweep = 0.0
    inbox_sweep_interval = max(
        30.0,
        float(getattr(Config, "INBOX_PENDING_SWEEP_INTERVAL_SECONDS", 300.0)),
    )
    inbox_max_pending_age = max(
        30.0,
        float(getattr(Config, "INBOX_MAX_PENDING_AGE_SECONDS", 900.0)),
    )
    while bot_state.is_running:
        try:
            await asyncio.sleep(float(getattr(Config, "INPUT_CACHE_CLEANUP_INTERVAL", 30)))
            _prune_site_code_seen()
            _prune_processed_message_hashes()
            now = time.monotonic()
            if now - last_media_cleanup >= media_cleanup_interval:
                loop = asyncio.get_running_loop()
                removed = await loop.run_in_executor(_MEDIA_EXECUTOR, _cleanup_stale_ocr_tmp)
                last_media_cleanup = now
                logger.info(
                    "🧹 [MEDIA CLEANUP] đã xóa %s thư mục OCR/media cũ (chu kỳ %.0f ngày)",
                    removed,
                    media_cleanup_interval / 86400.0,
                )
            if _durable_inbox is not None and now - last_inbox_sweep >= inbox_sweep_interval:
                cutoff = datetime.now(timezone.utc) - timedelta(seconds=inbox_max_pending_age)
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(_INBOX_EXECUTOR, _durable_inbox.reclaim_stale)
                purged = await loop.run_in_executor(
                    _INBOX_EXECUTOR,
                    _durable_inbox.ignore_pending_before,
                    cutoff,
                    "stale_pending_purge",
                )
                last_inbox_sweep = now
                if purged:
                    logger.warning(
                        "🧹 [Inbox-Sweep] Đã bỏ %s dòng 'pending' kẹt quá %.0f phút "
                        "(giftcode chắc chắn đã hết hạn)",
                        purged,
                        inbox_max_pending_age / 60.0,
                    )
        except asyncio.CancelledError:
            break
        except Exception:
            pass


async def _db_maintenance_loop():
    while bot_state.is_running:
        try:
            now = datetime.now()
            next_run = now.replace(hour=3, minute=0, second=0, microsecond=0)
            if next_run <= now:
                next_run += timedelta(days=1)
            await asyncio.sleep(max(1.0, (next_run - now).total_seconds()))
        except asyncio.CancelledError:
            break

        # ✅ FIX: mỗi bước chạy độc lập và LOG lỗi. Trước đây một
        # `except Exception: pass` duy nhất khiến vacuum lỗi làm bỏ qua luôn
        # bước purge inbox phía sau mà không có dấu vết nào trong log.
        loop = asyncio.get_running_loop()
        try:
            if _systems and _systems.get("db"):
                await loop.run_in_executor(_DB_EXECUTOR, _systems["db"].vacuum)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("❌ [DB-Maintenance] vacuum database chính lỗi")
        if _durable_inbox is not None:
            try:
                purged = await loop.run_in_executor(_INBOX_EXECUTOR, _durable_inbox.purge_completed, 7)
                if purged:
                    logger.info("🧹 [Inbox-Maintenance] Đã dọn %s row completed/ignored cũ (>7 ngày) cùng item con", purged)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("❌ [Inbox-Maintenance] purge_completed lỗi")
            try:
                stats = await loop.run_in_executor(_INBOX_EXECUTOR, _durable_inbox.maintenance)
                logger.info(
                    "🧹 [Inbox-Maintenance] checkpoint+VACUUM xong: %.1f KB → %.1f KB",
                    stats["before_bytes"] / 1024.0,
                    stats["after_bytes"] / 1024.0,
                )
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("❌ [Inbox-Maintenance] checkpoint/VACUUM inbox lỗi")


async def daily_reset_watchdog():
    while bot_state.is_running:
        try:
            await asyncio.sleep(60)
            _refresh_daily_state()
        except asyncio.CancelledError:
            break
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════
async def main():
    global _systems, BOT_START_TIME

    if client is None:
        raise RuntimeError(
            "Thiếu API_ID hoặc API_HASH. Hãy điền hai biến này trong file .env "
            "trước khi chạy bot."
        )

    try:
        logger.info("🚀 BOT v%s — BROWSER-ONLY", BOT_VERSION)
        logger.info("[Build] 2026-10-07-video-ocr-v1 | sites=%s | QQ88=%s | browser=existing-only",
                    sorted(Config.DOMAIN_TO_CHANNEL_URL), Config.QQ88_ENABLED)

        BOT_START_TIME = datetime.now(timezone.utc)

        logger.info(
            f"⏰ START: "
            f"{datetime.now().strftime('%H:%M:%S %d/%m/%Y')}"
        )
        logger.info(
            f"🆕 New-message-only mode | cutoff={BOT_START_TIME.isoformat()} | "
            f"catch_up={getattr(Config, 'TELEGRAM_CATCH_UP', False)} | "
            f"inbox_recovery={getattr(Config, 'TELEGRAM_INBOX_RECOVERY_MODE', 'at_least_once')}"
        )

        _systems = await init_systems()
        await asyncio.sleep(0.5)

        # Nạp RapidOCR trước khi nhận tin để ảnh đầu tiên không phải trả
        # thêm 1–3 giây khởi tạo model trong luồng xử lý Telegram.
        if getattr(Config, "MAX_CONCURRENT_OCR", 0) > 0:
            try:
                await asyncio.get_running_loop().run_in_executor(
                    _OCR_EXECUTOR,
                    warmup_image_extractor,
                )
                logger.info("✅ OCR warm-up model + ONNX inference hoàn tất")
            except Exception as exc:
                logger.warning("⚠️ OCR warm-up thất bại, sẽ thử lại khi có ảnh: %s", exc)

        logger.warning("🔥 client.start()...")
        for _attempt in range(1, 4):
            try:
                await client.start()
                break
            except asyncio.IncompleteReadError as e:
                if _attempt == 3:
                    raise
                logger.warning(
                    "⚠️ client.start() socket đọc dở (%s) — reset kết nối và thử lại %s/3",
                    e,
                    _attempt,
                )
                try:
                    await client.disconnect()
                except Exception:
                    pass
                await asyncio.sleep(3 * _attempt)
            except (asyncio.TimeoutError, ConnectionError, OSError, EOFError) as e:
                if _attempt == 3:
                    raise
                logger.warning("⚠️ client.start() lỗi (%s) — thử lại %s/3", e, _attempt)
                await asyncio.sleep(3 * _attempt)
        logger.warning("🔥 OK")
        # Signal handlers must wake ``run_until_disconnected()`` without
        # stopping the event loop, so main() can execute its async finally.
        get_shutdown_handler().set_stop_callback(client.disconnect)

        dialogs_synced = await sync_telegram_dialogs()
        if not dialogs_synced:
            logger.warning(
                "⚠️ Telegram dialog sync không hoàn tất; tiếp tục bằng "
                "verify_channels_and_get_ids() để kiểm tra quyền truy cập thực tế"
            )

        if not await verify_telegram_session():
            raise RuntimeError("Telegram session verification failed")

        disable_console_logging()
        start_dashboard()

        qm = init_queue_manager(
            soft_pct=float(
                getattr(
                    Config,
                    "QUEUE_SOFT_LIMIT_PCT",
                    0.75,
                )
            ),
            hard_pct=float(
                getattr(
                    Config,
                    "QUEUE_HARD_LIMIT_PCT",
                    0.95,
                )
            ),
            check_interval=float(
                getattr(
                    Config,
                    "QUEUE_CHECK_INTERVAL",
                    5.0,
                )
            ),
            cleanup_on_soft_limit=bool(
                getattr(
                    Config,
                    "QUEUE_CLEANUP_ON_SOFT_LIMIT",
                    False,
                )
            ),
            cleanup_target_pct=float(
                getattr(
                    Config,
                    "QUEUE_CLEANUP_TARGET_PCT",
                    0.50,
                )
            ),
        )

        await qm.start()

        logger.info(
            f"🧹 Queue manager ready "
            f"(soft={qm.soft_pct * 100:.0f}%, "
            f"hard={qm.hard_pct * 100:.0f}%, "
            f"interval={qm.check_interval:.0f}s)"
        )

        verified_channels = await verify_channels_and_get_ids()
        if len(verified_channels) != len(Config.CHANNEL_CONFIG):
            missing_channels = sorted(
                set(Config.CHANNEL_CONFIG) - set(verified_channels)
            )
            raise RuntimeError(
                "Configured Telegram channels are inaccessible: "
                + ", ".join(str(chat_id) for chat_id in missing_channels)
            )

        logger.warning("⭐ Init channels/accounts (browser-only)...")

        await init_channels_and_accounts()

        # Register ingress before the potentially slow browser preload. The
        # durable inbox and domain queues are already initialized, so messages
        # arriving during preload can be accepted and wait safely in queues.
        await setup_telegram_handler()

        # Preload every configured browser domain while already-accepted
        # messages wait safely in the durable/RAM queues.
        if _BROWSER_ENGINE_OK:
            browser_targets = [
                t for t in build_unique_account_targets()
                if t["domain"] in browser_engine.BROWSER_DOMAINS
            ]
            try:
                await browser_engine.preload_browsers_and_accounts(browser_targets)
            except Exception as e:
                logger.error(
                    f"❌ [Browser] preload lỗi ({e}) — submit sẽ được đánh dấu lỗi hạ tầng"
                )

        start_domain_workers()
        start_ocr_media_workers()
        register_default_commands()
        command_registry.register("clear", _clear_runtime_state, timeout=15.0)

        setup_admin_commands(
            client,
            admin_id=Config.TELEGRAM_ADMIN_ID,
            context_provider=lambda: {
                "bot_state": bot_state,
                "systems": _systems,
            },
        )

        if getattr(
            Config,
            "TELEGRAM_CATCH_UP",
            False,
        ):
            await client.catch_up()
        else:
            # Fill the startup gap between client connection and handler
            # registration. quick() still requires message.date >=
            # BOT_START_TIME, so this does not replay older history.
            logger.info("🔄 Startup catch_up có cutoff — chỉ nhận message sau thời điểm start")
            await client.catch_up()

        logger.info(
            f"✅ BOT READY! "
            f"{datetime.now().strftime('%H:%M:%S')}"
        )

        async def heartbeat():
            """
            Chỉ kiểm tra kết nối.
            Không tự disconnect/start ở đây để tránh heartbeat tranh
            quyền với run_until_disconnected().
            """
            while bot_state.is_running:
                try:
                    await asyncio.sleep(
                        float(
                            getattr(
                                Config,
                                "HEARTBEAT_INTERVAL",
                                300.0,
                            )
                        )
                    )

                    now_mono = time.monotonic()
                    raw_age = (
                        "-" if bot_state.last_raw_update_at is None
                        else f"{now_mono - bot_state.last_raw_update_at:.0f}s"
                    )
                    accepted_age = (
                        "-" if bot_state.last_accepted_message_at is None
                        else f"{now_mono - bot_state.last_accepted_message_at:.0f}s"
                    )
                    logger.info(
                        f"💓 {datetime.now().strftime('%H:%M:%S')} "
                        f"| connected={client.is_connected()} "
                        f"| raw_updates={bot_state.raw_update_count} "
                        f"(last={raw_age}) "
                        f"| accepted={bot_state.accepted_message_count} "
                        f"(last={accepted_age}) "
                        f"| loop_lag={bot_state.event_loop_lag_ms:.0f}ms "
                        f"| tasks={len(_active_submit_tasks)} "
                        f"| q={message_queue.qsize() if message_queue else 0}"
                    )

                    try:
                        me = await asyncio.wait_for(
                            client.get_me(),
                            timeout=float(
                                getattr(
                                    Config,
                                    "TELEGRAM_HEARTBEAT_TIMEOUT",
                                    8.0,
                                )
                            ),
                        )

                        if not me:
                            logger.warning(
                                "⚠️ [Heartbeat] Telegram session "
                                "không phản hồi"
                            )

                    except asyncio.CancelledError:
                        raise

                    except Exception as e:
                        logger.warning(
                            f"⚠️ [Heartbeat] Kiểm tra kết nối lỗi: {e}"
                        )

                except asyncio.CancelledError:
                    break

                except Exception as e:
                    logger.warning(
                        f"⚠️ [Heartbeat] Lỗi không mong muốn: {e}"
                    )

        async def event_loop_probe():
            """Detect a blocked asyncio loop independently of Telegram RPC."""
            expected = time.monotonic() + 1.0
            while bot_state.is_running:
                await asyncio.sleep(1.0)
                now = time.monotonic()
                lag_ms = max(0.0, (now - expected) * 1000.0)
                bot_state.event_loop_lag_ms = lag_ms
                if lag_ms >= 1000.0:
                    logger.warning(
                        "⚠️ [EventLoop] lag=%.0fms | raw_updates=%s | connected=%s",
                        lag_ms,
                        bot_state.raw_update_count,
                        client.is_connected(),
                    )
                expected = now + 1.0

        _bg = {
            asyncio.create_task(
                heartbeat(),
                name="hb",
            ),
            asyncio.create_task(
                event_loop_probe(),
                name="event-loop-probe",
            ),
            asyncio.create_task(
                _cleanup_scheduler(),
                name="cleanup",
            ),
            asyncio.create_task(
                daily_reset_watchdog(),
                name="daily",
            ),
            asyncio.create_task(
                _db_maintenance_loop(),
                name="db",
            ),
        }

        if _BROWSER_ENGINE_OK and getattr(Config, "USE_BROWSER_FOR_MULTI_SITE", True):
            _bg.add(asyncio.create_task(browser_engine.browser_watchdog(), name="browser-watchdog"))

        reconnect_attempts = 0
        max_reconnect_attempts = max(
            0,
            int(getattr(Config, "TELEGRAM_MAX_RECONNECT_ATTEMPTS", 5)),
        )
        reconnect_base_delay = max(
            0.5,
            float(getattr(Config, "TELEGRAM_RECONNECT_BASE_DELAY", 2.0)),
        )
        reconnect_max_delay = max(
            reconnect_base_delay,
            float(getattr(Config, "TELEGRAM_RECONNECT_MAX_DELAY", 60.0)),
        )
        reconnect_jitter = max(
            0.0,
            float(getattr(Config, "TELEGRAM_RECONNECT_JITTER", 1.0)),
        )
        stable_connection_seconds = max(
            0.0,
            float(getattr(Config, "TELEGRAM_STABLE_CONNECTION_SECONDS", 30.0)),
        )
        connect_timeout = max(
            1.0,
            float(getattr(Config, "TELEGRAM_CONNECT_TIMEOUT", 15.0)),
        )
        connected_since = time.monotonic() if client.is_connected() else None

        def reconnect_delay(attempt: int) -> float:
            exponential = min(
                reconnect_max_delay,
                reconnect_base_delay * (2 ** max(0, attempt - 1)),
            )
            return exponential + random.uniform(0.0, reconnect_jitter)

        while bot_state.is_running:
            try:
                if not client.is_connected():
                    reconnect_attempts += 1
                    if reconnect_attempts > max_reconnect_attempts:
                        logger.critical(
                            "🛑 [Telegram] Hết giới hạn reconnect (%s lần)",
                            max_reconnect_attempts,
                        )
                        bot_state.is_running = False
                        return

                    delay = reconnect_delay(reconnect_attempts)
                    logger.warning(
                        "🔄 [Telegram] Mất kết nối — thử lần %s/%s sau %.1fs",
                        reconnect_attempts,
                        max_reconnect_attempts,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    await asyncio.wait_for(
                        client.connect(),
                        timeout=connect_timeout,
                    )
                    connected_since = time.monotonic()

                await client.run_until_disconnected()

                if not bot_state.is_running:
                    break

                connection_age = (
                    time.monotonic() - connected_since
                    if connected_since is not None
                    else 0.0
                )
                if connection_age >= stable_connection_seconds:
                    reconnect_attempts = 0
                connected_since = None

            except asyncio.CancelledError:
                raise

            except AuthKeyDuplicatedError as e:
                logger.critical(
                    "🚨 [Telegram] AuthKeyDuplicated — dừng retry ngay: %s",
                    e,
                )
                bot_state.is_running = False
                try:
                    await client.disconnect()
                except Exception:
                    pass
                backup_corrupt_telegram_session()
                await send_auth_key_alert(e)
                raise SystemExit(78)

            except (asyncio.TimeoutError, ConnectionError, OSError) as e:
                reconnect_attempts += 1
                if reconnect_attempts > max_reconnect_attempts:
                    logger.critical(
                        "🛑 [Telegram] Hết giới hạn reconnect (%s lần): %s",
                        max_reconnect_attempts,
                        e,
                    )
                    bot_state.is_running = False
                    return

                delay = reconnect_delay(reconnect_attempts)
                logger.warning(
                    "⚠️ [Telegram] Lỗi kết nối: %s — thử lại sau %.1fs "
                    "(lần %s/%s)",
                    e,
                    delay,
                    reconnect_attempts,
                    max_reconnect_attempts,
                )
                await asyncio.sleep(delay)

            except Exception as e:
                logger.exception(
                    "❌ [Telegram] Lỗi không mong muốn — dừng bot, không retry: %s",
                    e,
                )
                bot_state.is_running = False
                return

    except AuthKeyDuplicatedError as e:
        logger.critical(
            "🚨 [Telegram] AuthKeyDuplicated — dừng bot, không retry: %s",
            e,
        )
        bot_state.is_running = False
        try:
            await client.disconnect()
        except Exception:
            pass
        backup_corrupt_telegram_session()
        await send_auth_key_alert(e)
        # Giữ mã lỗi sau khi finally hoàn tất để run.bat/supervisor biết
        # đây là lỗi session cần xử lý thủ công, không phải shutdown bình thường.
        raise SystemExit(78)

    except Exception as e:
        logger.critical(f"❌ Critical: {e}\n{traceback.format_exc()}")
        raise SystemExit(1)
    finally:
        logger.info("\n🛑 Shutting down...")
        bot_state.is_running = False
        # Ngừng nhận tin mới, rồi ghi nốt các tin đã nhận vào hộp thư bền;
        # nếu không, tắt máy giữa đợt dồn tin có thể làm tin chỉ còn trong RAM.
        global _channel_poll_task, _ingress_stage_task
        if _channel_poll_task is not None:
            _channel_poll_task.cancel()
            await asyncio.gather(_channel_poll_task, return_exceptions=True)
            _channel_poll_task = None
        if _ingress_stage is not None:
            try:
                await _ingress_stage.drain(timeout=5.0)
            except Exception:
                pass
        if _ingress_stage_task is not None:
            _ingress_stage_task.cancel()
            await asyncio.gather(_ingress_stage_task, return_exceptions=True)
            _ingress_stage_task = None

        # Workers are allowed to finish rows already in RAM.  This also lets
        # them create their final domain submissions before we wait for the
        # active submit set below.  Rows that do not finish before the bound
        # remain durable and are requeued by the worker cancellation handler.
        if message_queue is not None:
            try:
                await asyncio.wait_for(
                    message_queue.join(),
                    timeout=float(getattr(Config, "SHUTDOWN_MESSAGE_DRAIN_TIMEOUT", 8.0)),
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "⚠️ Shutdown: message queue chưa drain hết (q=%s); "
                    "các row còn lại sẽ được giữ trong DurableInbox",
                    message_queue.qsize(),
                )
            except Exception as exc:
                logger.warning("⚠️ Shutdown: lỗi drain message queue: %s", exc)

        # Message workers may have handed media jobs to the dedicated OCR queue.
        # Drain it before stopping OCR workers so accepted media is not left in
        # processing state unnecessarily during a graceful shutdown.
        if _ocr_media_queue is not None:
            try:
                await asyncio.wait_for(
                    _ocr_media_queue.join(),
                    timeout=float(getattr(Config, "SHUTDOWN_MESSAGE_DRAIN_TIMEOUT", 8.0)),
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "⚠️ Shutdown: OCR/media queue chưa drain hết (q=%s)",
                    _ocr_media_queue.qsize(),
                )
            except Exception as exc:
                logger.warning("⚠️ Shutdown: lỗi drain OCR/media queue: %s", exc)

        if _active_submit_tasks:
            active_submit_drain_timeout = min(
                120.0,
                max(
                    8.0,
                    float(getattr(Config, "SUBMIT_TIMEOUT_SECONDS", 45.0))
                    * max(1, int(getattr(Config, "MAX_RETRIES_PER_ACCOUNT", 1)))
                    + 5.0,
                ),
            )
            try:
                await asyncio.wait_for(
                    asyncio.gather(*list(_active_submit_tasks), return_exceptions=True),
                    timeout=active_submit_drain_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "⚠️ Shutdown: submit/domain item chưa drain sau %.1fs; sẽ hủy task còn lại",
                    active_submit_drain_timeout,
                )
                for t in list(_active_submit_tasks):
                    t.cancel()

        # Drain history only after ingress, message workers, and active
        # submits have finished. The previous ordering joined this queue
        # before those producers stopped, so their final RESULT rows could be
        # added after the join and then lost when the writer was cancelled.
        if _history_queue is not None:
            try:
                await asyncio.wait_for(_history_queue.join(), timeout=5.0)
            except Exception:
                pass
            if _history_writer_task:
                _history_writer_task.cancel()
        for w in message_workers:
            w.cancel()
        if message_workers:
            await asyncio.gather(*message_workers, return_exceptions=True)
        for w in _ocr_media_workers:
            w.cancel()
        if _ocr_media_workers:
            await asyncio.gather(*_ocr_media_workers, return_exceptions=True)
        global _inbox_drain_task
        if _inbox_drain_task is not None:
            _inbox_drain_task.cancel()
            await asyncio.gather(_inbox_drain_task, return_exceptions=True)
            _inbox_drain_task = None
        domain_worker_tasks = [t for ws in _domain_workers.values() for t in ws]
        for t in domain_worker_tasks:
            t.cancel()
        if domain_worker_tasks:
            await asyncio.gather(*domain_worker_tasks, return_exceptions=True)
        try:
            qm = get_queue_manager()
            if qm is not None:
                await qm.stop()
        except Exception:
            pass

        # Hủy các task nền trước khi tạo báo cáo tổng kết
        background_tasks = locals().get("_bg", set())

        for task in list(background_tasks):
            if not task.done():
                task.cancel()

        if background_tasks:
            await asyncio.gather(
                *background_tasks,
                return_exceptions=True,
            )

        try:
            shutdown_ocr_executor(wait=True, cancel_futures=True)
        except Exception as e:
            logger.debug(f"⚠️ [OCR] shutdown executor lỗi (bỏ qua): {e}")

        if _BROWSER_ENGINE_OK:
            browser_engine.shutdown()
            try:
                await browser_engine.cleanup_browsers()
            except Exception as e:
                logger.debug(f"⚠️ [Browser] cleanup lỗi (bỏ qua): {e}")

        global _durable_inbox
        if _durable_inbox is not None:
            try:
                _durable_inbox.close()
            except Exception:
                pass
            _durable_inbox = None

        # Đóng kết nối SQLite và các executor do ứng dụng sở hữu sau khi mọi
        # task đã dừng. Nếu bỏ qua, Python có thể phải chờ các worker thread
        # ở lúc thoát và lần chạy kế tiếp dễ gặp database locked trên Windows.
        if _systems and _systems.get("db"):
            try:
                _systems["db"].close()
            except Exception as exc:
                logger.debug("⚠️ [DB] cleanup lỗi (bỏ qua): %s", exc)
        for executor_name, executor in (
            ("media", _MEDIA_EXECUTOR),
            ("db", _DB_EXECUTOR),
            ("inbox-ingress", _INBOX_INGRESS_EXECUTOR),
            ("inbox-state", _INBOX_STATE_EXECUTOR),
        ):
            try:
                executor.shutdown(wait=True, cancel_futures=True)
            except Exception as exc:
                logger.debug("⚠️ [%s executor] cleanup lỗi (bỏ qua): %s", executor_name, exc)

        build_daily_summary()
        try:
            stop_monitoring()
        except Exception as e:
            logger.debug(f"⚠️ [Health] shutdown monitor lỗi (bỏ qua): {e}")
        stop_dashboard()
        get_shutdown_handler().notify_cleanup_done()
        logger.info("✅ Done")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Stopped")
