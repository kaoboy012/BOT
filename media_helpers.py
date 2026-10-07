"""
📸 SHARED MEDIA / SCREENSHOT HELPERS

Trước đây take_result_screenshot() được viết RIÊNG ở 2 nơi:
  - main_script.py         → chữ ký (page, user, code, target_url, status, debug_info=None)
  - submission_outcomes.py → chữ ký (page, user, code, domain, status, debug_info=None)
Hai bản gần giống nhau nhưng KHÁC tham số thứ 4 (target_url vs domain) —
dễ lệch hành vi khi sửa 1 bên mà quên sửa bên kia (đã xảy ra: bản trong
submission_outcomes.py thiếu phần lưu kèm HTML snapshot có ở bản
main_script.py). Giờ gộp về 1 bản DUY NHẤT ở đây — main_script.py và
submission_outcomes.py đều import từ module này, không ai tự định nghĩa
riêng nữa.

Module này CỐ TÌNH không import gì từ main_script.py / submission_outcomes.py
để tránh mọi nguy cơ import vòng — chỉ phụ thuộc config.py và logger_setup.py
(2 module gốc, không phụ thuộc ngược lại module nào khác trong dự án).
"""
from __future__ import annotations

import json
import re
import time
from uuid import uuid4
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from config import Config
from logger_setup import logger


def normalize_domain(url: str) -> str:
    """Bản normalize_domain RÚT GỌN, độc lập — main_script.py vẫn giữ bản
    đầy đủ của riêng nó (dùng ở nhiều chỗ khác), bản này chỉ phục vụ đặt
    tên file screenshot, không cần import chéo main_script.py."""
    parsed = urlparse(url or "")
    domain = parsed.netloc or parsed.path
    return domain.lower().replace("www.", "").strip("/")


# Chụp ảnh + lưu HTML trong lúc tab đang bị giữ tốn ~1s mỗi lần. Khi nhiều
# lượt liên tiếp ra "No popup", việc này làm submit chậm dần và đầy ổ đĩa. Chỉ
# chụp tối đa 1 lần / SCREENSHOT_MIN_INTERVAL_SECONDS cho mỗi domain+trạng thái,
# và giữ tối đa SCREENSHOT_MAX_FILES file mới nhất.
_LAST_SHOT_AT: dict = {}


def _screenshot_throttled(domain: str, status: str) -> bool:
    interval = float(getattr(Config, "SCREENSHOT_MIN_INTERVAL_SECONDS", 60.0))
    if interval <= 0:
        return False
    key = (domain, status)
    now = time.monotonic()
    last = _LAST_SHOT_AT.get(key)
    if last is not None and now - last < interval:
        return True
    _LAST_SHOT_AT[key] = now
    return False


def _prune_screenshots(shot_dir) -> None:
    limit = int(getattr(Config, "SCREENSHOT_MAX_FILES", 300))
    if limit <= 0:
        return
    try:
        files = sorted(
            (f for f in shot_dir.iterdir() if f.is_file()),
            key=lambda f: f.stat().st_mtime,
        )
        for f in files[: max(0, len(files) - limit)]:
            try:
                f.unlink()
            except OSError:
                pass
    except Exception:
        pass


def _safe_artifact_part(value: str, fallback: str = "unknown", limit: int = 80) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "")).strip("._-")
    return (cleaned or fallback)[:limit]


async def take_result_screenshot(
    page,
    user: str,
    code: str,
    target_url: str,
    status: str,
    debug_info: dict | None = None,
) -> str:
    """Chụp ảnh trang + lưu kèm HTML snapshot + (nếu có) file debug JSON,
    dùng khi kết quả submit không rõ ràng (SCREENSHOT_ON_UNKNOWN=true trong
    .env). Trả về đường dẫn ảnh .png, hoặc "" nếu tắt tính năng / lỗi.

    Dùng CHUNG cho cả main_script.py lẫn submission_outcomes.py — sửa 1
    chỗ, cả 2 nơi cùng nhận thay đổi, không còn 2 bản lệch nhau."""
    enabled = "SCREENSHOT_ON_FAILED" if status == "FAILED" else "SCREENSHOT_ON_UNKNOWN"
    if not bool(getattr(Config, enabled, False)):
        return ""
    try:
        if _screenshot_throttled(normalize_domain(target_url), str(status)):
            return ""
        shot_dir = Path("logs/screenshots")
        shot_dir.mkdir(parents=True, exist_ok=True)
        safe_domain = _safe_artifact_part(normalize_domain(target_url).replace(".", "_"))
        safe_user = _safe_artifact_part(user)
        safe_code = _safe_artifact_part(code)
        safe_status = _safe_artifact_part(status)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        base_name = f"{safe_domain}_{safe_user}_{safe_code}_{safe_status}_{ts}_{uuid4().hex[:8]}"
        path = shot_dir / f"{base_name}.png"
        await page.screenshot(path=str(path), full_page=False)

        try:
            html = await page.content()
            html_path = shot_dir / f"{base_name}.html"
            html_path.write_text(html, encoding="utf-8")
        except Exception as e:
            logger.debug(f"⚠️ Cannot save page HTML: {e}")

        if debug_info:
            try:
                debug_path = shot_dir / f"{base_name}.debug.json"
                debug_path.write_text(
                    json.dumps(debug_info, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )
            except Exception as e:
                logger.debug(f"⚠️ Cannot save debug JSON: {e}")

        _prune_screenshots(shot_dir)
        return str(path)
    except Exception as e:
        logger.debug(f"⚠️ Cannot take screenshot: {e}")
        return ""