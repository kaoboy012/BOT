#!/usr/bin/env python3
"""
Logger setup cho Bot v7.5 - Daily rotating logs (compressed) + Clean Console

- Ghi log vào LOG_FILE (mặc định logs/bot_activity.log)
- Xoay file mỗi nửa đêm (TimedRotatingFileHandler)
- Nén file rotate thành .gz sau khi xoay
- Giữ file rotate theo LOG_ROTATION_BACKUP_COUNT / LOG_BACKUP_DAYS
- Console: lọc rác, có màu (tùy terminal)
- Bật DEBUG_VERBOSE_MODE=true trong .env để show DEBUG trên console
"""

import logging
import logging.handlers
import os
import re
import sys
import gzip
import shutil
import unicodedata
import threading
import time
from collections import OrderedDict
from contextvars import ContextVar
from dotenv import load_dotenv
from pathlib import Path

load_dotenv()

# ============================================================
# NHÃN NGỮ CẢNH (kênh/domain) CHO MỖI DÒNG LOG
# ============================================================
# set_log_context(tên) gắn nhãn kênh/domain cho mọi logger.info/warning gọi
# bên trong ngữ cảnh (kể cả task nền tạo từ asyncio.create_task()) — giúp
# tách log của nhiều kênh chạy song song mà không cần sửa từng câu log.
_log_context: ContextVar[str] = ContextVar("log_context", default="SYSTEM")


def set_log_context(tag: str):
    """Gắn nhãn kênh/domain cho log trong ngữ cảnh async hiện tại. Trả về
    token — dùng reset_log_context(token) khi xử lý xong."""
    return _log_context.set(tag or "SYSTEM")


def reset_log_context(token) -> None:
    try:
        _log_context.reset(token)
    except Exception:
        pass


