"""
⚙️ CẤU HÌNH BOT — BROWSER-ONLY
Browser là route duy nhất; không gửi code qua HTTP API.
"""

import logging
import os
from dotenv import load_dotenv
from urllib.parse import urlparse

load_dotenv()


def get_int_env(key: str, default: int) -> int:
    value = os.getenv(key)
    if value is None or str(value).strip() == "":
        return default
    try:
        return int(value)
    except Exception:
        return default


def get_float_env(key: str, default: float) -> float:
    value = os.getenv(key)
    if value is None or str(value).strip() == "":
        return default
    try:
        return float(value)
    except Exception:
        return default


def get_bool_env(key: str, default: bool) -> bool:
    value = os.getenv(key)
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in ("true", "1", "yes", "y", "on")


def get_int_list_env(key: str, default: tuple[int, ...]) -> tuple[int, ...]:
    """Read comma-separated Telegram chat IDs, ignoring malformed values."""
    value = os.getenv(key)
    if value is None or not str(value).strip():
        return tuple(default)
    result = []
    for item in str(value).split(","):
        try:
            result.append(int(item.strip()))
        except (TypeError, ValueError):
            continue
    return tuple(dict.fromkeys(result)) or tuple(default)


class Config:
    # QQ88 is paused by default; change QQ88_ENABLED=true and restart to resume.
    QQ88_ENABLED = get_bool_env("QQ88_ENABLED", False)

    # ==========================================
    # 🔐 TELEGRAM
    # ==========================================
    API_ID       = get_int_env("API_ID", 0)
    API_HASH     = os.getenv("API_HASH", "")
    SESSION_NAME = os.getenv("SESSION_NAME", "session_bot")

    # Bot API độc lập để cảnh báo khi session Telethon mất kết nối.
    # Không dùng chung với SESSION_NAME hoặc client Telethon chính.
    ALERT_BOT_TOKEN = os.getenv("ALERT_BOT_TOKEN", "").strip()
    ALERT_CHAT_ID = get_int_env(
        "ALERT_CHAT_ID",
        get_int_env("TELEGRAM_ADMIN_ID", 0),
    )
    ALERT_HTTP_TIMEOUT = get_float_env("ALERT_HTTP_TIMEOUT", 10.0)
    SUBMIT_SUCCESS_NOTIFY = get_bool_env("SUBMIT_SUCCESS_NOTIFY", True)
    SUBMIT_SUCCESS_CHAT_ID = get_int_env(
        "SUBMIT_SUCCESS_CHAT_ID",
        ALERT_CHAT_ID,
    )

    # Session recovery: backup first; deletion is opt-in and disabled by default.
    SESSION_AUTO_BACKUP = get_bool_env("SESSION_AUTO_BACKUP", True)
    SESSION_DELETE_AFTER_BACKUP = get_bool_env(
        "SESSION_DELETE_AFTER_BACKUP",
        False,
    )
    SESSION_BACKUP_DIR = os.getenv(
        "SESSION_BACKUP_DIR",
        "backups/sessions",
    ).strip()
    TELEGRAM_AUTO_RECONNECT = get_bool_env(
        "TELEGRAM_AUTO_RECONNECT",
        True,
    )
    TELEGRAM_CONNECTION_RETRIES = get_int_env(
        "TELEGRAM_CONNECTION_RETRIES",
        5,
    )
    TELEGRAM_MAX_RECONNECT_ATTEMPTS = get_int_env(
        "TELEGRAM_MAX_RECONNECT_ATTEMPTS",
        5,
    )
    # Kept as a compatibility key; Telethon does not expose a generic
    # per-request timeout through TelegramClient(timeout=...).
    TELEGRAM_REQUEST_TIMEOUT = get_float_env(
        "TELEGRAM_REQUEST_TIMEOUT",
        20.0,
    )
    TELEGRAM_CONNECT_TIMEOUT = get_float_env(
        "TELEGRAM_CONNECT_TIMEOUT",
        TELEGRAM_REQUEST_TIMEOUT,
    )
    TELEGRAM_HEARTBEAT_TIMEOUT = get_float_env(
        "TELEGRAM_HEARTBEAT_TIMEOUT",
        8.0,
    )
    TELEGRAM_CHANNEL_TIMEOUT = get_float_env(
        "TELEGRAM_CHANNEL_TIMEOUT",
        10.0,
    )
    # Nạp toàn bộ dialog để Telethon cache entity/access_hash và nhận update
    # realtime ổn định kể cả với channel ít hoạt động.
    TELEGRAM_SYNC_DIALOGS = get_bool_env("TELEGRAM_SYNC_DIALOGS", True)
    TELEGRAM_DIALOG_SYNC_TIMEOUT = get_float_env(
        "TELEGRAM_DIALOG_SYNC_TIMEOUT",
        120.0,
    )
    TELEGRAM_RECONNECT_BASE_DELAY = get_float_env(
        "TELEGRAM_RECONNECT_BASE_DELAY",
        2.0,
    )
    TELEGRAM_RECONNECT_MAX_DELAY = get_float_env(
        "TELEGRAM_RECONNECT_MAX_DELAY",
        60.0,
    )
    TELEGRAM_RECONNECT_JITTER = get_float_env(
        "TELEGRAM_RECONNECT_JITTER",
        1.0,
    )
    TELEGRAM_STABLE_CONNECTION_SECONDS = get_float_env(
        "TELEGRAM_STABLE_CONNECTION_SECONDS",
        30.0,
    )

    # ==========================================
    # 📝 LOG / DATABASE
    # ==========================================
    LOG_LEVEL    = os.getenv("LOG_LEVEL", "INFO")
    LOG_FILE     = os.getenv("LOG_FILE", "logs/bot_activity.log")
    MAX_LOG_SIZE = get_int_env("MAX_LOG_SIZE", 10485760)
    BACKUP_COUNT = get_int_env("BACKUP_COUNT", 5)

    DATABASE_PATH = os.getenv("DATABASE_PATH", "data/code_history.db")

    # ==========================================
    # 🔎 CODE FILTER
    # ==========================================
    CODE_MIN_LENGTH = 6
    CODE_MAX_LENGTH = 15

    SPECIAL_CODE_CHARS_30 = r"""~!@#$%^&*()_+{}|:\"<>?`=[]\\;',./»«"""

    CODE_FILTER_GROUPS = {
        "multi_site_strict": {
            "description": "Dùng chung cho XX88, RR88, GG88",
            "url_keywords": ["xx88", "rr88", "gg88"],
            "allowed_sites": ["xx88", "rr88", "gg88"],
            "allow_numeric": False,
            "allow_random_mix": False,
            "require_uppercase": True,
            "force_uppercase": True,
            "remove_spaces": True,
            "prefer_spoiler": True,
            "marker_scan_lines": 3,
            "allow_fallback": False,
            "special_chars_group": "SPECIAL_CODE_CHARS_30",
            "special_chars": SPECIAL_CODE_CHARS_30,
            "min_special_chars": 0,
            "min_entropy": 2.0,
            "uppercase_min_entropy": 2.5,
            "soft_blacklist": ["CODE", "GAME", "FREE", "VIP", "NAP", "RUT"],
            "min_clean_length": 6,
            "max_clean_length": 8,
        },
        "mm88": {
            "description": "Nhóm riêng cho MM88",
            "url_keywords": ["mm88"],
            "allowed_sites": ["mm88"],
            "allow_numeric": False,
            "allow_random_mix": False,
            "require_uppercase": True,
            "force_uppercase": True,
            "remove_spaces": True,
            "prefer_spoiler": True,
            "marker_scan_lines": 3,
            "allow_fallback": False,
            "special_chars_group": "SPECIAL_CODE_CHARS_30",
            "special_chars": SPECIAL_CODE_CHARS_30,
            "min_special_chars": 0,
            "min_entropy": 2.0,
            "uppercase_min_entropy": 2.5,
            "soft_blacklist": ["CODE", "GAME", "FREE", "VIP", "NAP", "RUT"],
            "min_clean_length": 6,
            "max_clean_length": 8,
        },
        "qq88": {
            "description": "Nhóm riêng cho QQ88 (tangquaqq88.com)",
            "url_keywords": ["qq88", "tangquaqq88"],
            "allowed_sites": ["qq88"],
            "allow_numeric": False,
            "allow_random_mix": True,
            "require_uppercase": False,
            "force_uppercase": False,
            "remove_spaces": False,
            "prefer_spoiler": True,
            "marker_scan_lines": 3,
            "allow_fallback": False,
            "special_chars_group": "SPECIAL_CODE_CHARS_30",
            "special_chars": "",
            "min_special_chars": 0,
            "min_entropy": 2.0,
            "uppercase_min_entropy": 2.5,
            "soft_blacklist": ["CODE", "GAME", "FREE", "VIP", "NAP", "RUT"],
        },
        "hi88": {
            "description": "Nhóm riêng cho HI88 (hi88-freecode.pages.dev)",
            "url_keywords": ["hi88", "hi88code"],
            "allowed_sites": ["hi88"],
            "allow_numeric": False,
            "allow_random_mix": True,
            "require_uppercase": False,
            "force_uppercase": False,
            "remove_spaces": False,
            "prefer_spoiler": True,
            "marker_scan_lines": 3,
            "allow_fallback": False,
            "special_chars_group": "SPECIAL_CODE_CHARS_30",
            "special_chars": "",
            "min_special_chars": 0,
            "min_entropy": 2.0,
            "uppercase_min_entropy": 2.5,
            "soft_blacklist": ["CODE", "GAME", "FREE", "VIP", "NAP", "RUT"],
        },
        "default": {
            "description": "Fallback nếu URL chưa thuộc nhóm nào",
            "url_keywords": [],
            "allowed_sites": [],
            "allow_numeric": True,
            "allow_random_mix": True,
            "require_uppercase": False,
            "force_uppercase": False,
            "remove_spaces": False,
            "prefer_spoiler": True,
            "min_entropy": 2.3,
            "uppercase_min_entropy": 2.9,
            "soft_blacklist": ["CODE", "GAME", "FREE", "VIP", "NAP", "RUT"],
        },
    }

    # ==========================================
    # 📡 TELEGRAM CHANNEL CONFIG
    # ==========================================
    KJC_SPECIAL_CHANNEL_IDS = {
        -1002528908352,  # KJC GÁI XINH
        -1003503954906,  # KJC - ĐỒNG HÀNH THỂ THAO
    }

    KJC_SPECIAL_MODE = get_bool_env("KJC_SPECIAL_MODE", True)

    # OCR_ALLOWED_CHANNEL_IDS là whitelist cơ bản. OCR_MEDIA_ONLY_CHANNEL_IDS
    # và OCR_MEDIA_FALLBACK_CHANNEL_IDS là các nhóm bổ sung theo loại media;
    # logic runtime dùng hợp nhất cả ba nhóm, trừ khi fallback toàn media được bật.
    OCR_ALLOWED_CHANNEL_IDS = set(
        get_int_list_env(
            "OCR_ALLOWED_CHANNEL_IDS",
            (-1002817093108,),  # PHÁT CODE XX88
        )
    )
    # Hai channel QQ88 này phát code chủ yếu trong ảnh, không có spoiler/text.
    # Các channel khác mặc định giữ fast path spoiler/text để tránh OCR banner.
    OCR_MEDIA_ONLY_CHANNEL_IDS = get_int_list_env(
        "OCR_MEDIA_ONLY_CHANNEL_IDS",
        (-1002817093108,),
    )
    # HI88 là channel mixed: ưu tiên spoiler/text, OCR chỉ chạy khi không
    # tìm thấy code trong text/caption.
    OCR_MEDIA_FALLBACK_CHANNEL_IDS = get_int_list_env(
        "OCR_MEDIA_FALLBACK_CHANNEL_IDS",
        (-1002817093108,),
    )
    OCR_FALLBACK_ALL_MEDIA = get_bool_env("OCR_FALLBACK_ALL_MEDIA", False)

    # Chỉ channel này được phép OCR media/video. Mọi channel khác phải
    # ưu tiên spoiler/text và không được khởi động OCR.

    KJC_BROADCAST_DOMAINS = (
        "xx88code.com",
        "livemm88.net",
        "gg88live.tv",
        "liverr88.net",
    )

    KJC_BROADCAST_URLS = {
        "xx88code.com": "https://xx88code.com",
        "livemm88.net": "https://livemm88.net/nhap-code",
        "gg88live.tv": "https://gg88live.tv/nhap-code",
        "liverr88.net": "https://liverr88.net/nhap-code",
    }

    # Canonical account order per site. This override is applied after
    # channel-level config so every channel on the same site uses one list.
    # Quy tắc layout hiện tại: QQ88 / HI88 = 1 tab + 1 tài khoản.
    #          XX88 / MM88 = 3; GG88 / RR88 = 2 tài khoản song song.
    # Số tab thực tế = min(số tài khoản, tab_slots trong browser_site_profiles.py).
    DOMAIN_ACCOUNT_OVERRIDES = {
        "tangquaqq88.com": [
            {"username": "kaoboy012", "priority": 1},
            # {"username": "kuuteo012", "priority": 2},
            # {"username": "dad131", "priority": 3},
            # {"username": "okletgo123", "priority": 4},
        ],
        "xx88code.com": [
            {"username": "dad131", "priority": 1},
            {"username": "hugolan", "priority": 2},
            {"username": "dad123", "priority": 3},
        ],
        "gg88live.tv": [
            {"username": "kaoboy012", "priority": 1},
            {"username": "conve99sau", "priority": 2},
            # {"username": "hugolan012", "priority": 3},
        ],
        "livemm88.net": [
            {"username": "kaoboy012", "priority": 1},
            {"username": "dad131", "priority": 2},
            {"username": "ola12", "priority": 3},
            # Dự phòng: tab của tài khoản hết lượt sẽ chuyển sang tài khoản này.
            {"username": "dad123", "priority": 4},
        ],
        "liverr88.net": [
            {"username": "kaoboy012", "priority": 1},
            {"username": "miniichan", "priority": 2},
        ],
        "hi88-freecode.pages.dev": [
            {"username": "hugolan012", "priority": 1},
            # {"username": "kuuteo012", "priority": 2},
            # {"username": "minichan0123", "priority": 3},
            # {"username": "teoem0123", "priority": 4},
        ],
    }

    CHANNEL_CONFIG = {

        -1002528908352: {
            "name": "KJC GÁI XINH",
            "url": "https://xx88code.com",
            "filter_group": "multi_site_strict",
            "priority": 100,
            "has_video": False,
            "enabled": True,
            "accounts": [
                {"username": "dad131", "priority": 1},
                {"username": "hugolan", "priority": 2},
                {"username": "dad123", "priority": 3},
            ],
        },

        -1003503954906: {
            "name": "KJC - ĐỒNG HÀNH THỂ THAO",
            "url": "https://xx88code.com",
            "filter_group": "multi_site_strict",
            "priority": 101,
            "has_video": False,
            "enabled": True,
            "accounts": [
                {"username": "dad131", "priority": 1},
                {"username": "hugolan", "priority": 2},
                {"username": "dad123", "priority": 3},
            ],
        },

        -1002817093108: {
            "name": "PHÁT CODE XX88",
            "url": "https://xx88code.com/",
            "filter_group": "multi_site_strict",
            "priority": 6,
            "has_video": True,
            # Video mẫu 14.61s: bảng 10 mã hoàn chỉnh ở giây 9–10.
            # Một crop gồm cả hai cột, loại người và banner quảng cáo.
            "ocr_crop": (0.12, 0.67, 0.51, 0.98),
            "ocr_crops": [(0.12, 0.67, 0.51, 0.98)],
            # Chỉ OCR mốc tiếp theo nếu chưa tìm được mã hợp lệ.
            "ocr_frame_seconds": [10, 9, 8],
            "ocr_single_code_mode": False,
            # Không yêu cầu consensus vì chỉ chạy một crop bao phủ toàn vùng.
            "ocr_require_consensus": False,
            "max_ocr_codes_per_batch": 5,
            "accounts": [
                {"username": "dad131", "priority": 1},
                {"username": "hugolan", "priority": 2},
                {"username": "dad123", "priority": 3},
            ],
        },
        -1002768264448: {
            "name": "XX88 THỂ THAO ESPORT",
            "url": "https://xx88code.com/",
            "filter_group": "multi_site_strict",
            "priority": 8,
            "has_video": False,
            "accounts": [
                {"username": "dad131", "priority": 1},
                {"username": "hugolan", "priority": 2},
                {"username": "dad123", "priority": 3},
            ],
        },
        -1002730903277: {
            "name": "XX88 DỊCH VỤ GIAI NHÂN",
            "url": "https://xx88code.com/",
            "filter_group": "multi_site_strict",
            "priority": 9,
            "has_video": False,
            "accounts": [
                {"username": "dad131", "priority": 1},
                {"username": "hugolan", "priority": 2},
                {"username": "dad123", "priority": 3},
            ],
        },
        -1003731231345: {
            "name": "G88 DỊCH VỤ GIAI NHÂN",
            "url": "https://gg88live.tv/nhap-code",
            "filter_group": "multi_site_strict",
            "priority": 10,
            "has_video": False,
            "accounts": [
                {"username": "kaoboy012", "priority": 1},
                {"username": "conve99sau", "priority": 2},
                {"username": "hugolan012", "priority": 3},
            ],
        },
        -1002421765170: {
            "name": " QQ88 - KHO GIF",
            "url": "https://tangquaqq88.com/",
            "filter_group": "qq88",
            "priority": 11,
            "has_video": False,
            "accounts": [
                {"username": "kaoboy012", "priority": 1},
                {"username": "kuuteo012", "priority": 2},
                {"username": "dad131", "priority": 3},
                {"username": "okletgo123", "priority": 4},
            ],
        },
        -1003134541072: {
            "name": "MM88VIP Dịch Vụ Giai Nhân",
            "url": "https://livemm88.net/nhap-code",
            "filter_group": "mm88",
            "priority": 13,
            "has_video": False,
            "accounts": [
                {"username": "kaoboy012", "priority": 1},
                {"username": "dad131", "priority": 2},
                {"username": "ola12", "priority": 3},
                {"username": "dad123", "priority": 4},
            ],
        },
        -1002278162941: {
            "name": " QQ88 - TIN HOT 24/7",
            "url": "https://tangquaqq88.com",
            "filter_group": "qq88",
            "priority": 14,
            "has_video": False,
            "accounts": [
                {"username": "kaoboy012", "priority": 1},
                {"username": "kuuteo012", "priority": 2},
                {"username": "dad131", "priority": 3},
                {"username": "okletgo123", "priority": 4},
            ],
        },
        -1002324210129: {
            "name": "QQ88 - GIẢI TRÍ",
            "url": "https://tangquaqq88.com",
            "filter_group": "qq88",
            "priority": 15,
            "has_video": False,
            "accounts": [
                {"username": "kaoboy012", "priority": 1},
                {"username": "kuuteo012", "priority": 2},
                {"username": "dad131", "priority": 3},
                {"username": "okletgo123", "priority": 4},
            ],
        },
        -1002377579866: {
            "name": "QQ88 - TIN TỨC MỖI NGÀY",
            "url": "https://tangquaqq88.com",
            "filter_group": "qq88",
            "priority": 16,
            "has_video": False,
            "accounts": [
                {"username": "kaoboy012", "priority": 1},
                {"username": "kuuteo012", "priority": 2},
                {"username": "dad131", "priority": 3},
                {"username": "okletgo123", "priority": 4},
            ],
        },
        -1002325212717: {
            "name": "QQ88 - REVIEW PHIM HAY",
            "url": "https://tangquaqq88.com",
            "filter_group": "qq88",
            "priority": 18,
            "has_video": False,
            "accounts": [
                {"username": "kaoboy012", "priority": 1},
                {"username": "kuuteo012", "priority": 2},
                {"username": "dad131", "priority": 3},
                {"username": "okletgo123", "priority": 4},
            ],
        },
        -1002386905514: {
            "name": "RR88 DỊCH VỤ GIAI NHÂN",
            "url": "https://liverr88.net/nhap-code",
            "filter_group": "multi_site_strict",
            "priority": 21,
            "has_video": False,
            "accounts": [
                {"username": "kaoboy012", "priority": 1},
                {"username": "miniichan", "priority": 2},
            ],
        },
        -1004435825431: {
            "name": "Hi88 PHÁT CODE MIỄN PHÍ NỖ HŨ-BẮN CÁ",
            "url": "https://hi88-freecode.pages.dev/",
            "filter_group": "hi88",
            "priority": 24,
            "has_video": False,
            "ocr_crop": (0.33, 0.72, 0.05, 0.97),
            "accounts": [
                {"username": "hugolan012", "priority": 1},
                {"username": "kuuteo012", "priority": 2},
                {"username": "teoem0123", "priority": 3},
                {"username": "minichan0123", "priority": 4},
            ],
        },
        -1003933844700: {
            "name": "Hi88 - CƯỢC GIẢI TRÍ, KIẾM TIỀN TỶ",
            "url": "https://hi88-freecode.pages.dev/",
            "filter_group": "hi88",
            "priority": 25,
            "has_video": False,
            "accounts": [
                {"username": "hugolan012", "priority": 1},
                {"username": "kuuteo012", "priority": 2},
                {"username": "teoem0123", "priority": 3},
                {"username": "minichan0123", "priority": 4},
            ],
        },
        -1002695720902: {
            "name": "Hi88 - KÊNH GIẢI TRÍ HOT",
            "url": "https://hi88-freecode.pages.dev/",
            "filter_group": "hi88",
            "priority": 26,
            "has_video": False,
            "accounts": [
                {"username": "hugolan012", "priority": 1},
                {"username": "kuuteo012", "priority": 2},
                {"username": "teoem0123", "priority": 3},
                {"username": "minichan0123", "priority": 4},
            ],
        },
        -1002657420328: {
            "name": "Hi88 - TIN HOT MỖI NGÀY",
            "url": "https://hi88-freecode.pages.dev/",
            "filter_group": "hi88",
            "priority": 27,
            "has_video": False,
            "accounts": [
                {"username": "hugolan012", "priority": 1},
                {"username": "kuuteo012", "priority": 2},
                {"username": "teoem0123", "priority": 3},
                {"username": "minichan0123", "priority": 4},
            ],
        },
        -1002018121888: {
            "name": "Hi88 - KHO GIF",
            "url": "https://hi88-freecode.pages.dev/",
            "filter_group": "hi88",
            "priority": 28,
            "has_video": False,
            "ocr_crop": None,
            "accounts": [
                {"username": "hugolan012", "priority": 1},
                {"username": "kuuteo012", "priority": 2},
                {"username": "teoem0123", "priority": 3},
                {"username": "minichan0123", "priority": 4},
            ],
        },
        -1002662584621: {
            "name": "Hi88 - REVIEW PHIM HAY MỖI NGÀY",
            "url": "https://hi88-freecode.pages.dev/",
            "filter_group": "hi88",
            "priority": 29,
            "has_video": False,
            "accounts": [
                {"username": "hugolan012", "priority": 1},
                {"username": "kuuteo012", "priority": 2},
                {"username": "teoem0123", "priority": 3},
                {"username": "minichan0123", "priority": 4},
            ],
        },
        -1002625548636: {
            "name": "Hi88 TUYỂN ĐẠI LÝ HOA HỒNG 60%",
            "url": "https://hi88-freecode.pages.dev/",
            "filter_group": "hi88",
            "priority": 30,
            "has_video": False,
            "accounts": [
                {"username": "hugolan012", "priority": 1},
                {"username": "kuuteo012", "priority": 2},
                {"username": "teoem0123", "priority": 3},
                {"username": "minichan0123", "priority": 4},
            ],
        },
        -1003936595246: {
            "name": "GÁI 18+",
            "url": "https://livemm88.net/nhap-code",
            "filter_group": "mm88",
            "priority": 31,
            "has_video": False,
            "accounts": [
                {"username": "kaoboy012", "priority": 1},
                {"username": "dad131", "priority": 2},
                {"username": "ola12", "priority": 3},
                {"username": "dad123", "priority": 4},
            ],
        },
        -1003939163957: {
            "name": "MM88 GIRL DANCE",
            "url": "https://livemm88.net/nhap-code",
            "filter_group": "mm88",
            "priority": 32,
            "has_video": False,
            "accounts": [
                {"username": "kaoboy012", "priority": 1},
                {"username": "dad131", "priority": 2},
                {"username": "ola12", "priority": 3},
                {"username": "dad123", "priority": 4},
            ],
        },
        -1002519029952: {
            "name": "MM88 ĐỘNG BÀN TƠ",
            "url": "https://livemm88.net/nhap-code",
            "filter_group": "mm88",
            "priority": 33,
            "has_video": False,
            "accounts": [
                {"username": "kaoboy012", "priority": 1},
                {"username": "dad131", "priority": 2},
                {"username": "ola12", "priority": 3},
                {"username": "dad123", "priority": 4},
            ],
        },
        -1004411105242: {
            "name": "KÈO BÓNG GG88",
            "url": "https://gg88live.tv/nhap-code",
            "filter_group": "multi_site_strict",
            "priority": 36,
            "has_video": False,
            "accounts": [
                {"username": "kaoboy012", "priority": 1},
                {"username": "conve99sau", "priority": 2},
                {"username": "hugolan012", "priority": 3},
            ],
        },
        -1003912975699: {
            "name": "SOI KÈO MM88",
            "url": "https://livemm88.net/nhap-code",
            "filter_group": "mm88",
            "priority": 37,
            "has_video": False,
            "accounts": [
                {"username": "kaoboy012", "priority": 1},
                {"username": "dad131", "priority": 2},
                {"username": "ola12", "priority": 3},
                {"username": "dad123", "priority": 4},
            ],
        },
        -1004406362195: {
            "name": "RR88 SOI KÈO",
            "url": "https://liverr88.net/nhap-code",
            "filter_group": "multi_site_strict",
            "priority": 38,
            "has_video": False,
            "accounts": [
                {"username": "kaoboy012", "priority": 1},
                {"username": "miniichan", "priority": 2},
                {"username": "hugolan0123", "priority": 3},
            ],
        },
        -1004352437280: {
            "name": "SOI KÈO CÙNG XX88",
            "url": "https://xx88code.com",
            "filter_group": "multi_site_strict",
            "priority": 39,
            "has_video": False,
            "accounts": [
                {"username": "dad131", "priority": 1},
                {"username": "hugolan", "priority": 2},
                {"username": "dad123", "priority": 3},
            ],
        },
    }

    # Nếu không có env override, các nhóm được derive sau khi class hoàn tất
    # từ filter_group trong CHANNEL_CONFIG để không bị lệch khi thêm/bớt chat.
    QQ88_CHAT_IDS = get_int_list_env("QQ88_CHAT_IDS", ())
    HI88_CHAT_IDS = get_int_list_env("HI88_CHAT_IDS", ())

    # ==========================================
    # 🚫 BLACKLIST
    # ==========================================
    CODE_BLACKLIST = [
        "COM", "HTTP", "HTTPS", "WWW",
        "FACEBOOK", "TELEGRAM",
        "CHECK", "CLIP", "VUI", "BOT",
        "DAILY", "TRUYCAP", "BANCA", "NOHU",
        "ONLINE", "FREE", "CODE", "GIFTCODE",
        "MINIGAME", "GAME", "THETHAO",
        "O8THETHAO", "BONGDA", "TROLL",
    ]

    # ==========================================
    # ⚙️ FEATURE FLAGS
    # ==========================================
    ENABLE_MONITORING = True
    TELEGRAM_ADMIN_ID = get_int_env("TELEGRAM_ADMIN_ID", 0)

    # ==========================================
    # ⏱️ TIMEOUT / SPEED
    # ==========================================
    SITE_CODE_DEDUP_TTL = get_float_env("SITE_CODE_DEDUP_TTL", 30.0)
    PAGE_NAVIGATION_TIMEOUT = get_int_env("PAGE_NAVIGATION_TIMEOUT", 10000)
    FORM_SETTLE_SECONDS = get_float_env("FORM_SETTLE_SECONDS", 0.03)
    CF_WAIT_SECONDS = get_float_env("CF_WAIT_SECONDS", 1.0)
    # Edge kết nối CDP đã là trình duyệt thật; fingerprint giả có thể khiến
    # Turnstile nghi ngờ hơn. Chỉ bật khi cần A/B test với site cũ.
    STEALTH_JS_ENABLED = get_bool_env("STEALTH_JS_ENABLED", False)
    CF_POLL_INTERVAL = get_float_env("CF_POLL_INTERVAL", 0.10)
    VERIFICATION_BUTTON_WAIT_SECONDS = get_float_env("VERIFICATION_BUTTON_WAIT_SECONDS", 3.0)
    TURNSTILE_SOLVE_WAIT_SECONDS = get_float_env("TURNSTILE_SOLVE_WAIT_SECONDS", 8.0)
    PENDING_VERIFY_WATCH_SECONDS = get_float_env("PENDING_VERIFY_WATCH_SECONDS", 90.0)
    # Token Turnstile sống ~300s; coi là hết hạn sau ngần này giây (chừa dư).
    TURNSTILE_TOKEN_MAX_AGE_SECONDS = get_float_env("TURNSTILE_TOKEN_MAX_AGE_SECONDS", 240.0)
    SPOILER_FAST_PATH = get_bool_env("SPOILER_FAST_PATH", True)

    MAX_CONCURRENT_SUBMITS            = get_int_env("MAX_CONCURRENT_SUBMITS", 8)
    MAX_CONCURRENT_SUBMITS_PER_DOMAIN = get_int_env("MAX_CONCURRENT_SUBMITS_PER_DOMAIN", 3)
    # Legacy compatibility flag. Phân phối thực tế do nguồn code quyết định:
    # spoiler phân bổ theo số code/số tab; OCR luôn một lượt cho mỗi mã.
    ACCOUNTS_PER_CODE                 = get_int_env("ACCOUNTS_PER_CODE", 3)
    # Giữ tương thích với .env cũ; không dùng để nhân OCR hoặc spoiler nữa.
    SINGLE_ROUND_PER_BATCH            = get_bool_env("SINGLE_ROUND_PER_BATCH", True)
    # False (mặc định): gặp Turnstile chưa xác minh thì BỎ code và tạm dừng domain,
    # không retry. True: giữ hành vi cũ (retry theo backoff, tốn tab).
    PENDING_VERIFICATION_RETRY        = get_bool_env("PENDING_VERIFICATION_RETRY", False)
    PENDING_DOMAIN_COOLDOWN_SECONDS   = get_float_env("PENDING_DOMAIN_COOLDOWN_SECONDS", 45.0)
    MAX_RETRIES_PER_ACCOUNT           = get_int_env("MAX_RETRIES_PER_ACCOUNT", 2)
    RETRY_ON_TIMEOUT                  = get_bool_env("RETRY_ON_TIMEOUT", True)
    # Timeout tổng cho một lượt submit browser: lấy tab, điều hướng, fill,
    # xác minh và đọc kết quả. Timeout đọc kết quả vẫn cấu hình riêng theo site.
    SUBMIT_TIMEOUT_SECONDS            = get_float_env("SUBMIT_TIMEOUT_SECONDS", 45.0)

    RATE_LIMIT_BACKOFF_SECONDS = get_float_env("RATE_LIMIT_BACKOFF_SECONDS", 5.0)

    REQUESTS_PER_MINUTE = get_int_env("REQUESTS_PER_MINUTE", 30)
    MAX_BURST           = get_int_env("MAX_BURST", 5)

    # Per-domain browser rate limits. Values fall back to the global bucket
    # so older .env files remain compatible.
    DOMAIN_RATE_LIMITS = {
        "tangquaqq88.com": (get_int_env("QQ88_REQUESTS_PER_MINUTE", REQUESTS_PER_MINUTE), get_int_env("QQ88_MAX_BURST", MAX_BURST)),
        "hi88-freecode.pages.dev": (get_int_env("HI88_REQUESTS_PER_MINUTE", REQUESTS_PER_MINUTE), get_int_env("HI88_MAX_BURST", MAX_BURST)),
        "livemm88.net": (get_int_env("MM88_REQUESTS_PER_MINUTE", REQUESTS_PER_MINUTE), get_int_env("MM88_MAX_BURST", MAX_BURST)),
        "liverr88.net": (get_int_env("RR88_REQUESTS_PER_MINUTE", REQUESTS_PER_MINUTE), get_int_env("RR88_MAX_BURST", MAX_BURST)),
        "xx88code.com": (get_int_env("XX88_REQUESTS_PER_MINUTE", REQUESTS_PER_MINUTE), get_int_env("XX88_MAX_BURST", MAX_BURST)),
        "gg88live.tv": (get_int_env("GG88_REQUESTS_PER_MINUTE", REQUESTS_PER_MINUTE), get_int_env("GG88_MAX_BURST", MAX_BURST)),
    }

    # ==========================================
    # ⚡ TELEGRAM REALTIME / QUEUE
    # ==========================================
    MESSAGE_QUEUE_MAXSIZE      = get_int_env("MESSAGE_QUEUE_MAXSIZE", 2000)
    MESSAGE_WORKERS            = get_int_env("MESSAGE_WORKERS", 3)
    MAX_CONCURRENT_PROCESSING  = get_int_env("MAX_CONCURRENT_PROCESSING", 16)
    OCR_MEDIA_WORKERS          = get_int_env("OCR_MEDIA_WORKERS", 2)
    OCR_MEDIA_QUEUE_MAXSIZE    = get_int_env("OCR_MEDIA_QUEUE_MAXSIZE", 32)

    HEARTBEAT_INTERVAL            = get_float_env("HEARTBEAT_INTERVAL", 300.0)
    TELEGRAM_CATCH_UP             = get_bool_env("TELEGRAM_CATCH_UP", False)
    # Telegram KHÔNG luôn đẩy tin kênh theo thời gian thực (tài khoản vào nhiều kênh
    # -> tin chỉ về theo lượt kiểm tra 15 phút của Telethon). TelegramReceiver (tg_ingest.py)
    # hỏi Telegram TUẦN TỰ: mỗi lần chỉ 1 request GetPeerDialogs cho cả 31 kênh, xong mới
    # tới lượt kế. Đây là khoảng cách tối thiểu giữa 2 lần BẮT ĐẦU poll. Đặt 0 để tắt poll.
    CHANNEL_POLL_INTERVAL         = get_float_env("CHANNEL_POLL_INTERVAL", 1.0)
    # Request poll treo quá số giây này thì bỏ và bắn lại ngay.
    CHANNEL_POLL_REQUEST_TIMEOUT  = get_float_env("CHANNEL_POLL_REQUEST_TIMEOUT", 5.0)
    # Timeout khi phải gọi thêm get_messages (nhiều tin mới cùng lúc, tin sửa thiếu dữ liệu).
    CHANNEL_POLL_FETCH_TIMEOUT    = get_float_env("CHANNEL_POLL_FETCH_TIMEOUT", 8.0)
    # Reconnect theo THỜI GIAN: sau chừng này giây KHÔNG có phản hồi poll OK nào thì đóng
    # connection cho watchdog nối lại; tối thiểu cách nhau COOLDOWN giây. (Thay cho cơ chế
    # đếm "N timeout liên tiếp" cũ vốn gây bão reconnect.)
    POLL_STALL_RECONNECT_SECONDS    = get_float_env("POLL_STALL_RECONNECT_SECONDS", 45.0)
    POLL_RECONNECT_COOLDOWN_SECONDS = get_float_env("POLL_RECONNECT_COOLDOWN_SECONDS", 120.0)
    # Telegram giới hạn tần suất GetPeerDialogs (FloodWait ~4-5s, đo từ log: ~12 phản hồi OK/phút).
    # Receiver tự học nhịp poll từ FloodWait; đây là trần của nhịp tự học (giây) và số lần poll OK
    # liên tiếp không bị FloodWait trước khi thử nới nhịp xuống 5%.
    CHANNEL_POLL_MAX_INTERVAL     = get_float_env("CHANNEL_POLL_MAX_INTERVAL", 10.0)
    CHANNEL_POLL_FLOOD_DECAY_AFTER = get_int_env("CHANNEL_POLL_FLOOD_DECAY_AFTER", 60)
    TELEGRAM_INBOX_RECOVERY_MODE  = os.getenv(
        "TELEGRAM_INBOX_RECOVERY_MODE",
        "live_only",
    ).strip().lower()
    if TELEGRAM_INBOX_RECOVERY_MODE not in {"at_least_once", "live_only"}:
        logging.getLogger(__name__).warning(
            "⚠️ TELEGRAM_INBOX_RECOVERY_MODE=%r không hợp lệ; dùng live_only",
            TELEGRAM_INBOX_RECOVERY_MODE,
        )
        TELEGRAM_INBOX_RECOVERY_MODE = "live_only"
    # Chỉ nhận callback NewMessage/MessageEdited từ CHANNEL_CONFIG ngay tại
    # Telethon, sau đó quick() kiểm tra lại để phòng filter/entity edge cases.
    # Tắt cờ này chỉ dành cho chẩn đoán; Raw update probe vẫn luôn toàn cục.
    TELEGRAM_FILTER_AT_SOURCE     = get_bool_env("TELEGRAM_FILTER_AT_SOURCE", True)
    # Chỉ bật khi chẩn đoán peer/channel ID; mặc định không log mọi event
    # Telegram để tránh làm nghẽn event loop.

    DOMAIN_QUEUE_MAXSIZE = get_int_env("DOMAIN_QUEUE_MAXSIZE", 500)
    TELEGRAM_INBOX_DB_PATH = os.getenv("TELEGRAM_INBOX_DB_PATH", "data/telegram_inbox.db")
    TELEGRAM_INBOX_LEASE_SECONDS = get_int_env("TELEGRAM_INBOX_LEASE_SECONDS", 300)
    TELEGRAM_INBOX_DRAIN_BATCH = get_int_env("TELEGRAM_INBOX_DRAIN_BATCH", 250)
    # Poll nhanh hơn để row mới không phải chờ nửa giây trước khi vào worker.
    TELEGRAM_INBOX_DRAIN_INTERVAL = get_float_env("TELEGRAM_INBOX_DRAIN_INTERVAL", 0.2)
    TELEGRAM_INBOX_ENQUEUE_RETRIES = get_int_env("TELEGRAM_INBOX_ENQUEUE_RETRIES", 3)
    SHUTDOWN_MESSAGE_DRAIN_TIMEOUT = get_float_env("SHUTDOWN_MESSAGE_DRAIN_TIMEOUT", 8.0)

    # ✅ FIX RETRY-STORM: trước đây DurableInbox.retry() không có giới hạn số
    # lần thử → 1 tin nhắn lỗi (site trả NO_RESULT liên tục, CDP mất kết nối...)
    # bị replay lại vô hạn mỗi vài giây, chiếm slot xử lý và làm trễ tin nhắn
    # mới. Giờ mọi retry đi qua DurableInbox.retry_or_fail(), bị chặn ở
    # MAX_INBOX_ATTEMPTS lần thử với backoff tăng dần (INBOX_RETRY_BASE_DELAY
    # → nhân đôi mỗi lần, trần ở INBOX_RETRY_MAX_DELAY).
    MAX_INBOX_ATTEMPTS = get_int_env("MAX_INBOX_ATTEMPTS", 5)
    INBOX_RETRY_BASE_DELAY = get_float_env("INBOX_RETRY_BASE_DELAY", 2.0)
    INBOX_RETRY_MAX_DELAY = get_float_env("INBOX_RETRY_MAX_DELAY", 120.0)

    # Quét dọn định kỳ các dòng 'pending' bị kẹt quá lâu (lỗi không xác định,
    # crash giữa chừng...) — giftcode gần như luôn hết hạn trước mốc này nên
    # an toàn khi bỏ qua, tránh backlog cũ tiếp tục dồn ứ queue.
    INBOX_MAX_PENDING_AGE_SECONDS = get_float_env("INBOX_MAX_PENDING_AGE_SECONDS", 900.0)
    INBOX_PENDING_SWEEP_INTERVAL_SECONDS = get_float_env("INBOX_PENDING_SWEEP_INTERVAL_SECONDS", 300.0)

    # Tuổi tối đa (giây) của một mã/tin trước khi chiếm tab trình duyệt.
    # QQ88/HI88 chỉ có 1 tab nên xử lý tuần tự; mã cũ trong hàng đợi làm mã
    # mới bị trễ. Quá ngưỡng này sẽ bị bỏ qua ngay khi lấy khỏi hàng đợi.
    # Đặt 0 để tắt (hành vi cũ).
    CODE_MAX_AGE_SECONDS = get_float_env("CODE_MAX_AGE_SECONDS", 120.0)

    # ==========================================
    # ⚙️ WATCHDOG / MISC
    # ==========================================
    INPUT_CACHE_CLEANUP_INTERVAL = get_float_env("INPUT_CACHE_CLEANUP_INTERVAL", 30.0)

    # ==========================================
    # 🆕 OCR (chỉ PHÁT CODE XX88; tối đa 2 ảnh xử lý đồng thời)
    # ==========================================
    OCR_CONFIDENCE_THRESHOLD = get_float_env("OCR_CONFIDENCE_THRESHOLD", 0.70)
    # XX88 dùng crop bao phủ toàn vùng code; không chạy inference toàn ảnh
    # lần hai khi crop ít text để giữ latency thấp và tránh nhân đôi CPU.
    OCR_CROP_FALLBACK = get_bool_env("OCR_CROP_FALLBACK", False)
    # Giới hạn cạnh dài trước inference để giảm đáng kể số pixel cần xử lý.
    OCR_MAX_IMAGE_SIDE = get_int_env("OCR_MAX_IMAGE_SIDE", 1280)
    OCR_MIN_CROP_WIDTH = get_int_env("OCR_MIN_CROP_WIDTH", 300)
    # Cho phép xử lý song song có kiểm soát khi nhiều ảnh tới cùng lúc.
    # RapidOCR dùng chung engine singleton; không nên đặt quá cao trên CPU.
    MAX_CONCURRENT_OCR = get_int_env("MAX_CONCURRENT_OCR", 2)
    VIDEO_OCR_MAX_FRAMES = get_int_env("VIDEO_OCR_MAX_FRAMES", 3)
    # Tải media là bottleneck I/O chính; OCR vẫn giữ MAX_CONCURRENT_OCR=2
    # để không làm nghẽn CPU/RAM.
    MAX_CONCURRENT_MEDIA_DOWNLOADS = get_int_env("MAX_CONCURRENT_MEDIA_DOWNLOADS", 4)
    FAST_TELEGRAM_DOWNLOAD = get_bool_env("FAST_TELEGRAM_DOWNLOAD", False)
    FAST_DOWNLOAD_MIN_BYTES = get_int_env("FAST_DOWNLOAD_MIN_BYTES", 4 * 1024 * 1024)
    FAST_DOWNLOAD_WORKERS = get_int_env("FAST_DOWNLOAD_WORKERS", 6)
    FAST_DOWNLOAD_CHUNK_KB = get_int_env("FAST_DOWNLOAD_CHUNK_KB", 512)
    # Cạnh dài tối đa đưa vào model detect của RapidOCR (0 = dùng mặc định thư viện)
    OCR_DET_LIMIT_SIDE = get_int_env("OCR_DET_LIMIT_SIDE", 640)
    OCR_FAST_PATH = get_bool_env("OCR_FAST_PATH", True)
    OCR_FAST_MIN_CHARS = get_int_env("OCR_FAST_MIN_CHARS", 8)
    OCR_FAST_VARIANTS = get_int_env("OCR_FAST_VARIANTS", 1)
    OCR_SKIP_CLS = get_bool_env("OCR_SKIP_CLS", True)  # bỏ bước nhận diện hướng chữ, nhanh hơn
    # Cache kết quả theo hash nội dung media + tham số OCR để tránh chạy lại
    # RapidOCR khi Telegram giao lại cùng một ảnh/video trong thời gian ngắn.
    OCR_CACHE_TTL_SECONDS = get_float_env("OCR_CACHE_TTL_SECONDS", 600.0)
    OCR_CACHE_MAX_ENTRIES = get_int_env("OCR_CACHE_MAX_ENTRIES", 128)
    OCR_DEBUG_SAVE_FRAMES = get_bool_env("OCR_DEBUG_SAVE_FRAMES", False)
    OCR_DEBUG_TIMING = get_bool_env("OCR_DEBUG_TIMING", False)
    MEDIA_DOWNLOAD_RETRIES = get_int_env("MEDIA_DOWNLOAD_RETRIES", 1)
    MEDIA_DOWNLOAD_RETRY_DELAY = get_float_env("MEDIA_DOWNLOAD_RETRY_DELAY", 0.5)
    MEDIA_DOWNLOAD_MAX_SIZE_MB = get_int_env("MEDIA_DOWNLOAD_MAX_SIZE_MB", 200)
    # Dọn các thư mục media OCR bị sót sau crash theo chu kỳ 3 ngày.
    MEDIA_CLEANUP_INTERVAL_SECONDS = get_int_env(
        "MEDIA_CLEANUP_INTERVAL_SECONDS", 3 * 24 * 60 * 60
    )
    OCR_TEMP_DIR_MAX_AGE_SECONDS = get_int_env(
        "OCR_TEMP_DIR_MAX_AGE_SECONDS", 3 * 24 * 60 * 60
    )
    MAX_OCR_CODES_PER_BATCH = get_int_env("MAX_OCR_CODES_PER_BATCH", 4)
    PAUSE_OCR_ON_HIGH_CPU = get_bool_env("PAUSE_OCR_ON_HIGH_CPU", True)
    OCR_CPU_THRESHOLD = get_int_env("OCR_CPU_THRESHOLD", 85)
    OCR_RAM_PAUSE_THRESHOLD = get_int_env("OCR_RAM_PAUSE_THRESHOLD", 90)
    OCR_RAM_WARN_THRESHOLD = get_int_env("OCR_RAM_WARN_THRESHOLD", 85)
    HEALTH_CHECK_INTERVAL = get_float_env("HEALTH_CHECK_INTERVAL", 30.0)
    HEALTH_BREACH_CONFIRM_COUNT = get_int_env("HEALTH_BREACH_CONFIRM_COUNT", 2)
    QUEUE_SOFT_LIMIT_PCT = get_float_env("QUEUE_SOFT_LIMIT_PCT", 0.75)
    QUEUE_HARD_LIMIT_PCT = get_float_env("QUEUE_HARD_LIMIT_PCT", 0.95)
    QUEUE_CLEANUP_ON_SOFT_LIMIT = get_bool_env("QUEUE_CLEANUP_ON_SOFT_LIMIT", False)
    QUEUE_CLEANUP_TARGET_PCT = get_float_env("QUEUE_CLEANUP_TARGET_PCT", 0.50)
    QUEUE_CHECK_INTERVAL = get_float_env("QUEUE_CHECK_INTERVAL", 5.0)

    # ==========================================
    # 🆕 LOG ROTATION
    # ==========================================
    LOG_ROTATION_MAX_BYTES    = get_int_env("LOG_ROTATION_MAX_BYTES", 10_485_760)
    LOG_ROTATION_BACKUP_COUNT = get_int_env("LOG_ROTATION_BACKUP_COUNT", 5)

    # ==========================================
    # 🌐 BROWSER-ONLY
    # ==========================================
    BROWSER_ONLY = True
    BROWSER_CIRCUIT_FAILURE_THRESHOLD = get_int_env("BROWSER_CIRCUIT_FAILURE_THRESHOLD", 3)
    BROWSER_CIRCUIT_COOLDOWN_SECONDS = get_float_env("BROWSER_CIRCUIT_COOLDOWN_SECONDS", 60.0)


    # Cổng debug Edge — phải khớp với Edge đang chạy sẵn trên máy.
    # (--remote-debugging-port=<cổng này>).
    EDGE_CDP_HOST = os.getenv("EDGE_CDP_HOST", "127.0.0.1").strip() or "127.0.0.1"
    EDGE_CDP_PORT = get_int_env("EDGE_CDP_PORT", 9222)
    EDGE_CDP_CONNECT_TIMEOUT_SECONDS = get_float_env("EDGE_CDP_CONNECT_TIMEOUT_SECONDS", 15.0)
    EDGE_CDP_RECONNECT_COOLDOWN_SECONDS = get_float_env("EDGE_CDP_RECONNECT_COOLDOWN_SECONDS", 5.0)
    # Maximum existing tab slots per domain; missing tabs are never created.
    TAB_POOL_SIZE = get_int_env("TAB_POOL_SIZE", 3)
    TAB_ACQUIRE_WAIT_SECONDS = get_float_env("TAB_ACQUIRE_WAIT_SECONDS", 2.0)
    MAX_TAB_PER_DOMAIN_CAP = get_int_env("MAX_TAB_PER_DOMAIN_CAP", 5)
    # Compatibility GC settings: closed references only; live tabs are kept.
    TAB_POOL_IDLE_TTL = get_float_env("TAB_POOL_IDLE_TTL", 900.0)
    TAB_POOL_MIN_TABS_PER_DOMAIN = get_int_env("TAB_POOL_MIN_TABS_PER_DOMAIN", 2)
    TAB_POOL_MEMORY_COMPACT_IDLE_SECONDS = get_float_env(
        "TAB_POOL_MEMORY_COMPACT_IDLE_SECONDS", 300.0
    )
    TAB_POOL_MEMORY_RELOAD_IDLE_SECONDS = get_float_env(
        "TAB_POOL_MEMORY_RELOAD_IDLE_SECONDS", 1800.0
    )
    # Strict existing-browser mode; creation is removed from browser_engine.
    # Keep this compatibility attribute false even if an old .env says true.
    AUTO_OPEN_MISSING_TABS = False

    # Nhịp browser_watchdog: kiểm tra/reconnect CDP trước, rồi mới phục hồi tab.
    CDP_PING_INTERVAL = get_float_env("CDP_PING_INTERVAL", 5.0)
    EDGE_CDP_WATCHDOG_INTERVAL = get_float_env("EDGE_CDP_WATCHDOG_INTERVAL", 5.0)

    # Thời gian tối đa (ms) chờ đọc kết quả sau khi bấm submit, và nhịp
    # poll giữa các lần đọc (giây) — RESULT_POLL_INTERVAL đã có sẵn trong
    # .env (dùng chung với phần queue trước đây).
    RESULT_DETECTION_TIMEOUT = get_float_env("RESULT_DETECTION_TIMEOUT", 5000.0)
    RESULT_SELECTOR_FAST_WINDOW_SECONDS = get_float_env("RESULT_SELECTOR_FAST_WINDOW_SECONDS", 0.80)
    RESULT_POLL_INTERVAL = get_float_env("RESULT_POLL_INTERVAL", 0.05)
    MM88_FORM_SETTLE_SECONDS = get_float_env("MM88_FORM_SETTLE_SECONDS", 0.05)
    MM88_RESULT_DETECTION_TIMEOUT = get_float_env("MM88_RESULT_DETECTION_TIMEOUT", 3500.0)
    RESULT_DETECTION_TIMEOUT_BY_DOMAIN = {
        "tangquaqq88.com": get_float_env("QQ88_RESULT_DETECTION_TIMEOUT", 3500.0),
        "hi88-freecode.pages.dev": get_float_env("HI88_RESULT_DETECTION_TIMEOUT", 4500.0),
        "livemm88.net": get_float_env("MM88_RESULT_DETECTION_TIMEOUT", 4000.0),
        "liverr88.net": get_float_env("RR88_RESULT_DETECTION_TIMEOUT", 3000.0),
        "xx88code.com": get_float_env("XX88_RESULT_DETECTION_TIMEOUT", 3500.0),
        "gg88live.tv": get_float_env("GG88_RESULT_DETECTION_TIMEOUT", 3500.0),
    }
    PRESERVE_PREFILLED_USERNAME = get_bool_env("PRESERVE_PREFILLED_USERNAME", True)

    # Sau bao nhiêu lần submit thì reload lại trang 1 lần cho "sạch" (thay
    # vì chỉ xoá input) — tránh trang bị phình state sau nhiều lượt submit.
    FULL_RELOAD_EVERY_N = get_int_env("FULL_RELOAD_EVERY_N", 300)

    # true = chụp ảnh + lưu HTML khi kết quả submit không rõ ràng (giúp
    # debug khi giao diện 1 site đã đổi so với lúc code chạy trước đó).
    SCREENSHOT_ON_UNKNOWN = get_bool_env("SCREENSHOT_ON_UNKNOWN", True)
    SCREENSHOT_MIN_INTERVAL_SECONDS = get_float_env("SCREENSHOT_MIN_INTERVAL_SECONDS", 60.0)
    SCREENSHOT_MAX_FILES = get_int_env("SCREENSHOT_MAX_FILES", 300)
    # Sai code/hết hạn là kết quả thường gặp; không chụp trong lúc giữ tab lock.
    SCREENSHOT_ON_FAILED = get_bool_env("SCREENSHOT_ON_FAILED", False)


