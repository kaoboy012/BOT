"""
📋 CODE VALIDATOR - STRICT CODE FILTER
Ưu tiên code thật, chặn chữ quảng cáo, link, hashtag, username, domain và text chat.
Hỗ trợ gom nhiều kênh vào một nhóm lọc để dễ kiểm soát.
✅ MMOO: Hỗ trợ placeholder & tính toán
"""

from __future__ import annotations

import ast
import math
import operator
import re
from config import Config
from logger_setup import logger


# ✅ Safe math evaluator thay thế eval() cho placeholder MMOO
_SAFE_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.USub: operator.neg,
}


def _safe_eval_math(expr: str):
    """Evaluate simple math expression safely (no eval())."""
    try:
        tree = ast.parse(expr.strip(), mode="eval")
        return _eval_node(tree.body)
    except Exception:
        raise ValueError(f"Invalid expression: {expr}")


def _eval_node(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _SAFE_OPS:
        return _SAFE_OPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _SAFE_OPS:
        return _SAFE_OPS[type(node.op)](_eval_node(node.operand))
    raise ValueError("Unsupported expression")


# Compile sẵn 1 lần lúc import — tránh compile lại regex mỗi lượt validate.
_XX88_BIGWIN_RE = re.compile(r'^[A-Za-z0-9](\*[A-Za-z0-9]){4,}$')
_ALPHA_ONLY_RE = re.compile(r"[A-Z]+")
_VIET_NUMBER_DIGITS_RE = re.compile(r'^s[oố]\s*(\d+)$')
_VIET_NUMBER_WORD_RE = re.compile(r'^s[oố]\s+(.+)$')
_PLACEHOLDER_ANGLE_RE = re.compile(r'<([^>]+)>')
_PLACEHOLDER_MATH_RE = re.compile(r'^[\d+\-*/%\s()]+$')
_PLACEHOLDER_QUOTE_RE = re.compile(r'"([^"]+)"')
_VIET_QUOTE_RE = re.compile(r'"s[oố]\s*\w+"')
_REPEATED_CHAR_RE = re.compile(r"(.)\1{2,}")


class CodeValidator:
    SITE_ROUTING_RULES = {
        # Chỉ nhận tiền tố 4 ký tự đầy đủ. Bỏ "M88" và "QQ" (quá ngắn): mã
        # ngẫu nhiên của XX88/Hi88... bắt đầu bằng QQ hoặc M88 từng bị loại nhầm.
        "mm88": ["MM88"],
        "qq88": ["QQ88"],
        # KHÔNG thêm rule cho hi88/xx88 — code thật của các site này là
        # chuỗi random thuần túy, không có tiền tố cố định, dễ nhận nhầm.
    }

    COMMON_WORDS = [
        "CHUC", "MUNG", "HOM", "NAY", "NGAY", "THANG", "TANG", "LIXI", "NHAN", "THUONG",
        "DANG", "NHAP", "THAM", "GIA", "LINK", "GAME", "RUT", "NAP",
        "TIEN", "TAI", "KHOAN", "KHUYEN", "MAI", "DANGKY", "THANHCONG",
        "NHOM", "KENH", "ADMIN", "HOTRO", "CSKH", "ZALO", "TELE",
        "DANGNHAP", "MATKHAU", "LIENHE", "TRANGCHU", "NHACAI", "UYTIN",
        "BAOTRI", "NOHU", "BANCA", "THETHAO", "CASINO", "LODE", "XOSO",
        "KHONG", "DUOC", "HAY", "THOI", "GIAI", "TRI", "MUC", "VIP",
        "THEO", "DOI", "CHIA", "LIKE", "SHARE", "YOUTUBE",
        "MINIGAME", "O8THETHAO", "BONGDA", "TRUYCAP", "GIFTCODE", "EVENT",
        "FACEBOOK", "TELEGRAM", "TIKTOK", "WEBSITE", "OFFICIAL", "CHANNEL",
        "QUATANG", "BOT", "CHECK", "FREE", "ONLINE", "DAILY", "CLIP",
        "NHANH", "NHANHTAY", "ANH", "EM", "DUNG", "BO", "LO", "JACKPOT",
        "CANHBAO", "GIAMAO", "THONGBAO", "DANGNHAP", "BAOMAT", "KIEMTRA",
        "TAIDAY", "THONGTIN", "HOTLINE", "SUPPORT", "CHAT", "POST",
        "VIEW", "COMMENT", "PINNED", "SUBSCRIBE", "JOIN", "GROUP",
        "DANHSACH", "DIEUKIEN", "HUONGDAN", "KETQUA", "CHUCDANH", "PHAT",
        "XEMNGAY", "CLICK", "LOGIN", "PASSWORD", "TAIKHOAN", "KHUYENMAI",
        "TIENTHUONG", "SIEUTIENTHUONG", "SIEUPHAM",
        "DOTPHA", "GIAITHUONG", "GIAICUU", "CUOCTHUA",
        "NHANTHUONG", "MINHTHUONG", "PHANTHUONG",
        "NOHUBANCA", "BANCANO", "NOHUTAPDO",
        "SIEUKHUYEN", "SIEUNAP", "DOITHUONG",
        "REVIEWPHIM", "TINTUC", "TINTUCHANGAY",
        "NOHU47", "BANCA47",
        "VOUCHER", "BONUS", "GIFT", "REWARD", "PROMO", "PROMOTION",
        "WELCOME", "DEPOSIT", "WITHDRAW", "REGISTER", "SIGNUP", "MOBILE",
    ]

    VIETNAMESE_TEXT_WORDS = [
        "khong", "dung", "nhanh", "tang", "code", "free", "clip", "vui",
        "dang", "nhap", "dangky", "truycap", "chinh", "thuc", "kenh",
        "thong", "bao", "canh", "gia", "mao", "kiem", "tra", "duong",
        "link", "facebook", "tiktok", "telegram", "zalo", "website",
        "hom", "nay", "anh", "em", "nhan", "qua", "thuong", "jackpot",
        "may", "man", "don", "cho", "chat", "bot", "cskh", "hotro",
        "lienhe", "taiday", "dangnhap", "matkhau", "taikhoan",
    ]

    FAKE_CODE_PATTERNS = [
        r"^(TEST|DEMO|EXAMPLE|FAKE|SAMPLE)",
        r"^(ABC|DEF|GHI|JKL|MNO|PQR|STU|VWX|YZ)$",
        r"^(123|456|789|000|111|222|333|444|555|666|777|888|999)$",
        r"^(AAAA|BBBB|CCCC|DDDD|EEEE|FFFF|GGGG|HHHH|IIII|JJJJ)$",
    ]
    # ✅ TỐI ƯU: biên dịch sẵn 1 lần — is_likely_fake() quét qua TOÀN BỘ
    # danh sách này cho MỖI code, trước đây mỗi vòng lặp gọi
    # re.match(fake_pattern, ...) với pattern dạng str (tự compile lại/tra
    # cache mỗi lần); giờ dùng thẳng object đã compile.
    _FAKE_CODE_REGEXES = [re.compile(p) for p in FAKE_CODE_PATTERNS]

    # Tên nhà cung cấp/thương hiệu game hay xuất hiện trong quảng cáo gần
    # link/banner (vd "TRÒ CHƠI Golden Empire", "SÃNH GAME JiLi") — không
    # bao giờ là code thật dù đứng riêng lẻ trên 1 dòng cạnh link giftcode.
    GAME_BRAND_BLACKLIST = {
        "JILI", "PGSOFT", "PRAGMATIC", "GOLDEN", "EMPIRE", "JOKER",
        "CQ9", "AMB", "FACHAI", "SPADEGAMING", "MICROGAMING", "SCATTER",
    }

    SOFT_BLACKLIST = {"CODE", "GAME", "FREE", "VIP", "NAP", "RUT"}
    HARD_BLACKLIST = {
        "HTTP", "HTTPS", "WWW", "FACEBOOK", "TELEGRAM", "TIKTOK", "ZALO",
        "CHECK", "CLIP", "DAILY", "TRUYCAP", "BANCA", "NOHU", "ONLINE",
        "GIFTCODE", "MINIGAME", "THETHAO", "O8THETHAO", "BONGDA", "TROLL",
        "SUPPORT", "HOTLINE", "CSKH",
    }

    # Từ khoá rác OCR — banner sự kiện hay bị OCR dính liền vào code thật
    # trong video XX88 (vd "EEOEMEGALIVE" = code "EEOE" + banner "MEGA LIVE").
    OCR_JUNK_KEYWORDS = [
        "TIENTHUONG", "NOHUBAN", "BANCANO", "SIEUTHUO",
        "DOTPHA", "GIAICUU", "CUOCTHUA", "REVIEWPHIM",
        "TINTUCMO", "NOHUTAP", "KHOGIF",
        "IENTHU", "IEUTHU", "AKTIEN", "SIKTIEN",
        "MEGALIVE", "MEGA LIVE", "BUNGNA",
    ]

    @staticmethod
    def clean_code(code, to_upper: bool = False):
        """
        Làm sạch code — xử lý các dạng đặc biệt:
        - XX88 BigWin: T*2*8*G*K*P*G*G → T28GKPGG (dấu * xen giữa từng ký tự)
        - ví dụ: 9Q»KT»AZ >> 62_IR*9O → 9QKTAZ62IR9O
        - MM88: N*8W/T0#S → N8WT0S
        - MMOO/UY88: code bình thường có thể có dấu

        to_upper=True: ép hoa ngay trong cùng 1 lượt duyệt (single-pass),
        tránh phải gọi .upper() riêng ở nơi gọi (tạo bản sao chuỗi thừa).
        """
        if not code:
            return ""
        s = str(code).strip()

        # XX88 BigWin: mỗi ký tự cách nhau bởi * (vd T*2*8*G*K*P*G*G)
        if _XX88_BIGWIN_RE.match(s):
            s = s.replace('*', '')
            return s.upper() if to_upper else s

        # Lọc ký tự hợp lệ (A-Z a-z 0-9) + ép hoa (nếu to_upper) trong cùng
        # 1 lượt duyệt, dùng list+join thay vì nối chuỗi += (tránh O(n) mỗi lần).
        out = []
        append = out.append
        for ch in s:
            if ('a' <= ch <= 'z') or ('A' <= ch <= 'Z') or ('0' <= ch <= '9'):
                append(ch.upper() if to_upper else ch)
        return "".join(out)

    @staticmethod
    def calculate_entropy(code):
        if not code:
            return 0.0

        char_freq = {}
        for char in code:
            char_freq[char] = char_freq.get(char, 0) + 1

        entropy = 0.0
        code_len = len(code)

        for freq in char_freq.values():
            p = freq / code_len
            if p > 0:
                entropy -= p * math.log2(p)

        return entropy

    @classmethod
    def get_filter_group(cls, target_url="", filter_group_name=None):
        groups = getattr(Config, "CODE_FILTER_GROUPS", {}) or {}

        if filter_group_name and filter_group_name in groups:
            return filter_group_name, groups[filter_group_name]

        target_lower = (target_url or "").lower()
        for group_name, group_config in groups.items():
            if group_name == "default":
                continue
            keywords = group_config.get("url_keywords", [])
            if any(str(keyword).lower() in target_lower for keyword in keywords):
                return group_name, group_config

        return "default", groups.get("default", {})

    @staticmethod
    def get_special_chars(group_config=None):
        group_config = group_config or {}
        return str(group_config.get("special_chars") or getattr(Config, "SPECIAL_CODE_CHARS_30", ""))

    @classmethod
    def count_special_chars(cls, raw_code, group_config=None):
        special_chars = set(cls.get_special_chars(group_config))
        return sum(1 for char in str(raw_code or "") if char in special_chars)

    @staticmethod
    def is_sequential_code(code):
        if not code:
            return True

        upper = code.upper()

        if len(set(upper)) <= 2 and len(upper) >= 6:
            return True

        if len(code) >= 4:
            pattern_1 = code[:1]
            if pattern_1 and pattern_1 * len(code) == code:
                return True

            pattern_2 = code[:2]
            if len(code) % 2 == 0 and pattern_2 * (len(code) // 2) == code:
                return True

            pattern_3 = code[:3]
            if len(code) % 3 == 0 and pattern_3 * (len(code) // 3) == code:
                return True

        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        if _ALPHA_ONLY_RE.fullmatch(upper) and len(upper) >= 4:
            if upper in alphabet or upper in alphabet[::-1]:
                return True

        digits = "0123456789"
        if code.isdigit() and len(code) >= 4:
            if code in digits or code in digits[::-1]:
                return True

        return False

    @staticmethod
    def looks_like_domain_or_link(code):
        upper = code.upper()

        if upper.startswith(("HTTP", "HTTPS", "WWW", "TME", "TELEGRAM", "FACEBOOK", "TIKTOK")):
            return True

        if upper.endswith(("COM", "NET", "ORG", "VN", "APP", "INFO")) and len(upper) <= 14:
            return True

        if any(fragment in upper for fragment in ("DOTCOM", "CHAMCOM", "COMVN", "NETVN")):
            return True

        return False

    @classmethod
    def detect_site_identity(cls, clean_code):
        clean_upper = clean_code.upper()

        for site_key, prefixes in cls.SITE_ROUTING_RULES.items():
            if any(clean_upper.startswith(prefix) for prefix in prefixes):
                return site_key

        return None

    @classmethod
    def has_code_shape(cls, code, group_config=None):
        if not code:
            return False

        group_config = group_config or {}
        length = len(code)

        min_len = int(group_config.get("min_clean_length", getattr(Config, "CODE_MIN_LENGTH", 6)) or 6)
        max_len = int(group_config.get("max_clean_length", getattr(Config, "CODE_MAX_LENGTH", 15)) or 15)

        if length < min_len:
            return False

        if length > max_len:
            return False

        if bool(group_config.get("require_uppercase", False)) and code != code.upper():
            return False

        has_lower = any(c.islower() for c in code)
        has_upper = any(c.isupper() for c in code)
        has_digit = any(c.isdigit() for c in code)
        has_letter = any(c.isalpha() for c in code)
        entropy = cls.calculate_entropy(code)
        min_entropy = float(group_config.get("min_entropy", 2.3))
        uppercase_min_entropy = float(group_config.get("uppercase_min_entropy", 2.9))
        allow_numeric = bool(group_config.get("allow_numeric", True))
        allow_random_mix = bool(group_config.get("allow_random_mix", True))

        if cls.detect_site_identity(code):
            return entropy >= min_entropy and not cls.is_sequential_code(code)

        if code.isdigit():
            return allow_numeric and 8 <= length <= 12 and entropy >= 2.6 and not cls.is_sequential_code(code)

        if not has_letter:
            return False

        if has_upper and has_lower:
            return allow_random_mix and entropy >= min_entropy

        if has_letter and has_digit:
            return entropy >= min_entropy

        if code.isupper() and length >= min_len and entropy >= uppercase_min_entropy:
            return True

        return False

    _SEGMENT_WORDS_CACHE: frozenset | None = None

    @classmethod
    def is_composed_of_common_words(cls, code):
        """Chặn chuỗi chữ thuần được ghép hoàn toàn từ >=2 từ quảng cáo
        (vd "CHUCMUNG" = CHUC + MUNG). Code thật không chứa số mới cần check;
        DP O(n^2) trên chuỗi <= 15 ký tự nên gần như không tốn thời gian."""
        up = code.upper()
        if not up.isalpha():
            return False
        if cls._SEGMENT_WORDS_CACHE is None:
            cls._SEGMENT_WORDS_CACHE = frozenset(w for w in cls.COMMON_WORDS if len(w) >= 3)
        words = cls._SEGMENT_WORDS_CACHE
        n = len(up)
        best = [None] * (n + 1)  # best[i] = số từ ít nhất để ghép up[:i]
        best[0] = 0
        for i in range(1, n + 1):
            for j in range(max(0, i - 15), i):
                if best[j] is not None and up[j:i] in words:
                    cand = best[j] + 1
                    if best[i] is None or cand < best[i]:
                        best[i] = cand
        return best[n] is not None and best[n] >= 2

    @classmethod
    def is_text_word(cls, code):
        lower = code.lower()
        upper = code.upper()

        if lower in cls.VIETNAMESE_TEXT_WORDS:
            return True

        if upper in cls.COMMON_WORDS:
            return True

        # ✅ MỚI: chặn tên thương hiệu/nhà cung cấp game phổ biến hay bị
        # OCR/marker bắt nhầm gần link/quảng cáo (Golden Empire, JiLi...) —
        # so sánh không phân biệt hoa/thường nên bắt được cả "JiLi" (mixed case).
        if upper in cls.GAME_BRAND_BLACKLIST:
            return True

        # ✅ MỚI: chặn dạng "Title Case" thuần chữ không số (vd "Golden",
        # "Empire", "Mobile") — đây là kiểu viết tên riêng/tên game tiếng
        # Anh, KHÔNG phải kiểu code thật (code thật hầu như không bao giờ
        # chỉ viết hoa đúng 1 chữ cái đầu rồi toàn bộ còn lại viết thường,
        # không có số xen vào). Ngưỡng len>=4 để không đụng code ngắn hợp lệ.
        if code.isalpha() and code[:1].isupper() and code[1:].islower() and len(code) >= 4:
            return True

        if code.islower() and not any(c.isdigit() for c in code):
            return True

        if code.isupper() and not any(c.isdigit() for c in code):
            if upper in cls.COMMON_WORDS:
                return True
            if len(code) <= 5:
                return True
            if len(code) <= 7 and cls.calculate_entropy(code) < 2.4:
                return True

        return False

    # ✅ TỐI ƯU: Config.CODE_BLACKLIST là danh sách TĨNH (không đổi lúc bot
    # đang chạy) nhưng trước đây contains_blacklisted_fragment() build lại
    # thành set + upper() từng phần tử MỖI LẦN được gọi (chạy cho MỌI code).
    # Cache 1 lần duy nhất, dùng lại về sau — chỉ set None lại nếu thực sự
    # cần refresh (không có nhu cầu này trong luồng chạy bình thường của bot).
    _config_blacklist_upper_cache: frozenset | None = None

    @classmethod
    def _get_config_blacklist_upper(cls) -> frozenset:
        if cls._config_blacklist_upper_cache is None:
            cls._config_blacklist_upper_cache = frozenset(
                str(item).upper() for item in getattr(Config, "CODE_BLACKLIST", []) if str(item).strip()
            )
        return cls._config_blacklist_upper_cache

    @classmethod
    def contains_blacklisted_fragment(cls, code, group_config=None):
        group_config = group_config or {}
        code_upper = code.upper()
        config_blacklist = cls._get_config_blacklist_upper()
        group_soft_blacklist = {str(item).upper() for item in group_config.get("soft_blacklist", []) if str(item).strip()}

        soft_blacklist = cls.SOFT_BLACKLIST | group_soft_blacklist
        hard_blacklist = cls.HARD_BLACKLIST | (config_blacklist - soft_blacklist)

        for word in hard_blacklist:
            if not word:
                continue
            if word == code_upper:
                return True
            if len(word) >= 5 and word in code_upper and len(code_upper) <= len(word) + 4:
                return True

        has_digit = any(c.isdigit() for c in code)
        has_lower = any(c.islower() for c in code)
        has_upper = any(c.isupper() for c in code)

        for word in soft_blacklist:
            if not word:
                continue
            if word == code_upper:
                return True
            if word in code_upper and len(code_upper) <= len(word) + 2 and not (has_digit or (has_lower and has_upper)):
                return True

        return False

    @classmethod
    def is_site_allowed_for_group(cls, site_identity, group_config):
        if not site_identity:
            return True

        allowed_sites = group_config.get("allowed_sites", []) if group_config else []
        if not allowed_sites:
            return True

        return site_identity in {str(site).lower() for site in allowed_sites}

    @classmethod
    def is_likely_fake(cls, code, group_config=None):
        code_upper = code.upper()
        group_config = group_config or {}

        if not cls.has_code_shape(code, group_config):
            return True

        # ✅ TỐI ƯU: dùng regex đã compile sẵn (_FAKE_CODE_REGEXES) thay vì
        # re.match(fake_pattern_str, ...) — tránh compile lại/tra cache mỗi
        # lần gọi is_likely_fake() (chạy cho MỌI code, kể cả code hợp lệ).
        for fake_regex in cls._FAKE_CODE_REGEXES:
            if fake_regex.match(code_upper):
                return True

        if cls.is_sequential_code(code):
            return True

        if cls.looks_like_domain_or_link(code):
            return True

        if cls.is_text_word(code):
            return True

        if cls.is_composed_of_common_words(code):
            return True

        if cls.contains_blacklisted_fragment(code, group_config):
            return True

        for word in cls.COMMON_WORDS:
            if word not in code_upper:
                continue
            has_digit_in_code = any(c.isdigit() for c in code_upper)
            # ✅ FIX: Từ quảng cáo DÀI (>=8 ký tự) gần như KHÔNG BAO GIỜ xuất hiện
            # trong code random thật. Nếu code có số, trước đây bỏ qua, giờ chặn.
            if len(word) >= 6:
                return True
            if len(code_upper) <= len(word) + 2 and not has_digit_in_code:
                return True

        code_upper_nospace = code.upper().replace(" ", "")
        for junk in cls.OCR_JUNK_KEYWORDS:
            if junk in code_upper_nospace:
                return True

        entropy = cls.calculate_entropy(code)
        min_entropy = float(group_config.get("min_entropy", 2.3))

        return entropy < min_entropy

    # ✅ MMOO PLACEHOLDER SUPPORT

    _VIET_DIGIT_MAP = {
        "không": "0", "khong": "0",
        "một": "1", "mot": "1",
        "hai": "2",
        "ba": "3",
        "bốn": "4", "bon": "4",
        "năm": "5", "nam": "5",
        "sáu": "6", "sau": "6",
        "bảy": "7", "bay": "7",
        "tám": "8", "tam": "8",
        "chín": "9", "chin": "9",
    }

    @classmethod
    def _resolve_viet_number(cls, text: str) -> str:
        """
        Chuyển "số chín" / "số 5" / "số năm" → "9" / "5" / "5".
        Trả về chuỗi số nếu nhận ra, ngược lại trả về text gốc.
        """
        text_lower = text.strip().lower()

        m = _VIET_NUMBER_DIGITS_RE.match(text_lower)
        if m:
            return m.group(1)

        m = _VIET_NUMBER_WORD_RE.match(text_lower)
        if m:
            word = m.group(1).strip()
            if word in cls._VIET_DIGIT_MAP:
                return cls._VIET_DIGIT_MAP[word]

        if text_lower in cls._VIET_DIGIT_MAP:
            return cls._VIET_DIGIT_MAP[text_lower]

        return text

    @classmethod
    def _expand_placeholder_code(cls, code: str) -> list:
        """
        Mở rộng code có placeholder thành 1 code thực.
        Xử lý đủ 4 dạng MMOO thực tế:
        Dạng 1: ngoặc nhọn + toán học: VqbEmDM<8+1>NB3KfZVNhB → VqbEmDM9NB3KfZVNhB
        Dạng 2: ngoặc nhọn + tiếng Việt: SXRglFuErLL<số 5>u3Lzzh → SXRglFuErLL5u3Lzzh
        Dạng 3: nháy kép + tiếng Việt: zQAWwNTeBpQbnNh"số chín"hdY → zQAWwNTeBpQbnNh9hdY
        Dạng 4: code thuần: tdmYKxmcUXsHueZpNTj → [tdmYKxmcUXsHueZpNTj]
        """
        result = code

        for match in _PLACEHOLDER_ANGLE_RE.finditer(result):
            placeholder_text = match.group(1).strip()

            resolved = cls._resolve_viet_number(placeholder_text)
            if resolved != placeholder_text and resolved.isdigit():
                result = result.replace(match.group(0), resolved, 1)
                continue

            if _PLACEHOLDER_MATH_RE.match(placeholder_text):
                try:
                    computed = str(int(_safe_eval_math(placeholder_text)))
                    result = result.replace(match.group(0), computed, 1)
                except Exception:
                    pass

        for match in _PLACEHOLDER_QUOTE_RE.finditer(result):
            inner = match.group(1).strip()
            resolved = cls._resolve_viet_number(inner)
            if resolved != inner and resolved.isdigit():
                result = result.replace(match.group(0), resolved, 1)

        return [result]

    @classmethod
    def validate_code(cls, code, target_url="", filter_group_name=None, source="normal"):
        raw_code = str(code or "").strip()

        group_name, group_config = cls.get_filter_group(target_url, filter_group_name)
        enable_placeholder = bool(group_config.get("enable_placeholder_mode", False))

        has_angle = '<' in raw_code and '>' in raw_code
        has_quote_viet = bool(_VIET_QUOTE_RE.search(raw_code))

        if enable_placeholder and (has_angle or has_quote_viet):
            logger.info(f"🔧 [MMOO] Placeholder detected: {raw_code}")
            expanded_codes = cls._expand_placeholder_code(raw_code)
            expanded = expanded_codes[0] if expanded_codes else raw_code
            if expanded != raw_code:
                logger.info(f"✅ [MMOO] Expanded: {raw_code} → {expanded}")
                raw_code = expanded

        force_uppercase = bool(group_config.get("force_uppercase", False))
        clean_code = cls.clean_code(raw_code, to_upper=force_uppercase)
        special_count = cls.count_special_chars(raw_code, group_config)

        result = {
            "valid": False,
            "confidence": 0.0,
            "reason": "",
            "is_fake": False,
            "entropy": 0.0,
            "recommendation": "SKIP",
            "clean_code": clean_code,
            "raw_code": raw_code,
            "filter_group": group_name,
            "special_count": special_count,
            "source": source,
        }

        if source in ("image_ocr", "marker"):
            clean_upper_ocr = clean_code.upper()
            for word in cls.COMMON_WORDS:
                if len(word) >= 4 and word in clean_upper_ocr:
                    result["is_fake"] = True
                    tag = "OCR" if source == "image_ocr" else "MARKER"
                    result["reason"] = f"🚫 [{tag}] Nghi rác — chứa từ '{word}': {clean_code}"
                    return result

            # OCR đọc rác từ ảnh/video mờ/nhiễu thường sinh chuỗi có cùng 1
            # ký tự lặp liên tiếp 3+ lần (vd "eeeeeUnI8S") — code thật không
            # có đặc điểm này. Chặn sớm để không tốn 1 lượt submit cho rác.
            if _REPEATED_CHAR_RE.search(clean_code):
                result["is_fake"] = True
                tag = "OCR" if source == "image_ocr" else "MARKER"
                result["reason"] = (
                    f"🚫 [{tag}] Nghi rác — có ký tự lặp liên tiếp ≥3 lần: {clean_code}"
                )
                return result

        min_len = int(group_config.get("min_clean_length", getattr(Config, "CODE_MIN_LENGTH", 6)) or 6)
        max_len = int(group_config.get("max_clean_length", getattr(Config, "CODE_MAX_LENGTH", 15)) or 15)

        if len(clean_code) < min_len or len(clean_code) > max_len:
            result["reason"] = f"❌ Độ dài không hợp lệ: {len(clean_code)}"
            return result

        min_special_chars = 0
        # min_special_chars chỉ áp cho spoiler/marker (vd "KT5_H") — code từ
        # OCR gần như không bao giờ có ký tự đặc biệt nên loại trừ, tránh
        # loại oan toàn bộ code OCR hợp lệ.
        if source not in ("spoiler", "marker", "image_ocr"):
            min_special_chars = int(group_config.get("min_special_chars", 0) or 0)

        if min_special_chars > 0 and special_count < min_special_chars:
            result["reason"] = f"🚫 Không đủ dấu đặc biệt: {special_count}/{min_special_chars} ({group_name})"
            return result

        if bool(group_config.get("require_uppercase", False)) and clean_code != clean_code.upper():
            result["reason"] = f"🚫 Mã không viết hoa đúng chuẩn ({group_name})"
            return result

        target_lower = target_url.lower() if target_url else ""
        site_identity = cls.detect_site_identity(clean_code)

        if site_identity and not cls.is_site_allowed_for_group(site_identity, group_config):
            result["reason"] = f"🛡️ Code [{site_identity.upper()}] không thuộc nhóm lọc [{group_name}]"
            return result

        if site_identity and target_lower and site_identity not in target_lower:
            result["reason"] = (
                f"🛡️ CHỐNG NHẬP SAI: Code [{site_identity.upper()}] "
                f"không thuộc trang [{target_url}]"
            )
            return result

        entropy = cls.calculate_entropy(clean_code)
        result["entropy"] = round(entropy, 2)

        if cls.is_likely_fake(clean_code, group_config):
            result["is_fake"] = True
            result["reason"] = f"🚫 Nhận diện là chữ quảng cáo / link / text rác ({group_name})"
            return result

        min_entropy = float(group_config.get("min_entropy", 2.3))
        has_lower = any(c.islower() for c in clean_code)
        has_upper = any(c.isupper() for c in clean_code)
        has_digit = any(c.isdigit() for c in clean_code)

        if site_identity:
            result["valid"] = True
            result["confidence"] = 1.0
            result["reason"] = f"🌟 Code có định danh {site_identity.upper()} hợp lệ ({group_name})"
            result["recommendation"] = "SUBMIT"
            return result

        if clean_code.isdigit() and bool(group_config.get("allow_numeric", True)) and entropy >= 2.6:
            result["valid"] = True
            result["confidence"] = 0.9
            result["reason"] = f"✅ Code hợp lệ dạng toàn số ({group_name})"
            result["recommendation"] = "SUBMIT"
            return result

        if has_lower and has_upper and entropy >= min_entropy:
            result["valid"] = True
            result["confidence"] = 0.95
            result["reason"] = f"✅ Code hợp lệ dạng mix chữ hoa/thường ({group_name})"
            result["recommendation"] = "SUBMIT"
            return result

        if has_digit and entropy >= min_entropy:
            result["valid"] = True
            result["confidence"] = 0.92
            result["reason"] = f"✅ Code hợp lệ dạng có số ({group_name})"
            result["recommendation"] = "SUBMIT"
            return result

        if entropy >= float(group_config.get("uppercase_min_entropy", 2.9)):
            result["valid"] = True
            result["confidence"] = 0.85
            result["reason"] = f"✅ Code hợp lệ độ ngẫu nhiên cao ({group_name})"
            result["recommendation"] = "SUBMIT"
            return result

        result["reason"] = f"🚫 Không đủ đặc điểm code thật ({group_name})"
        return result