class ContextTagFilter(logging.Filter):
    """Gắn record.context_tag = tên kênh/domain hiện tại (hoặc 'SYSTEM' nếu
    log không thuộc luồng xử lý kênh nào, vd watchdog nền)."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.context_tag = _log_context.get()
        return True


def _slugify_for_filename(text: str) -> str:
    """Chuyển tên kênh/domain (có dấu tiếng Việt) thành tên file an toàn,
    dễ đọc — vd 'XX88 SĂN CODE MỖI NGÀY' → 'XX88_SAN_CODE_MOI_NGAY'."""
    text = (text or "").replace("Đ", "D").replace("đ", "d")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-zA-Z0-9_.-]+", "_", text).strip("_")
    return text or "misc"


class PerChannelFileHandler(logging.Handler):
    """Ghi THÊM 1 bản sao của mỗi log record vào file logs/channels/<tên
    kênh/domain>.log riêng, dựa theo context_tag hiện tại — giúp mở đúng 1
    file để xem TOÀN BỘ log của riêng 1 kênh/domain, không phải lọc giữa
    hàng nghìn dòng của kênh khác. File chính (bot_activity.log) vẫn ghi
    ĐẦY ĐỦ mọi log theo đúng thứ tự thời gian như trước — đây chỉ là bản
    sao có chọn lọc, không thay thế file chính.
    Log không gắn nhãn kênh nào (context_tag='SYSTEM', vd watchdog nền,
    heartbeat...) bị BỎ QUA ở đây — tránh sinh ra 1 file 'SYSTEM.log' khổng
    lồ không có giá trị tra cứu theo kênh; các log này vẫn có đủ trong
    bot_activity.log như bình thường."""

    def __init__(self, base_dir: str = "logs/channels", encoding: str = "utf-8", max_handlers: int = 64):
        super().__init__()
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.encoding = encoding
        self._handlers: OrderedDict[str, logging.FileHandler] = OrderedDict()
        self._max_handlers = max(1, int(max_handlers))
        self._formatter = logging.Formatter(
            "[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%d/%m/%Y %H:%M:%S"
        )

    def _get_file_handler(self, tag: str) -> logging.FileHandler:
        safe_tag = _slugify_for_filename(tag)
        handler = self._handlers.get(safe_tag)
        if handler is not None:
            self._handlers.move_to_end(safe_tag)
            return handler
        handler = logging.FileHandler(
            self.base_dir / f"{safe_tag}.log", encoding=self.encoding
        )
        handler.setFormatter(self._formatter)
        self._handlers[safe_tag] = handler
        while len(self._handlers) > self._max_handlers:
            _, old_handler = self._handlers.popitem(last=False)
            try:
                old_handler.close()
            except Exception:
                pass
        return handler

    def emit(self, record: logging.LogRecord) -> None:
        tag = getattr(record, "context_tag", "SYSTEM")
        if not tag or tag == "SYSTEM":
            return
        try:
            self._get_file_handler(tag).emit(record)
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        for handler in self._handlers.values():
            try:
                handler.close()
            except Exception:
                pass
        super().close()


# Ép stdout/stderr dùng UTF-8 — trên Windows, Python có thể vẫn dùng bảng
# mã cũ (cp1258/cp437) dù CMD đã chạy "chcp 65001", khiến tiếng Việt lỗi "?".
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Env helpers
def _get_int_env(key: str, default: int) -> int:
    try:
        v = os.getenv(key)
        return int(v) if v is not None and str(v).strip() != "" else default
    except Exception:
        return default

def _get_bool_env(key: str, default: bool) -> bool:
    v = os.getenv(key)
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip().lower() in ("true", "1", "yes", "y", "on")

# Options from .env
DEBUG_VERBOSE_MODE = os.getenv("DEBUG_VERBOSE_MODE", "false").lower() == "true"
LOG_FILE = os.getenv("LOG_FILE", "logs/bot_activity.log")
LOG_DIR = os.getenv("LOG_DIR", "logs")
LOG_ROTATION_BACKUP_COUNT = _get_int_env("LOG_ROTATION_BACKUP_COUNT", _get_int_env("LOG_BACKUP_DAYS", 7))
LOG_USE_UTC = _get_bool_env("LOG_USE_UTC", False)
CONSOLE_LOG_LEVEL = os.getenv("CONSOLE_LOG_LEVEL", "INFO").upper()
COMPACT_CONSOLE_LOG = _get_bool_env("COMPACT_CONSOLE_LOG", True)
FILE_LOG_LEVEL = os.getenv("FILE_LOG_LEVEL", "INFO").upper()
LOG_ROTATION_MAX_BYTES = _get_int_env("LOG_ROTATION_MAX_BYTES", 10 * 1024 * 1024)
LOG_ROTATION_BACKUP_COUNT_SIZE = _get_int_env("LOG_ROTATION_BACKUP_COUNT", 5)
CONSOLE_COLOR = _get_bool_env("CONSOLE_COLOR", True)
CONSOLE_SUMMARY_ONLY = _get_bool_env("CONSOLE_SUMMARY_ONLY", True)


def _enable_windows_ansi() -> None:
    """Bật hỗ trợ mã màu ANSI trên Windows cmd.exe (mặc định cmd.exe cũ không
    hiểu \\033[...m, chỉ in ra ký tự rác). Không cần cài thêm gì (không dùng
    colorama) — chỉ bật cờ ENABLE_VIRTUAL_TERMINAL_PROCESSING qua WinAPI.
    An toàn: nếu fail (Windows quá cũ, không phải cmd thật...) thì bỏ qua,
    tối đa là mất màu chứ không crash bot.
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        STD_OUTPUT_HANDLE = -11
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
    except Exception:
        pass

# Những chuỗi cần lọc trên console để tránh spam — vẫn ghi đầy đủ vào file
# log (logs/bot_activity.log), chỉ ẩn khỏi CONSOLE.
CONSOLE_SKIP_PHRASES = [
    "Error processing line",
    "Remainder of file ignored",
    "protobuf",
    "NoneType",
    "loader",
    "MESSAGE RAW",
    "text_len=",
    "Stealth JS loaded",
    "add_init_script",
    "Cannot setup route",
    "TabPool:",
    "tab riêng (persistent)",
    "Chat ",
    " not in config",
    "Cleanup done",
    "Cleanup error",
    "browser_minimize error",
    "history_writer",
    "Cannot write code history",
    "Cannot enqueue",
    "History queue",
    "⚠️ auto_solve error",
    "⚠️ Error finding input",
    "popup không mong đợi",
    "Đóng 0 popup",
    "TASK CANCELLED",
    "⏭️ Chat",
    "nspkg",
    # ── Log kỹ thuật nội bộ, không cần hiện trên console ──────────────────
    "[HANDLER]",
    "[HANDLER-EDIT]",
    "🔔",
    "Browser-Watchdog] Tất cả tab OK",
    "Telegram session OK",
    "Đã kết nối — dùng context hiện có",
    "🗂️ Tab-",
    "Worker #",
    "Edge restored",
    "Scrolled to input fields",
    "Clicked domain-specific button",
    # ── Khởi động/telemetry lặp, không cần hiện trong chế độ gọn ──────────
    "✅ [Telegram channel]",
    "[Telegram accepted]",
    "[Domain-Worker#",
    "[Startup]",
    "[TabPool-Memory]",
    "[Heartbeat]",
    "[EXTRACT]",
    "[SUBMIT_STAGE]",
    "[HI88-LINK]",
    "[SPOILER]",
    "[PLAIN-TEXT]",
    "[EXTRACT-MERGED]",
    "[MARKER]",
    "[Browser] SUBMIT",
    "────────────────",
]

class CleanConsoleFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        for phrase in CONSOLE_SKIP_PHRASES:
            if phrase in msg:
                return False
        return True


class ConsoleSummaryFilter(logging.Filter):
    """Keep the interactive console readable without reducing file detail.

    INFO logs are reduced to the small set of lifecycle/result signals an
    operator needs. WARNING/ERROR/CRITICAL always pass through. The file and
    per-channel handlers do not use this filter, so troubleshooting detail is
    still retained in logs/bot_activity.log and logs/channels/.
    """

    INFO_SIGNALS = (
        "🤖 BOT RUNNING",
        "🗓️ Ngày mới",
        "✅ Đã nhận code",
        "📥 Đã xếp hàng",
        "SUCCESS",
        "FAILED",
        "NO_RESULT",
        "PENDING_VERIFICATION",
        "SITE_UI_CHANGED",
        "INFRA_FAILURE",
        "ACCOUNT_BLOCKED",
        "RATE_LIMIT",
        "Cloudflare=passed",
        "Cloudflare=pending",
        "đã bấm nút Xác thực",
        "Cloudflare đã xác minh từ trước",
        "✅ Session:",
        "[Edge-CDP] ✅",
        "[TabPool] Đã gán",
        "domain workers",
        "[Inbox] recovery_mode=",
        "TẮT BOT AN TOÀN",
        "TAT BOT AN TOAN",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        if not CONSOLE_SUMMARY_ONLY or record.levelno >= logging.WARNING:
            return True
        try:
            message = record.getMessage()
        except Exception:
            return True
        return any(signal in message for signal in self.INFO_SIGNALS)


class ConsoleRepeatFilter(logging.Filter):
    """Collapse identical transient warnings on the interactive console."""

    WINDOW_SECONDS = 5.0

    def __init__(self) -> None:
        super().__init__()
        self._last_seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno != logging.WARNING:
            return True
        try:
            message = record.getMessage()
        except Exception:
            return True
        if not any(marker in message for marker in ("Turnstile", "PENDING_VERIFICATION")):
            return True
        now = time.monotonic()
        with self._lock:
            previous = self._last_seen.get(message, 0.0)
            self._last_seen[message] = now
            # Bound memory when a long-running bot sees many distinct domains.
            if len(self._last_seen) > 256:
                cutoff = now - self.WINDOW_SECONDS
                self._last_seen = {
                    key: value for key, value in self._last_seen.items() if value >= cutoff
                }
        return now - previous >= self.WINDOW_SECONDS

class ColoredFormatter(logging.Formatter):
    """✅ FIX: trước đây TÔ MÀU NGUYÊN DÒNG cho MỌI log theo level (DEBUG=xám,
    INFO=xanh lá, WARNING=vàng...) — khiến console nhìn lòe loẹt như "ổ điện"
    dù phần lớn chỉ là log thông thường, không có gì đáng chú ý. Giờ:
      - INFO bình thường  → KHÔNG tô màu (giữ màu mặc định terminal)
      - WARNING/ERROR/CRITICAL → vẫn tô màu (đúng ý nghĩa: cần chú ý)
      - Log "quan trọng" (SUCCESS/FAILED/rate-limit) → luôn nổi bật nhất,
        bất kể level, vì đây là KẾT QUẢ XỬ LÝ thật sự người dùng cần thấy.
    """

    LEVEL_COLORS = {
        'DEBUG':    '\033[90m',        # xám
        'INFO':     '\033[32m',        # xanh lá
        'WARNING':  '\033[33m',        # vàng
        'ERROR':    '\033[31m',        # đỏ
        'CRITICAL': '\033[1;97;41m',   # trắng đậm, nền đỏ
    }
    RESET = '\033[0m'
    ICONS = {
        'DEBUG':    '·',
        'INFO':     '✓',
        'WARNING':  '!',
        'ERROR':    '✗',
        'CRITICAL': '!!',
    }

    # Log "quan trọng" — ưu tiên cao hơn màu theo level, kiểm tra theo thứ tự.
    # Khớp đúng các chuỗi bot đang log thật (SUCCESS/FAILED) ở main_script.py.
    IMPORTANT_STYLES = [
        ("SUCCESS", '\033[1;30;42m'),          # đen đậm, nền xanh lá — trúng code
        ("FAILED", '\033[1;97;41m'),           # trắng đậm, nền đỏ — code sai/hết hạn
        ("Too Many Requests", '\033[1;30;43m'),  # đen đậm, nền vàng — bị rate limit
    ]

    def format(self, record: logging.LogRecord) -> str:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)

        # Mặc định: INFO/DEBUG không tô màu (đỡ rối mắt) — chỉ WARNING trở
        # lên mới có màu theo level như trước.
        if record.levelname in ("WARNING", "ERROR", "CRITICAL"):
            color = self.LEVEL_COLORS.get(record.levelname, "")
        else:
            color = ""

        # Log "quan trọng" (kết quả submit) LUÔN được tô nổi bật, bất kể level.
        for keyword, style in self.IMPORTANT_STYLES:
            if keyword in msg:
                color = style
                break

        icon = self.ICONS.get(record.levelname, ' ')
        rec = logging.makeLogRecord(record.__dict__)
        rec.levelname = icon
        line = super().format(rec)

        if not CONSOLE_COLOR or not color:
            return line
        return f"{color}{line}{self.RESET}"

def _ensure_log_dir(path: str):
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

def _timed_rotator(source: str, dest: str):
    """
    Rotator that compresses rotated file into the already-named destination
    and removes the original.
    Designed to be assigned to handler.rotator.
    """
    try:
        with open(source, "rb") as f_in:
            with gzip.open(dest, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
        try:
            os.remove(source)
        except Exception:
            pass
    except Exception:
        # Avoid raising within rotator to not break logging flow
        logging.getLogger("bot_logger").exception("Error compressing log %s -> %s", source, dest)

def _timed_namer(default_name: str) -> str:
    return default_name + ".gz"

def setup_logger() -> logging.Logger:
    _ensure_log_dir(LOG_DIR)
    log_parent = str(Path(LOG_FILE).expanduser().parent)
    if log_parent not in ("", "."):
        _ensure_log_dir(log_parent)

    if CONSOLE_COLOR:
        _enable_windows_ansi()

    logger = logging.getLogger("bot_logger")
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    # Console handler
    console_handler = logging.StreamHandler()
    try:
        console_level = getattr(logging, CONSOLE_LOG_LEVEL, logging.INFO)
    except Exception:
        console_level = logging.INFO

    if DEBUG_VERBOSE_MODE:
        console_handler.setLevel(logging.DEBUG)
        # Verbose mode vẫn giữ màu (full-line + highlight SUCCESS/FAILED) —
        # chỉ khác là không lọc bớt log rác qua CleanConsoleFilter.
        console_handler.setFormatter(ColoredFormatter('[%(asctime)s] [%(levelname)s] [%(context_tag)-18.18s] %(message)s', datefmt='%H:%M:%S'))
        # notify user that verbose mode is enabled (only once)
        logger.warning("🔊 DEBUG_VERBOSE_MODE=true — console filter TẮT, hiện mọi log")
    else:
        console_handler.setLevel(console_level)
        console_handler.addFilter(CleanConsoleFilter())
        console_handler.addFilter(ConsoleSummaryFilter())
        console_handler.addFilter(ConsoleRepeatFilter())
        if COMPACT_CONSOLE_LOG:
            console_handler.setFormatter(ColoredFormatter('%(levelname)s %(message)s'))
        else:
            console_handler.setFormatter(ColoredFormatter('[%(asctime)s] %(levelname)s [%(context_tag)-18.18s] %(message)s', datefmt='%H:%M:%S'))

    console_handler.addFilter(ContextTagFilter())
    logger.addHandler(console_handler)

    # File handler: try TimedRotatingFileHandler (daily), fallback to RotatingFileHandler (size)
    try:
        from logging.handlers import TimedRotatingFileHandler
        file_handler = TimedRotatingFileHandler(
            filename=LOG_FILE,
            when="midnight",
            interval=1,
            backupCount=LOG_ROTATION_BACKUP_COUNT,
            encoding="utf-8",
            utc=LOG_USE_UTC,
        )
        file_handler.setLevel(getattr(logging, FILE_LOG_LEVEL, logging.INFO))
        # ✅ MỚI: thêm cột [context_tag] cố định 22 ký tự — nhìn phát biết
        # ngay dòng log thuộc kênh/domain nào mà không cần đọc hết nội dung.
        file_handler.setFormatter(logging.Formatter('[%(asctime)s] [%(levelname)-8s] [%(context_tag)-22.22s] %(message)s', datefmt='%d/%m/%Y %H:%M:%S'))
        file_handler.addFilter(ContextTagFilter())
        # add suffix to include date in rotated filename
        try:
            file_handler.suffix = "%Y-%m-%d"
        except Exception:
            pass

        # set rotator/namer to compress rotated files
        file_handler.rotator = _timed_rotator
        file_handler.namer = _timed_namer

        logger.addHandler(file_handler)
    except Exception:
        # Fallback to size-based rotating file handler
        rf = logging.handlers.RotatingFileHandler(
            LOG_FILE,
            maxBytes=LOG_ROTATION_MAX_BYTES,
            backupCount=LOG_ROTATION_BACKUP_COUNT_SIZE,
            encoding="utf-8",
        )
        rf.setLevel(getattr(logging, FILE_LOG_LEVEL, logging.INFO))
        rf.setFormatter(logging.Formatter('[%(asctime)s] [%(levelname)-8s] [%(context_tag)-22.22s] %(message)s', datefmt='%d/%m/%Y %H:%M:%S'))
        rf.addFilter(ContextTagFilter())
        logger.addHandler(rf)
        logger.warning("TimedRotatingFileHandler không khả dụng — chuyển sang RotatingFileHandler kích thước")

    # ✅ MỚI: mỗi kênh/domain có thêm 1 file log RIÊNG trong logs/channels/
    # — vd logs/channels/xx88code_com.log chỉ chứa log của XX88, không lẫn
    # kênh khác. Chỉ nhận log INFO trở lên (bỏ DEBUG cho gọn, dễ đọc) và
    # dùng chung CleanConsoleFilter để lọc bớt log kỹ thuật nội bộ.
    try:
        channel_handler = PerChannelFileHandler(base_dir=os.path.join(LOG_DIR, "channels"))
        channel_handler.setLevel(logging.INFO)
        channel_handler.addFilter(ContextTagFilter())
        channel_handler.addFilter(CleanConsoleFilter())
        logger.addHandler(channel_handler)
    except Exception as e:
        logger.warning(f"⚠️ Không khởi tạo được log riêng theo kênh: {e}")

    # Optional: reduce verbosity of noisy libraries unless DEBUG_VERBOSE_MODE
    if not DEBUG_VERBOSE_MODE:
        try:
            logging.getLogger("telethon").setLevel(logging.WARNING)
            logging.getLogger("asyncio").setLevel(logging.WARNING)
            logging.getLogger("urllib3").setLevel(logging.WARNING)
            logging.getLogger("playwright").setLevel(logging.WARNING)
        except Exception:
            pass

    # Telethon tự ngủ rồi gửi lại khi bị FloodWait (<= flood_sleep_threshold) và chỉ ghi
    # dòng đó ở mức INFO, nên với telethon=WARNING ở trên log hoàn toàn không thấy:
    # request chậm 4-5s trông như "timeout" mà không rõ nguyên nhân. Chuyển riêng các
    # dòng "flood wait" vào log chính để chẩn đoán được.
    try:
        class _TelethonFloodForwarder(logging.Handler):
            def emit(self, record):
                try:
                    msg = record.getMessage()
                    if "flood wait" in msg.lower():
                        logger.warning("⏳ [Telethon] %s", msg)
                except Exception:
                    pass

        _flood_log = logging.getLogger("telethon.client.users")
        _flood_log.setLevel(logging.INFO)
        _flood_log.addHandler(_TelethonFloodForwarder(level=logging.INFO))
    except Exception:
        pass

    logger.propagate = False
    return logger

# Create global logger
logger = setup_logger()
# Example: logger.debug("Logger initialized")