# Derive các nhóm chat và URL theo domain sau khi class đã được tạo. Cách đặt
# sau class tránh giới hạn scope của comprehension trong thân class.
def _normalize_config_domain(value: str) -> str:
    """Return the canonical hostname used by all account configuration paths."""
    raw = str(value or "").strip().lower()
    if "://" in raw:
        raw = urlparse(raw).netloc or raw
    else:
        raw = raw.split("/", 1)[0]
    return raw.removeprefix("www.").strip(".")


def is_domain_enabled(domain: str) -> bool:
    """One runtime gate for ingress targets, workers, recovery and browser."""
    canonical = _normalize_config_domain(domain)
    return canonical in getattr(Config, "DOMAIN_TO_CHANNEL_URL", {})


def get_effective_domain_accounts(domain: str) -> list[dict]:
    """Return the single effective account list for a domain.

    ``DOMAIN_ACCOUNT_OVERRIDES`` is authoritative when the domain is present.
    If no override exists, accounts are merged from all channel configs for the
    domain, de-duplicated by username, and ordered by priority.  Every returned
    account is copied so callers cannot mutate the configuration in place.
    """
    canonical_domain = _normalize_config_domain(domain)
    if not is_domain_enabled(canonical_domain):
        return []
    overrides = getattr(Config, "DOMAIN_ACCOUNT_OVERRIDES", {})
    normalized_overrides = {
        _normalize_config_domain(key): value
        for key, value in overrides.items()
    }
    if canonical_domain in normalized_overrides:
        source = normalized_overrides[canonical_domain]
    else:
        merged: dict[str, dict] = {}
        for site in getattr(Config, "CHANNEL_CONFIG", {}).values():
            site_domain = _normalize_config_domain(site.get("url", ""))
            if site_domain != canonical_domain:
                continue
            for account in site.get("accounts", []) or []:
                if not isinstance(account, dict):
                    continue
                username = str(account.get("username", "")).strip()
                if not username:
                    continue
                candidate = dict(account)
                candidate["username"] = username
                previous = merged.get(username)
                try:
                    candidate_priority = int(candidate.get("priority", 999))
                except (TypeError, ValueError):
                    candidate_priority = 999
                try:
                    previous_priority = int(previous.get("priority", 999)) if previous else 999
                except (TypeError, ValueError):
                    previous_priority = 999
                if previous is None or candidate_priority < previous_priority:
                    merged[username] = candidate
        source = merged.values()

    def _sort_key(account: dict) -> tuple[int, str]:
        try:
            priority = int(account.get("priority", 999))
        except (TypeError, ValueError):
            priority = 999
        return priority, str(account.get("username", "")).strip()

    return sorted(
        (
            dict(account)
            for account in source
            if isinstance(account, dict) and str(account.get("username", "")).strip()
        ),
        key=_sort_key,
    )


