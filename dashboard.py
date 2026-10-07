"""
📊 DASHBOARD HIỂN THỊ KẾT QUẢ SĂN CODE (RICH LIVE TABLE) - PHIÊN BẢN TỐI ƯU GIAO DIỆN
Khắc phục triệt để:
  1. Loại bỏ hoàn toàn lỗi chữ rớt dọc từng ký tự (C-l-o-s-e / đ-ã / s-ử / d-ụ-n-g...).
  2. Cột "Trang" và "Giftcode" hiển thị đầy đủ, không bị cắt đuôi (xx88code....).
  3. Cột "Trạng Thái" và "Latency phases" được khóa kích thước chuẩn, không chiếm dụng khoảng trống vô ích.
  4. Cột "Phản Hồi RAW" nhận toàn bộ diện tích còn lại của màn hình, tự động lọc text rác modal (Close / popup).
  5. Tự động co giãn theo chiều cao terminal thực tế, đảm bảo không bao giờ bị cuộn giật màn hình.
"""

import logging
import threading
from collections import deque
from typing import Optional

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.text import Text

# Nhịp làm mới giao diện nền (8 khung hình/giây)
REFRESH_INTERVAL = 1.0 / 8
_dirty = False
_flush_thread: Optional[threading.Thread] = None
_flush_stop = threading.Event()

_lock = threading.Lock()
# Lưu trữ lịch sử 100 dòng kết quả gần nhất trong bộ nhớ
_rows: deque = deque(maxlen=100)
_stats = {
    "success": 0,
    "failed": 0,
    "total": 0,
    "last_telegram_rtt_ms": None,
    "last_concurrent": 0,
    "last_concurrent_elapsed_ms": None,
    "server_rtts_ms": deque(maxlen=200),
    "timing_records": 0,
    "download_active": 0,
    "download_completed": 0,
    "download_dedup": 0,
    "last_download_ms": None,
    "last_download_mb_s": None,
    "last_download_size_mb": None,
    "last_download_attempts": None,
}
_live: Optional[Live] = None
_console: Optional[Console] = None
_row_id = 0

def disable_console_logging():
    """Tắt log stdout để tránh xung đột ghi đè luồng giao diện Rich.Live."""
    logger = logging.getLogger("bot_logger")
    for h in list(logger.handlers):
        if isinstance(h, logging.StreamHandler) and not isinstance(
            h, logging.FileHandler
        ):
            logger.removeHandler(h)


def _fmt_ms(ms) -> str:
    """Định dạng milli-giây thành chuỗi ngắn gọn dễ đọc."""
    if ms is None:
        return "-"
    try:
        return f"{float(ms):.0f}ms"
    except Exception:
        return "-"

def _format_latency(latency: Optional[dict]) -> str:
    """Format rút gọn các phase độ trễ để tiết kiệm không gian hàng."""
    if not latency:
        return "-"
    # Chỉ giữ 3 số chẩn đoán nhất (vừa cột 32 ký tự): chờ tab và gap
    # click→POST (chỉ hiện khi ≥100ms: tranh chấp tab / Turnstile đang giải), API thật (nếu đo được, không thì thời
    # gian chờ kết quả) và tổng. Đầy đủ các pha nằm trong log [METRIC] submit.
    def _num(key):
        try:
            v = latency.get(key)
            return None if v is None else float(v)
        except (TypeError, ValueError):
            return None

    parts = []
    tab = _num("tab_wait_ms")
    if tab is not None and tab >= 100:
        parts.append(f"tab{tab:.0f}")
    gap = _num("pre_api_ms")
    if gap is not None and gap >= 100:
        parts.append(f"gap{gap:.0f}")
    api = _num("api_ms")
    if api is not None:
        parts.append(f"api{api:.0f}")
    else:
        res = _num("result_wait_ms")
        if res is not None:
            parts.append(f"res{res:.0f}")
    tot = _num("total_submit_ms")
    if tot is not None:
        parts.append(f"tot{tot:.0f}")
    return " ".join(parts) or "-"

