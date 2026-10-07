"""
🧹 QUEUE MANAGER — Giám sát + dọn dẹp queue tự động

Mục đích: tránh hiện tượng queue đầy khiến tin nhắn/code bị drop âm thầm.

Cơ chế:
  - Vòng loop chạy mỗi QUEUE_CHECK_INTERVAL giây, kiểm tra qsize() của mọi
    queue đã đăng ký.
  - Khi queue đạt >= QUEUE_SOFT_LIMIT_PCT (mặc định 75%) → log cảnh báo
    (rate-limited: 1 lần mỗi 6 lần check ~ 30s, tránh spam). Có thể bật
    cleanup sớm để drop entry cũ nhất và giữ lại tin mới.
  - Khi queue đạt >= QUEUE_HARD_LIMIT_PCT (mặc định 95%) → drop bớt entry
    CŨ NHẤT cho tới khi về mức QUEUE_SOFT_LIMIT_PCT. Lý do drop cũ (không
    phải drop mới): giftcode/tin nhắn mới đăng còn hạn sử dụng, cái cũ có
    thể đã hết hạn — giữ cái mới hơn có giá trị hơn.

Module này KHÔNG import main_script.py — main_script.py gọi register()
để đăng ký queue vào manager, tránh circular import.
"""

import asyncio
import time
from logger_setup import logger


class QueueManager:
    def __init__(
        self,
        soft_pct: float = 0.75,
        hard_pct: float = 0.95,
        check_interval: float = 5.0,
        cleanup_on_soft_limit: bool = False,
        cleanup_target_pct: float = 0.50,
    ):
        self._queues: dict = {}
        self._stats: dict = {}
        self._on_drop: dict = {}
        self.soft_pct = max(0.10, min(0.99, float(soft_pct)))
        self.hard_pct = max(self.soft_pct + 0.01, min(1.00, float(hard_pct)))
        self.check_interval = max(1.0, float(check_interval))
        self.cleanup_on_soft_limit = bool(cleanup_on_soft_limit)
        self.cleanup_target_pct = max(0.10, min(self.soft_pct - 0.01, float(cleanup_target_pct)))
        self._stop = asyncio.Event()
        self._task = None
        self._lock = asyncio.Lock()
        self._started_at = 0.0

    def register(self, name: str, queue: asyncio.Queue, on_drop=None):
        if not isinstance(queue, asyncio.Queue):
            return
        if name in self._queues:
            return
        self._queues[name] = queue
        self._stats[name] = {"dropped": 0, "warn_count": 0, "peak": 0, "maxsize": queue.maxsize}
        self._on_drop[name] = on_drop


    async def start(self):
        if self._task is not None:
            return self
        self._stop.clear()
        self._started_at = time.time()
        self._task = asyncio.create_task(self._loop(), name="queue-manager")
        return self

    async def stop(self):
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except Exception:
                try:
                    self._task.cancel()
                except Exception:
                    pass
            self._task = None

    def snapshot(self) -> dict:
        out = {}
        for name, q in self._queues.items():
            st = self._stats.get(name, {})
            maxsize = max(1, q.maxsize)
            try:
                size = q.qsize()
            except Exception:
                size = 0
            out[name] = {
                "size": size,
                "maxsize": q.maxsize,
                "pct": round(size / maxsize * 100, 1),
                "dropped": st.get("dropped", 0),
                "peak": st.get("peak", 0),
                "warn_count": st.get("warn_count", 0),
            }
        return out

    async def _loop(self):
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=2.0)
            return
        except asyncio.TimeoutError:
            pass
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.check_interval)
                break
            except asyncio.TimeoutError:
                pass
            try:
                await self._check_all()
            except Exception as e:
                logger.debug(f"⚠️ [QueueMgr] check error: {e}")

    async def _check_all(self):
        async with self._lock:
            for name, q in list(self._queues.items()):
                st = self._stats.setdefault(name, {"dropped": 0, "warn_count": 0, "peak": 0, "maxsize": q.maxsize})
                try:
                    size = q.qsize()
                except Exception:
                    continue
                if size > st["peak"]:
                    st["peak"] = size
                maxsize = q.maxsize
                if maxsize <= 0:
                    continue
                ratio = size / maxsize
                if ratio >= self.hard_pct:
                    # Message ingress có DurableInbox làm nguồn bền vững;
                    # tuyệt đối không drop item để ưu tiên tin mới, nếu không
                    # sẽ phá FIFO khi channel phát dồn. Worker sẽ tự giải
                    # phóng slot và inbox-drain sẽ nạp tiếp các row pending.
                    if name == "message":
                        if st["warn_count"] % 3 == 0:
                            logger.warning(
                                "⚠️ [QueueMgr] message queue đầy (%s/%s) — giữ FIFO, không drop; durable inbox sẽ drain tiếp",
                                size,
                                maxsize,
                            )
                        st["warn_count"] += 1
                        continue
                    target = int(maxsize * self.soft_pct)
                    dropped = self._drop_oldest(name, q, target)
                    if dropped > 0:
                        st["dropped"] += dropped
                        logger.warning(
                            f"🧹 [QueueMgr] '{name}' đầy ({size}/{maxsize} = {ratio*100:.0f}%) "
                            f"→ đã drop {dropped} entry cũ nhất (còn {q.qsize()})"
                        )
                elif ratio >= self.soft_pct:
                    st["warn_count"] += 1
                    if self.cleanup_on_soft_limit:
                        target = int(maxsize * self.cleanup_target_pct)
                        dropped = self._drop_oldest(name, q, target)
                        if dropped > 0:
                            st["dropped"] += dropped
                            logger.warning(
                                f"🧹 [QueueMgr] '{name}' đạt soft limit "
                                f"({size}/{maxsize} = {ratio*100:.0f}%) "
                                f"→ drop {dropped} entry cũ, giữ tin mới "
                                f"(còn {q.qsize()})"
                            )
                            continue
                    if st["warn_count"] % 6 == 1:
                        logger.warning(
                            f"⚠️ [QueueMgr] '{name}' gần đầy ({size}/{maxsize} = {ratio*100:.0f}%) "
                            f"— cân nhắc tăng MESSAGE_WORKERS / MAX_CONCURRENT_SUBMITS_PER_DOMAIN"
                        )

    def _drop_oldest(self, name: str, q: asyncio.Queue, target_size: int) -> int:
        on_drop = self._on_drop.get(name)
        dropped = 0
        guard = 0
        while q.qsize() > target_size and guard < 10000:
            guard += 1
            try:
                item = q.get_nowait()
                if on_drop is not None:
                    try:
                        on_drop(item)
                    except Exception:
                        logger.debug("⚠️ [QueueMgr] on_drop lỗi cho '%s'", name, exc_info=True)
                try:
                    q.task_done()
                except Exception:
                    pass
                dropped += 1
            except asyncio.QueueEmpty:
                break
        return dropped


_queue_manager: "QueueManager | None" = None


def init_queue_manager(
    soft_pct: float = 0.75,
    hard_pct: float = 0.95,
    check_interval: float = 5.0,
    cleanup_on_soft_limit: bool = False,
    cleanup_target_pct: float = 0.50,
) -> QueueManager:
    global _queue_manager
    if _queue_manager is None:
        _queue_manager = QueueManager(
            soft_pct,
            hard_pct,
            check_interval,
            cleanup_on_soft_limit,
            cleanup_target_pct,
        )
    return _queue_manager


def get_queue_manager() -> "QueueManager | None":
    return _queue_manager