def _normalize_active_domain(value: str) -> str:
    """Map short domain names from env to the canonical site hostname."""
    raw = str(value or "").strip().lower().removeprefix("www.")
    aliases = {
        "hi88": "hi88-freecode.pages.dev",
        "qq88": "tangquaqq88.com",
        "mm88": "livemm88.net",
        "rr88": "liverr88.net",
        "xx88": "xx88code.com",
        "gg88": "gg88live.tv",
    }
    return aliases.get(raw, raw)


_active_domains_raw = os.getenv("ACTIVE_DOMAINS", "").strip()
if _active_domains_raw:
    _active_domains = {
        _normalize_active_domain(part)
        for part in _active_domains_raw.split(",")
        if str(part).strip()
    }
    if _active_domains:
        Config.CHANNEL_CONFIG = {
            chat_id: cfg
            for chat_id, cfg in Config.CHANNEL_CONFIG.items()
            if _normalize_active_domain(
                str(cfg.get("url", "")).split("//", 1)[-1].split("/", 1)[0]
            ) in _active_domains
        }
else:
    _active_domains = set()
Config.ACTIVE_DOMAINS = tuple(sorted(_active_domains))

# Remove paused QQ88 channels before deriving receiver/account/domain maps.
if not Config.QQ88_ENABLED:
    Config.CHANNEL_CONFIG = {
        cid: site for cid, site in Config.CHANNEL_CONFIG.items()
        if _normalize_config_domain(site.get("url", "")) != "tangquaqq88.com"
    }

