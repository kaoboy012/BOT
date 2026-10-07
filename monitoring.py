"""Lightweight CPU/RAM health and task performance monitoring."""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover
    psutil = None

from logger_setup import logger

_health_monitor = None
_perf_monitor = None


class HealthMonitor:
    def __init__(
        self,
        cpu_threshold: int = 85,
        interval: float = 120.0,
        recovery_margin: float = 10.0,
        ram_threshold: int = 90,
        ram_warn_threshold: int = 85,
        breach_confirm_count: int = 2,
    ):
        self.cpu_threshold = max(1, min(100, int(cpu_threshold)))
        # Sàn tối thiểu 5.0s — quét CPU/RAM dày hơn không có giá trị thêm,
        # chỉ tốn tài nguyên; mục tiêu là phát hiện nghẽn kéo dài, không
        # phải spike tức thời.
        self.interval = max(5.0, float(interval))
        # Chỉ pause/báo động khi CPU/RAM vượt ngưỡng ở >= breach_confirm_count
        # lần đo LIÊN TIẾP (chống báo động giả do spike ngắn hạn). Hồi phục
        # (tắt pause) vẫn phản ứng ngay, không cần xác nhận nhiều lần.
        self.breach_confirm_count = max(1, int(breach_confirm_count))
        self._consecutive_breaches = 0
        # Hysteresis: chỉ tắt pause khi CPU hạ xuống dưới (ngưỡng - margin),
        # tránh bật/tắt liên tục khi CPU dao động sát biên.
        self.recovery_threshold = max(1, self.cpu_threshold - max(0, recovery_margin))
        self.ram_threshold = max(1, min(100, int(ram_threshold)))
        self.ram_warn_threshold = max(1, min(100, int(ram_warn_threshold)))
        self.ram_recovery_threshold = max(1, self.ram_threshold - max(0, recovery_margin))
        self._ram_warned_recently = False
        self.pause_ocr = False
        self.pause_reason = ""
        self.last_cpu = 0.0
        self.last_memory = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        # ✅ MỚI: callback dọn dẹp KHẨN CẤP khi RAM chạm ngưỡng pause — đăng
        # ký qua set_high_memory_callback(). Chạy trên THREAD RIÊNG của
        # HealthMonitor (không phải asyncio event loop), nên callback phải
        # là hàm SYNC, không được await bất cứ gì.
        self.on_high_memory_callback = None
        self._last_high_memory_cleanup = 0.0
        self._high_memory_cleanup_cooldown = 60.0  # tối thiểu 60s giữa 2 lần dọn

    def set_high_memory_callback(self, callback):
        """Đăng ký hàm dọn dẹp SYNC (không async) — gọi khi RAM chạm
        ngưỡng pause. HealthMonitor chạy trên thread riêng, không phải
        asyncio event loop, nên callback không được await bất cứ gì."""
        self.on_high_memory_callback = callback

    def _sample(self):
        if psutil is None:
            return 0.0, 0.0
        return float(psutil.cpu_percent(interval=None)), float(psutil.virtual_memory().percent)

    def _loop(self):
        if psutil is not None:
            psutil.cpu_percent(interval=None)  # lần gọi đầu luôn trả 0.0, bỏ qua
        while not self._stop.wait(self.interval):
            try:
                cpu, mem = self._sample()
                with self._lock:
                    self.last_cpu, self.last_memory = cpu, mem

                    cpu_over_raw = cpu >= self.cpu_threshold
                    ram_over_raw = mem >= self.ram_threshold
                    cpu_recovered = cpu <= self.recovery_threshold
                    ram_recovered = mem <= self.ram_recovery_threshold

                    # ✅ MỚI: đếm dồn số lần đo LIÊN TIẾP vượt ngưỡng — chỉ
                    # coi là "over" thật (đủ để pause/báo động) khi đạt
                    # breach_confirm_count lần liên tiếp. Bất kỳ lần đo nào
                    # KHÔNG vượt ngưỡng sẽ reset bộ đếm về 0 ngay (phải vượt
                    # liên tục, không cộng dồn rải rác).
                    if cpu_over_raw or ram_over_raw:
                        self._consecutive_breaches += 1
                    else:
                        self._consecutive_breaches = 0
                    confirmed = self._consecutive_breaches >= self.breach_confirm_count
                    cpu_over = cpu_over_raw and confirmed
                    ram_over = ram_over_raw and confirmed

                    # ✅ MỚI: RAM chạm ngưỡng pause (đã XÁC NHẬN, không phải
                    # spike đơn lẻ) → gọi callback dọn dẹp khẩn cấp (nếu có
                    # đăng ký), cách nhau tối thiểu _high_memory_cleanup_cooldown
                    # giây để không spam dọn liên tục khi RAM cứ lảng vảng
                    # quanh ngưỡng.
                    if ram_over and self.on_high_memory_callback is not None:
                        now_ts = time.time()
                        if now_ts - self._last_high_memory_cleanup >= self._high_memory_cleanup_cooldown:
                            self._last_high_memory_cleanup = now_ts
                            try:
                                logger.warning("🧹 [Health] RAM cao → chạy dọn dẹp khẩn cấp...")
                                self.on_high_memory_callback()
                            except Exception as exc:
                                logger.debug(f"⚠️ high_memory_callback error: {exc}")

                    if not self.pause_ocr and (cpu_over or ram_over):
                        self.pause_ocr = True
                        self.pause_reason = "CPU" if cpu_over else "RAM"
                        if cpu_over and ram_over:
                            self.pause_reason = "CPU+RAM"
                        logger.warning(
                            "⚠️ [Health] OCR paused (lý do=%s | CPU %.1f%%, RAM %.1f%%)",
                            self.pause_reason, cpu, mem,
                        )
                    elif self.pause_ocr and cpu_recovered and ram_recovered:
                        self.pause_ocr = False
                        self.pause_reason = ""
                        logger.warning(
                            "✅ [Health] OCR resumed (CPU %.1f%%, RAM %.1f%%)", cpu, mem
                        )

                    # ✅ MỚI: cảnh báo SỚM (không pause) khi RAM chạm ngưỡng
                    # cảnh báo nhưng chưa tới ngưỡng pause — giúp phát hiện xu
                    # hướng RAM tăng dần trước khi thực sự phải dừng OCR.
                    # Chỉ log 1 lần mỗi lần vượt ngưỡng (không lặp lại mỗi
                    # interval) để tránh spam log.
                    if mem >= self.ram_warn_threshold and not ram_over:
                        if not self._ram_warned_recently:
                            logger.warning(
                                "⚠️ RAM cao: %.1f%% — cân nhắc giảm TAB_POOL_SIZE / "
                                "MAX_TAB_PER_DOMAIN_CAP / MAX_CONCURRENT_* trong .env "
                                "nếu tình trạng này lặp lại thường xuyên",
                                mem,
                            )
                            self._ram_warned_recently = True
                    elif mem < self.ram_warn_threshold - 3:
                        # Hồi phục rõ ràng (có biên hysteresis nhỏ) → cho phép cảnh báo lại lần sau
                        self._ram_warned_recently = False
            except Exception as exc:
                logger.debug("Health monitor error: %s", exc)

    def start(self):
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="health-monitor", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "cpu_percent": self.last_cpu,
                "memory_percent": self.last_memory,
                "pause_ocr": self.pause_ocr,
                "pause_reason": self.pause_reason,
            }