def _status_style(status: str):
    """Định dạng màu sắc và biểu tượng cho trạng thái submit."""
    upper = (status or "").upper()
    if "THÀNH CÔNG" in upper or "SUCCESS" in upper:
        return "bold green", f"✓ {status}"
    if any(k in upper for k in ("THẤT BẠI", "FAILED", "SAI", "HẾT HẠN", "LỖI")):
        return "bold red", f"✗ {status}"
    return "bold yellow", f"? {status}"

def _build_renderable():
    """Tạo giao diện hoàn chỉnh gồm Panel Thống kê và Bảng kết quả thích ứng."""
    with _lock:
        stats = dict(_stats)
        stats["server_rtts_ms"] = list(_stats["server_rtts_ms"])
        rows_snapshot = list(_rows)

    server_rtts = stats["server_rtts_ms"]
    avg_server_rtt = (sum(server_rtts) / len(server_rtts)) if server_rtts else None

    # Header dòng 1: RTT & Concurrency
    header1 = Text()
    header1.append("⚡ RTT Telegram: ", style="bold")
    header1.append(_fmt_ms(stats["last_telegram_rtt_ms"]), style="cyan")
    header1.append("   |   ", style="dim")
    header1.append("🚀 Bắn ", style="bold")
    header1.append(f"{stats['last_concurrent']}", style="bold yellow")
    header1.append(" nick đồng thời: ", style="bold")
    header1.append(_fmt_ms(stats["last_concurrent_elapsed_ms"]), style="cyan")
    header1.append("   |   ", style="dim")
    header1.append("📡 RTT Server TB: ", style="bold")
    header1.append(_fmt_ms(avg_server_rtt), style="magenta")

    # Header dòng 2: Thống kê Thành công / Thất bại
    header2 = Text()
    header2.append("📊 Thống kê: ", style="bold")
    header2.append(f"{stats['success']} Thành công", style="bold green")
    header2.append(" / ", style="dim")
    header2.append(f"{stats['failed']} Thất bại", style="bold red")
    header2.append(f"   (Tổng {stats['total']} lượt submit)", style="dim")

    # Header dòng 3: Tốc độ tải media & ảnh OCR
    header3 = Text()
    header3.append("⬇ Download: ", style="bold")
    header3.append(f"{stats['download_completed']} xong", style="green")
    header3.append(f" / {stats['download_dedup']} dedup", style="yellow")
    if stats["last_download_mb_s"] is not None:
        header3.append(
            f" | gần nhất {stats['last_download_mb_s']:.2f} MB/s, "
            f"{stats['last_download_size_mb']:.2f} MB, {stats['last_download_ms']:.0f}ms",
            style="cyan",
        )
    header3.append(f" | đang tải={stats['download_active']}", style="bold yellow")
    header3.append(f" | timing={stats['timing_records']}", style="dim")

    # Cấu hình bảng hiển thị với phân bổ kích thước cột nghiêm ngặt
    table = Table(
        show_header=True,
        header_style="bold white on blue",
        expand=True,
        box=box.ROUNDED,
        pad_edge=False,
    )

    # 1. Cột Số thứ tự: cố định 4 ký tự
    table.add_column("#", justify="right", style="dim", width=4, no_wrap=True)

    # 2. Cột Trang: Đủ rộng (14 ký tự) cho xx88code.com, gg88live.vip mà không bị '....'
    table.add_column("Trang", style="cyan", width=14, no_wrap=True)

    # 3. Cột Tài Khoản: cố định 12 ký tự
    table.add_column("Tài Khoản", style="bright_cyan", width=12, no_wrap=True)

    # 4. Cột Giftcode: luôn hiển thị nguyên vẹn mã code
    table.add_column("Giftcode", style="magenta bold", width=12, no_wrap=True)

    # 5. Cột Trạng Thái: giới hạn chuẩn 14 ký tự, tuyệt đối không cho phình to lấn sân
    table.add_column("Trạng Thái", justify="center", width=14, no_wrap=True)

    # 6. Cột RTT API: cố định 8 ký tự
    table.add_column("RTT API", justify="right", width=8, style="yellow", no_wrap=True)

    # 7. Cột Latency phases (32 ký tự). Chỉ hiện khi terminal đủ rộng: ở ~115
    # cột, tổng cột cố định + viền đã ~100 → cột RAW chỉ còn ~14 < min_width 25
    # khiến bảng tràn/vỡ dòng. Terminal hẹp → ẩn cột này, ưu tiên cột RAW.
    term_width = _console.size.width if _console else 120
    show_latency = term_width >= 155
    if show_latency:
        table.add_column("Latency phases", style="dim", width=32, no_wrap=True, overflow="ellipsis")

    # 8. Cột Phản Hồi RAW: DÀNH TOÀN BỘ PHẦN CÒN LẠI CỦA MÀN HÌNH CHO CỘT NÀY
    table.add_column("Phản Hồi RAW", style="white", min_width=25, ratio=1, no_wrap=True, overflow="ellipsis")

    # Tự động tính số dòng hiển thị dựa trên chiều cao terminal để chống cuộn màn hình
    term_height = _console.size.height if _console else 30
    # Trừ 9 dòng dành cho Header Panel và khung bảng
    max_visible_rows = max(5, term_height - 9)
    rows_to_render = rows_snapshot[-max_visible_rows:]

    for row in rows_to_render:
        style, display = _status_style(row["status"])
        cells = [
            str(row["id"]),
            row["domain"],
            row["account"],
            row["code"],
            f"[{style}]{display}[/{style}]",
            row["rtt"],
        ]
        if show_latency:
            cells.append(row.get("latency", "-"))
        cells.append(row["raw"])
        table.add_row(*cells)

    header_panel = Panel(
        Group(header1, header2, header3),
        border_style="cyan",
        title="🎯 OCR Hunter",
        title_align="center",
        expand=True,
    )
    return Group(header_panel, table)