if not os.getenv("QQ88_CHAT_IDS", "").strip():
    Config.QQ88_CHAT_IDS = tuple(
        int(cid) for cid, cfg in Config.CHANNEL_CONFIG.items()
        if cfg.get("filter_group") == "qq88"
    )
if not os.getenv("HI88_CHAT_IDS", "").strip():
    Config.HI88_CHAT_IDS = tuple(
        int(cid) for cid, cfg in Config.CHANNEL_CONFIG.items()
        if cfg.get("filter_group") == "hi88"
    )

Config.DOMAIN_TO_CHANNEL_URL: dict[str, str] = {}
for _cfg in Config.CHANNEL_CONFIG.values():
    _raw_url = str(_cfg.get("url", ""))
    _domain = _raw_url.split("//", 1)[-1].split("/", 1)[0].lower()
    _domain = _domain.removeprefix("www.")
    if _domain:
        Config.DOMAIN_TO_CHANNEL_URL.setdefault(_domain, _raw_url)

# Chuẩn hóa account ở cấp channel theo hàm canonical của domain.
# Như vậy mọi đường xử lý (ingress, queue, browser worker) đều dùng cùng
# một danh sách, không còn tình trạng channel giữ account cũ khác domain map.
_config_logger = logging.getLogger(__name__)
for _chat_id, _site in Config.CHANNEL_CONFIG.items():
    _domain = _normalize_config_domain(_site.get("url", ""))
    _canonical = get_effective_domain_accounts(_domain)
    if _canonical:
        _old_accounts = _site.get("accounts", [])
        _new_accounts = [dict(_account) for _account in _canonical]
        if _old_accounts != _new_accounts:
            _config_logger.debug(
                "Domain account override applied: chat=%s domain=%s old=%s new=%s",
                _chat_id,
                _domain,
                [a.get("username") for a in _old_accounts],
                [a.get("username") for a in _new_accounts],
            )
        _site["accounts"] = _new_accounts

# Chốt chặn kiểm tra lỗi cấu hình lúc khởi động
for chat_id, site in Config.CHANNEL_CONFIG.items():
    group = site.get("filter_group")
    if group and group not in Config.CODE_FILTER_GROUPS:
        raise ValueError(
            f"LỖI CẤU HÌNH: filter_group '{group}' (site '{site.get('name', 'Unknown')}') "
            f"chưa được định nghĩa trong CODE_FILTER_GROUPS!"
        )