@dataclass
class TaskMetric:
    name: str
    elapsed: float
    success: bool
    timestamp: float


class PerformanceMonitor:
    def __init__(self, max_samples: int = 500):
        self.samples = deque(maxlen=max_samples)
        self._lock = threading.Lock()

    def record_task(self, name: str, elapsed: float, success: bool):
        with self._lock:
            self.samples.append(TaskMetric(name, float(elapsed), bool(success), time.time()))

    def snapshot(self) -> dict:
        with self._lock:
            rows = list(self.samples)
        if not rows:
            return {"count": 0, "success_rate": 0.0, "avg_elapsed": 0.0}
        return {
            "count": len(rows),
            "success_rate": sum(r.success for r in rows) / len(rows),
            "avg_elapsed": sum(r.elapsed for r in rows) / len(rows),
        }


def init_monitoring():
    # _perf_monitor là singleton module-level (như _health_monitor) — tránh
    # mất số liệu tích lũy nếu init_monitoring() lỡ được gọi lại lần 2.
    global _health_monitor, _perf_monitor
    if _health_monitor is None:
        from config import Config
        _health_monitor = HealthMonitor(
            cpu_threshold=getattr(Config, "OCR_CPU_THRESHOLD", 85),
            interval=getattr(Config, "HEALTH_CHECK_INTERVAL", 120.0),
            ram_threshold=getattr(Config, "OCR_RAM_PAUSE_THRESHOLD", 90),
            ram_warn_threshold=getattr(Config, "OCR_RAM_WARN_THRESHOLD", 85),
            # ✅ MỚI: HEALTH_BREACH_CONFIRM_COUNT trong .env (mặc định 2) —
            # số lần đo LIÊN TIẾP vượt ngưỡng trước khi thực sự pause/báo
            # động (xem giải thích ở HealthMonitor.__init__).
            breach_confirm_count=getattr(Config, "HEALTH_BREACH_CONFIRM_COUNT", 2),
        )
        if getattr(Config, "ENABLE_MONITORING", True):
            _health_monitor.start()
    if _perf_monitor is None:
        _perf_monitor = PerformanceMonitor()
    return _health_monitor, _perf_monitor


def stop_monitoring() -> None:
    """Stop the singleton health monitor if it was started."""
    if _health_monitor is not None:
        _health_monitor.stop()