def _flush_loop():
    """Thread nền thực hiện render giao diện theo chu kỳ cố định."""
    global _dirty
    while not _flush_stop.wait(REFRESH_INTERVAL):
        if _live is None:
            continue
        with _lock:
            should_flush = _dirty
            _dirty = False
        if not should_flush:
            continue
        try:
            _live.update(_build_renderable(), refresh=True)
        except Exception:
            pass

def start_dashboard():
    """Khởi động Live dashboard."""
    global _live, _console, _flush_thread
    if _live is not None:
        return
    _console = Console()
    _live = Live(
        _build_renderable(), console=_console, auto_refresh=False, screen=False
    )
    _live.start(refresh=True)

    _flush_stop.clear()
    _flush_thread = threading.Thread(target=_flush_loop, name="dashboard-flush", daemon=True)
    _flush_thread.start()


def stop_dashboard():
    """Dừng Live dashboard an toàn khi thoát bot."""
    global _live, _flush_thread
    _flush_stop.set()
    if _flush_thread is not None:
        try:
            _flush_thread.join(timeout=1.0)
        except Exception:
            pass
        _flush_thread = None
    if _live:
        try:
            _live.update(_build_renderable(), refresh=True)
            _live.stop()
        except Exception:
            pass
        _live = None


def _refresh():
    """Đánh dấu có dữ liệu mới để thread nền flush."""
    global _dirty
    if _live is None:
        return
    with _lock:
        _dirty = True

