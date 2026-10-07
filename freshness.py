"""Kiểm tra độ "tươi" của tin nhắn / code trước khi chiếm tab trình duyệt.

Giftcode hết hạn rất nhanh. Các domain chỉ có 1 tab (QQ88, HI88) xử lý tuần
tự, mỗi mã mất vài giây (result timeout 3.5-4.5s). Nếu hàng đợi đang giữ mã
đã cũ thì mã mới phải chờ sau chúng. Module này cho phép bỏ qua mã quá cũ
ngay khi lấy ra khỏi hàng đợi, trước khi tốn tab/mạng.

``max_age <= 0`` nghĩa là tắt kiểm tra (giữ hành vi cũ).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _to_utc(value: Any) -> datetime | None:
    """Chuyển datetime / ISO-8601 / 'YYYY-MM-DD HH:MM:SS' (UTC) về datetime UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def age_seconds(value: Any, now: datetime | None = None) -> float | None:
    """Tuổi (giây) của mốc thời gian ``value``; None nếu không đọc được."""
    dt = _to_utc(value)
    if dt is None:
        return None
    ref = now or datetime.now(timezone.utc)
    return max(0.0, (ref - dt).total_seconds())


def is_stale(value: Any, max_age: float, now: datetime | None = None) -> tuple[bool, float | None]:
    """Trả về (đã_quá_cũ, tuổi_giây). Không đọc được mốc thời gian → không stale."""
    if max_age is None or float(max_age) <= 0:
        return False, None
    age = age_seconds(value, now)
    if age is None:
        return False, None
    return age > float(max_age), age


def first_known_age(candidates, max_age: float, now: datetime | None = None) -> tuple[bool, float | None]:
    """Dùng mốc thời gian đầu tiên đọc được trong ``candidates`` (ưu tiên giờ
    đăng tin của Telegram, rồi tới giờ tạo row/item trong DB)."""
    for value in candidates:
        stale, age = is_stale(value, max_age, now)
        if age is not None:
            return stale, age
    return False, None