def update_dashboard(
    domain: str,
    account: str,
    code: str,
    status: str,
    rtt_ms: Optional[float] = None,
    raw_response: str = "",
    telegram_rtt_ms: Optional[float] = None,
    latency_ms: Optional[dict] = None,
):
    """Cập nhật dữ liệu lượt submit mới vào bảng."""
    global _row_id
    if _live is None:
        return

    # 1. Chuẩn hóa: loại bỏ triệt để ký tự xuống dòng (\n, \r) làm vỡ bảng
    clean_raw = " ".join((raw_response or "").replace("\r", " ").replace("\n", " ").split())
    
    # 2. Làm sạch các text rác từ nút bấm modal giao diện web (Close, No popup Close)
    for noise in ("No popup Close", "popup Close", "Close"):
        if len(clean_raw) > len(noise) and noise in clean_raw:
            clean_raw = clean_raw.replace(noise, "").strip()
            
    raw = " ".join(clean_raw.split()) if clean_raw else "-"
    if len(raw) > 160:
        raw = raw[:160] + "..."

    def _bucket(value: str) -> str:
        upper = (value or "").upper()
        if "THÀNH CÔNG" in upper or "SUCCESS" in upper:
            return "success"
        if "THẤT BẠI" in upper or "FAILED" in upper:
            return "failed"
        return "other"

    with _lock:
        existing = next(
            (
                row for row in _rows
                if row["domain"] == domain
                and row["account"] == account
                and row["code"] == code
            ),
            None,
        )
        if existing is None:
            _row_id += 1
            _rows.append(
                {
                    "id": _row_id,
                    "domain": domain,
                    "account": account,
                    "code": code,
                    "status": status,
                    "rtt": _fmt_ms(rtt_ms),
                    "raw": raw,
                    "latency": _format_latency(latency_ms),
                }
            )
            _stats["total"] += 1
            old_bucket = "other"
        else:
            old_bucket = _bucket(existing.get("status", ""))
            existing.update(
                status=status,
                rtt=_fmt_ms(rtt_ms),
                raw=raw,
                latency=_format_latency(latency_ms),
            )

        new_bucket = _bucket(status)
        if old_bucket in ("success", "failed"):
            _stats[old_bucket] = max(0, _stats[old_bucket] - 1)
        if new_bucket in ("success", "failed"):
            _stats[new_bucket] += 1

        if rtt_ms is not None:
            _stats["server_rtts_ms"].append(rtt_ms)
        if telegram_rtt_ms is not None:
            _stats["last_telegram_rtt_ms"] = telegram_rtt_ms

    _refresh()

def update_latency(domain: str, account: str, code: str, latency_ms: Optional[dict]) -> None:
    """Chỉ cập nhật cột 'Latency phases' của dòng đã có (không đụng thống kê/RTT TB)."""
    if _live is None:
        return
    text = _format_latency(latency_ms)
    with _lock:
        for row in reversed(_rows):
            if row["domain"] == domain and row["account"] == account and row["code"] == code:
                row["latency"] = text
                break
    _refresh()


def report_download_started():
    with _lock:
        _stats["download_active"] += 1
    _refresh()


def report_download_finished():
    with _lock:
        _stats["download_active"] = max(0, _stats["download_active"] - 1)
    _refresh()


def report_timing_record(record: dict):
    """Cập nhật thống kê hiệu năng tải media."""
    with _lock:
        _stats["timing_records"] += 1
        if record.get("media"):
            _stats["download_completed"] += 1
            if record.get("download_dedup_hit"):
                _stats["download_dedup"] += 1
            elapsed = record.get("download_elapsed_ms")
            size = record.get("file_size_bytes")
            speed = record.get("download_bytes_per_sec")
            _stats["last_download_ms"] = float(elapsed) if elapsed is not None else None
            _stats["last_download_mb_s"] = float(speed) / 1_000_000 if speed is not None else None
            _stats["last_download_size_mb"] = float(size) / 1_000_000 if size is not None else None
            _stats["last_download_attempts"] = record.get("download_attempts")
    _refresh()


def get_dashboard_snapshot() -> dict:
    """Trả về dữ liệu thống kê hiện tại phục vụ API hoặc bot Telegram."""
    with _lock:
        return {
            "success": _stats["success"],
            "failed": _stats["failed"],
            "total": _stats["total"],
            "download_active": _stats["download_active"],
            "download_completed": _stats["download_completed"],
            "rows_tracked": len(_rows),
        }



