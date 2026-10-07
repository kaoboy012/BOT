"""Đường nhận tin Telegram — MỘT chủ sở hữu, MỘT luồng đi.

    push (NewMessage / MessageEdited) ─┐
                                        ├─> TelegramReceiver.offer() ─ loại trùng ─> sink
    poll (GetPeerDialogs, TUẦN TỰ) ─────┘

    sink = IngressStage.put() ─> gom lô ─> 1 commit SQLite ─> on_row() ─> message_queue

Quy tắc thiết kế (thay cho các bản vá poll/ingress cũ):

1. Mọi lệnh gọi Telegram để *đọc tin kênh* nằm trong ``TelegramReceiver``.
   Tại một thời điểm có tối đa 1 request poll + 1 request tải bổ sung đang bay,
   nên không còn nhiều request cùng tranh một kết nối Telethon.
2. Push và poll cùng đi qua ``offer()``: một nơi lọc kênh, một nơi loại trùng
   (khóa chat, message_id, edit_ts). Không còn set "inflight/scheduled/seen" rải rác.
3. Lỗi kết nối được xử lý theo THỜI GIAN không có phản hồi OK (mặc định 45s) và
   có cooldown, không đếm "N timeout liên tiếp" nên không tự gây bão reconnect.
4. Callback push chỉ gọi ``offer()`` đồng bộ O(1): không tạo task, không semaphore.
5. Ghi hộp thư bền theo lô: một lần commit cho cả đợt tin đến cùng lúc.
6. Poll checkpoint chỉ tiến sau khi backfill đã đọc hết target theo ID tăng dần.

Lưu ý: poll checkpoint hiện nằm trong RAM. Durable inbox bảo vệ các envelope đã
persist, nhưng muốn replay an toàn qua process restart cần lưu checkpoint poll
nguyên tử cùng inbox rows trong SQLite (xem hướng dẫn đi kèm).

Module này không import telethon ở mức module; phần phụ thuộc Telethon nằm trong
``TelethonAdapter`` để test được bằng client giả.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Iterable, Optional

_log = logging.getLogger("tg_ingest")


# ═══════════════════════════════════════════════════════════════
# Kiểu dữ liệu
# ═══════════════════════════════════════════════════════════════
@dataclass
class Envelope:
    """Một tin đã qua lọc kênh + loại trùng, chờ ghi vào hộp thư."""

    chat_id: int
    message: Any
    edited: bool
    source: str  # "push" | "poll"
    seen_perf: float  # time.perf_counter() lúc receiver thấy tin
    age_s: float  # tuổi tin lúc thấy (giây); -1 nếu không biết


class OfferResult(str, Enum):
    """Kết quả offer; sink lỗi phải giữ nguyên checkpoint để thử lại."""

    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    FILTERED = "filtered"
    INVALID = "invalid"
    SINK_FAILED = "sink_failed"


@dataclass
class ReceiverConfig:
    poll_interval: float = 1.0  # khoảng cách giữa 2 lần BẮT ĐẦU poll
    request_timeout: float = 5.0  # timeout 1 request GetPeerDialogs
    fetch_timeout: float = 8.0  # timeout get_messages bổ sung (album / tin sửa)
    entity_timeout: float = 10.0  # timeout resolve 1 kênh
    stall_reconnect_seconds: float = 45.0  # không có phản hồi OK quá lâu -> reconnect
    reconnect_cooldown_seconds: float = 120.0  # tối thiểu giữa 2 lần ép reconnect
    peers_refresh_seconds: float = 300.0  # thử resolve lại kênh còn thiếu
    stat_interval: float = 60.0
    dedup_size: int = 8192
    backfill_batch_size: int = 100  # một transaction cho tối đa N message đã quét
    # Telegram giới hạn tần suất GetPeerDialogs (FloodWait ~4-5s). Khoảng poll tự học
    # theo FloodWait nhưng không vượt quá mức này (giây).
    max_adaptive_interval: float = 10.0
    # Sau chừng này lần poll OK liên tiếp không bị FloodWait thì nới khoảng poll 5%.
    flood_decay_after: int = 60

    @classmethod
    def from_config(cls, cfg: Any) -> "ReceiverConfig":
        def f(name: str, default: float) -> float:
            try:
                value = float(getattr(cfg, name, default))
            except (TypeError, ValueError):
                return default
            return value if value > 0 else default

        def i(name: str, default: int) -> int:
            try:
                value = int(getattr(cfg, name, default))
            except (TypeError, ValueError):
                return default
            return max(1, value)

        return cls(
            poll_interval=f("CHANNEL_POLL_INTERVAL", 1.0),
            request_timeout=max(2.0, f("CHANNEL_POLL_REQUEST_TIMEOUT", 5.0)),
            fetch_timeout=f("CHANNEL_POLL_FETCH_TIMEOUT", 8.0),
            entity_timeout=max(3.0, f("TELEGRAM_CHANNEL_TIMEOUT", 10.0)),
            stall_reconnect_seconds=f("POLL_STALL_RECONNECT_SECONDS", 45.0),
            reconnect_cooldown_seconds=f("POLL_RECONNECT_COOLDOWN_SECONDS", 120.0),
            backfill_batch_size=i("CHANNEL_BACKFILL_BATCH_SIZE", 100),
            max_adaptive_interval=max(1.0, f("CHANNEL_POLL_MAX_INTERVAL", 10.0)),
            flood_decay_after=i("CHANNEL_POLL_FLOOD_DECAY_AFTER", 60),
        )


class TelethonAdapter:
    """Toàn bộ chỗ chạm vào API Telethon của receiver (import lười)."""

    def __init__(self, client: Any):
        self.client = client

    def make_request(self, peers: list):
        from telethon.tl.functions.messages import GetPeerDialogsRequest

        return GetPeerDialogsRequest(peers=peers)

    def make_peer(self, entity: Any):
        from telethon.tl.types import InputDialogPeer

        return InputDialogPeer(entity)

    def peer_id(self, peer: Any) -> int:
        from telethon.utils import get_peer_id

        return int(get_peer_id(peer))

    def flood_seconds(self, exc: BaseException) -> Optional[int]:
        try:
            from telethon.errors import FloodWaitError
        except ImportError:  # pragma: no cover
            return None
        if isinstance(exc, FloodWaitError):
            return int(getattr(exc, "seconds", 5) or 5) + 1
        return None

    async def call(self, request):
        """Gửi request poll mà KHÔNG để Telethon tự ngủ khi bị FloodWait.

        ``client(request)`` của Telethon 1.35 bắt FloodWait <= flood_sleep_threshold
        (60s), ``asyncio.sleep`` rồi gửi lại ngay trong lời gọi, chỉ ghi log mức
        INFO. Với bộ poll, điều đó biến FloodWait ~4-5s thành request "treo" tới
        khi wait_for timeout. Ở đây gửi thẳng qua sender để FloodWaitError nổi lên
        ngay và receiver tự điều chỉnh nhịp. Thiếu API nội bộ thì quay về client().
        """
        client = self.client
        sender = getattr(client, "_sender", None)
        send = getattr(sender, "send", None)
        if send is None:
            return await client(request)
        from telethon import utils

        await request.resolve(client, utils)
        result = await send(request)
        try:
            client.session.process_entities(result)
        except Exception:
            pass
        return result



# ═══════════════════════════════════════════════════════════════
# TelegramReceiver
# ═══════════════════════════════════════════════════════════════
class TelegramReceiver:
    """Chủ sở hữu duy nhất của việc đọc tin kênh từ Telegram."""

    def __init__(
        self,
        client: Any,
        chats: Iterable[int],
        sink: Callable[[Envelope], None],
        *,
        adapter: Any = None,
        accept: Optional[Callable[[Any], bool]] = None,
        accept_backfill: Optional[Callable[[Any], bool]] = None,
        commit_backfill: Optional[Callable[[int, list[Envelope], tuple[int, int]], Any]] = None,
        bootstrap_checkpoint: Optional[Callable[[int, int, int], tuple[int, int]]] = None,
        cfg: Optional[ReceiverConfig] = None,
        log: Optional[logging.Logger] = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._client = client
        self._chats = tuple(int(c) for c in chats)
        self._chat_set = set(self._chats)
        self._sink = sink
        self._adapter = adapter or TelethonAdapter(client)
        self._accept = accept
        # Recovery/backfill must not use a BOT_START_TIME freshness cutoff.
        self._accept_backfill = accept_backfill
        self._commit_backfill = commit_backfill
        self._bootstrap_checkpoint = bootstrap_checkpoint
        self._cfg = cfg or ReceiverConfig()
        self._log = log or _log
        self._clock = clock

        self._seen: "OrderedDict[tuple, None]" = OrderedDict()
        # chat -> (last fully scanned message ID, last scanned edit timestamp).
        # This is deliberately not the latest top_message observed in a poll.
        self._state: dict[int, tuple[int, int]] = {}
        self._fetch_q: "asyncio.Queue | None" = None  # queue of chat IDs
        self._pending_targets: dict[int, tuple[int, int]] = {}
        self._fetch_scheduled: set[int] = set()
        self._peers: Optional[list] = None
        self._peers_refresh_at = 0.0
        self._pause_until = 0.0
        self._started_at = clock()
        self._last_ok: Optional[float] = None
        self._last_reconnect = float("-inf")
        self._err_streak = 0
        self._last_err_log = float("-inf")
        # Khoảng cách giữa 2 lần BẮT ĐẦU poll. Bắt đầu từ cấu hình, tự tăng khi Telegram
        # trả FloodWait (đo thực tế log: ~12 phản hồi OK/phút, tức ~1 request / 5s).
        self._interval = self._cfg.poll_interval
        self._ok_streak = 0
        self._last_ok_start: Optional[float] = None
        self._last_flood_log = float("-inf")
        self._reset_stats()

        # đếm tối đa request đồng thời (phục vụ test + chẩn đoán)
        self.inflight_requests = 0
        self.max_inflight_requests = 0

    def restore_checkpoints(self, checkpoints: dict[int, tuple[int, int]]) -> None:
        """Restore cursors loaded from DurableInbox before starting poll."""
        for cid, value in checkpoints.items():
            key = int(cid)
            if key in self._chat_set:
                message_id, edit_ts = value
                self._state[key] = (int(message_id), int(edit_ts))

    # ── số liệu ────────────────────────────────────────────────
    def _reset_stats(self) -> None:
        self._st = {
            "ok": 0, "timeout": 0, "error": 0, "flood": 0, "lat_sum": 0.0, "lat_max": 0.0,
            "push": 0, "poll": 0, "dup": 0, "rejected": 0,
            "age_sum": 0.0, "age_n": 0, "age_max": 0.0,
        }

    def _log_stats(self) -> None:
        s = self._st
        ok = s["ok"]
        self._log.info(
            "📡 [Rx-Stat] %.0fs: poll ok=%s timeout=%s lỗi=%s flood=%s nhịp=%.1fs | "
            "trễ TB=%.0fms max=%.0fms "
            "| tin mới: push=%s poll=%s trùng=%s | tuổi lúc thấy TB=%.1fs max=%.1fs",
            self._cfg.stat_interval, ok, s["timeout"], s["error"], s["flood"], self._interval,
            (s["lat_sum"] / ok) if ok else 0.0, s["lat_max"],
            s["push"], s["poll"], s["dup"],
            (s["age_sum"] / s["age_n"]) if s["age_n"] else 0.0, s["age_max"],
        )
        self._reset_stats()

    # ── cổng vào duy nhất ──────────────────────────────────────
    def offer(self, chat_id: Any, message: Any, *, edited: bool, source: str) -> bool:
        """Giao diện tương thích: True chỉ khi envelope mới được chuyển cho sink."""
        return self._offer_detailed(chat_id, message, edited=edited, source=source) == OfferResult.ACCEPTED

    def _offer_detailed(
        self, chat_id: Any, message: Any, *, edited: bool, source: str
    ) -> OfferResult:
        """Lọc + dedupe + handoff, phân biệt lỗi tạm thời với loại/trùng hợp lệ."""
        try:
            cid = int(chat_id)
        except (TypeError, ValueError):
            return OfferResult.INVALID
        if cid not in self._chat_set or message is None:
            return OfferResult.INVALID
        mid = getattr(message, "id", None)
        if mid is None:
            return OfferResult.INVALID
        try:
            if self._accept is not None and not self._accept(message):
                self._st["rejected"] += 1
                return OfferResult.FILTERED
        except Exception as exc:
            self._log.error("❌ [Rx] accept lỗi chat=%s message=%s: %r", cid, mid, exc)
            return OfferResult.SINK_FAILED

        edit_dt = getattr(message, "edit_date", None)
        edit_ts = int(edit_dt.timestamp()) if (edited and edit_dt) else 0
        key = (cid, int(mid), edit_ts, bool(edited))
        if key in self._seen:
            self._seen.move_to_end(key)
            self._st["dup"] += 1
            return OfferResult.DUPLICATE
        self._seen[key] = None
        while len(self._seen) > self._cfg.dedup_size:
            self._seen.popitem(last=False)

        m_date = getattr(message, "date", None)
        if m_date is not None:
            if m_date.tzinfo is None:
                m_date = m_date.replace(tzinfo=timezone.utc)
            age = max(0.0, (datetime.now(timezone.utc) - m_date).total_seconds())
        else:
            age = -1.0

        env = Envelope(cid, message, bool(edited), source, time.perf_counter(), age)
        try:
            self._sink(env)
        except Exception as exc:  # sink hỏng: cho phép lần giao sau thử lại
            self._seen.pop(key, None)
            self._log.error("❌ [Rx] sink lỗi chat=%s message=%s: %r", cid, mid, exc)
            return OfferResult.SINK_FAILED

        self._st[source if source in ("push", "poll") else "push"] += 1
        if age >= 0:
            self._st["age_sum"] += age
            self._st["age_n"] += 1
            self._st["age_max"] = max(self._st["age_max"], age)
        if source == "poll":
            self._log.info(
                "📡 [Poll] chat=%s message_id=%s edited=%s tuổi=%.1fs", cid, mid, edited, age
            )
        return OfferResult.ACCEPTED

    def on_push(self, event: Any, *, edited: bool = False) -> None:
        """Gắn thẳng vào handler NewMessage/MessageEdited của Telethon."""
        try:
            self.offer(
                getattr(event, "chat_id", None),
                getattr(event, "message", None),
                edited=edited,
                source="push",
            )
        except Exception as exc:
            self._log.error("❌ [Rx] push handler lỗi: %r", exc)

    # ── vòng chạy ──────────────────────────────────────────────
    async def run(self) -> None:
        self._fetch_q = asyncio.Queue()
        # Nếu cùng receiver được chạy lại sau cancellation, queue cũ đã mất.
        self._fetch_scheduled.clear()
        # Push có thể đến trước lúc queue được khởi tạo; schedule target đã lưu.
        for cid in tuple(self._pending_targets):
            self._schedule_fetch(cid)
        fetcher = asyncio.create_task(self._fetch_loop(), name="rx-fetch")
        try:
            await self._poll_loop()
        finally:
            fetcher.cancel()
            await asyncio.gather(fetcher, return_exceptions=True)

    async def _poll_loop(self) -> None:
        cfg = self._cfg
        last_stat = self._clock()
        err_backoff = 0.0
        while True:
            now = self._clock()
            if now - last_stat >= cfg.stat_interval:
                self._log_stats()
                last_stat = now

            if not self._client.is_connected():
                await asyncio.sleep(0.5)
                continue

            if now < self._pause_until:
                await asyncio.sleep(min(1.0, self._pause_until - now))
                continue

            if self._peers is None or (
                self._peers_refresh_at and now >= self._peers_refresh_at
            ):
                resolved = await self._resolve_peers()
                if not resolved:
                    self._log.error("❌ [Rx] không resolve được kênh nào — thử lại sau 5s")
                    await asyncio.sleep(5.0)
                    continue
                self._peers = resolved

            started = self._clock()
            outcome = await self._poll_once(self._peers)
            if outcome == "ok":
                err_backoff = 0.0
                await asyncio.sleep(max(0.0, self._interval - (self._clock() - started)))
            elif outcome == "timeout":
                err_backoff = 0.0  # đã chờ đủ request_timeout rồi, bắn lại ngay
                await asyncio.sleep(0)
            elif outcome == "error":
                err_backoff = min(5.0, max(0.5, err_backoff * 2 or 0.5))
                await asyncio.sleep(err_backoff)
            else:  # flood: _pause_until đã đặt
                await asyncio.sleep(0)

    async def _resolve_peers(self) -> list:
        out: list = []
        missing = 0
        for cid in self._chats:
            try:
                ent = await asyncio.wait_for(
                    self._client.get_input_entity(cid), timeout=self._cfg.entity_timeout
                )
                out.append(self._adapter.make_peer(ent))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                missing += 1
                self._log.warning("⚠️ [Rx] không resolve được chat=%s: %s", cid, exc)
        self._peers_refresh_at = (
            self._clock() + self._cfg.peers_refresh_seconds if missing else 0.0
        )
        return out

    async def _poll_once(self, peers: list) -> str:
        cfg = self._cfg
        t0 = self._clock()
        self.inflight_requests += 1
        self.max_inflight_requests = max(self.max_inflight_requests, self.inflight_requests)
        try:
            request = self._adapter.make_request(peers)
            call = getattr(self._adapter, "call", None)
            resp = await asyncio.wait_for(
                call(request) if call is not None else self._client(request),
                timeout=cfg.request_timeout,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            self._st["timeout"] += 1
            await self._maybe_reconnect()
            return "timeout"
        except Exception as exc:
            secs = self._adapter.flood_seconds(exc)
            if secs:
                self._on_flood(secs, t0)
                return "flood"
            self._st["error"] += 1
            self._err_streak += 1
            now = self._clock()
            if now - self._last_err_log >= 30.0:
                self._last_err_log = now
                self._log.warning(
                    "⚠️ [Rx] poll lỗi (%s lần liên tiếp): %s: %r",
                    self._err_streak, type(exc).__name__, exc,
                )
            if self._err_streak >= 3:
                self._peers = None  # resolve lại kênh ở vòng sau
            await self._maybe_reconnect()
            return "error"
        finally:
            self.inflight_requests -= 1

        lat_ms = (self._clock() - t0) * 1000.0
        self._last_ok = self._clock()
        self._err_streak = 0
        self._last_ok_start = t0
        self._note_ok_for_interval()
        self._st["ok"] += 1
        self._st["lat_sum"] += lat_ms
        self._st["lat_max"] = max(self._st["lat_max"], lat_ms)
        try:
            self._apply(resp)
        except Exception as exc:
            self._log.warning("⚠️ [Rx] xử lý phản hồi lỗi: %s: %r", type(exc).__name__, exc)
        return "ok"

    def _on_flood(self, secs: int, t0: float) -> None:
        """Honor FloodWait, including long waits, without repeating the same poll rate."""
        cfg = self._cfg
        now = self._clock()
        self._pause_until = max(self._pause_until, now + secs)
        self._last_ok = now  # FloodWait proves the connection is alive.
        self._ok_streak = 0
        self._st["flood"] += 1
        raw = max(1, secs - 1)
        cap = max(cfg.poll_interval, cfg.max_adaptive_interval)
        if self._last_ok_start is None:
            # First startup penalty has no successful baseline to learn from.
            new_interval = self._interval
        elif raw <= cfg.max_adaptive_interval:
            learned = max(0.0, t0 - self._last_ok_start) + raw + 0.2
            new_interval = min(cap, max(self._interval, learned))
        else:
            # A long penalty after successful polls also proves the current
            # rate is too aggressive. Increase gradually rather than treating
            # the entire penalty as the required steady-state interval.
            new_interval = min(cap, max(cfg.poll_interval, self._interval * 1.5))
        if new_interval > self._interval + 0.05 or now - self._last_flood_log >= 30.0:
            self._last_flood_log = now
            self._log.warning(
                "⏳ [Rx] FloodWait %ss từ Telegram — nghỉ poll, nhịp poll %.1fs -> %.1fs",
                raw, self._interval, new_interval,
            )
        self._interval = new_interval

    def _note_ok_for_interval(self) -> None:
        """Poll OK liên tiếp lâu không bị FloodWait: nới nhịp về phía cấu hình gốc."""
        cfg = self._cfg
        self._ok_streak += 1
        if self._interval > cfg.poll_interval and self._ok_streak >= cfg.flood_decay_after:
            self._ok_streak = 0
            self._interval = max(cfg.poll_interval, self._interval * 0.95)

    async def _maybe_reconnect(self) -> None:
        """Ép reconnect khi KHÔNG có phản hồi OK quá stall_reconnect_seconds."""
        cfg = self._cfg
        now = self._clock()
        since_ok = now - (self._last_ok if self._last_ok is not None else self._started_at)
        if since_ok < cfg.stall_reconnect_seconds:
            return
        if now - self._last_reconnect < cfg.reconnect_cooldown_seconds:
            return
        self._last_reconnect = now
        self._peers = None
        self._log.warning(
            "🔌 [Rx] %.0fs không có phản hồi OK — đóng connection để watchdog reconnect", since_ok
        )
        try:
            await asyncio.wait_for(self._client.disconnect(), timeout=5.0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log.warning("⚠️ [Rx] disconnect lỗi: %s: %r", type(exc).__name__, exc)

    # ── xử lý phản hồi poll ────────────────────────────────────
    def _apply(self, resp: Any) -> None:
        """Nhận top_message làm target; không nhảy checkpoint trước backfill."""
        ad = self._adapter
        raw_msgs: dict = {}
        for rm in getattr(resp, "messages", None) or []:
            peer = getattr(rm, "peer_id", None)
            if peer is not None:
                raw_msgs[(ad.peer_id(peer), int(rm.id))] = rm

        for dialog in getattr(resp, "dialogs", None) or []:
            cid = ad.peer_id(dialog.peer)
            if cid not in self._chat_set:
                continue
            top = int(getattr(dialog, "top_message", 0) or 0)
            if top <= 0:
                continue

            raw = raw_msgs.get((cid, top))
            edit_dt = getattr(raw, "edit_date", None) if raw is not None else None
            edit_ts = int(edit_dt.timestamp()) if edit_dt else 0
            checkpoint = self._state.get(cid)
            if checkpoint is None:
                if self._bootstrap_checkpoint is not None:
                    # INSERT OR IGNORE: nếu checkpoint durable đã xuất hiện, lấy giá trị thắng.
                    checkpoint = self._bootstrap_checkpoint(cid, top, edit_ts)
                else:
                    # Standalone compatibility; production wires the SQLite bootstrap.
                    checkpoint = (top, edit_ts)
                self._state[cid] = checkpoint
                if top > checkpoint[0] or (top == checkpoint[0] and edit_ts > checkpoint[1]):
                    self._request_backfill(cid, (top, edit_ts))
                continue
            if top < checkpoint[0] or (top == checkpoint[0] and edit_ts < checkpoint[1]):
                continue  # phản hồi poll cũ hơn checkpoint đã hoàn tất
            if top > checkpoint[0] or (top == checkpoint[0] and edit_ts > checkpoint[1]):
                self._request_backfill(cid, (top, edit_ts))

    def _request_backfill(self, cid: int, target: tuple[int, int]) -> None:
        """Gộp target mới theo channel để không tạo nhiều job trùng."""
        previous = self._pending_targets.get(cid)
        if previous is None or target > previous:
            self._pending_targets[cid] = target
        self._schedule_fetch(cid)

    def _schedule_fetch(self, cid: int) -> None:
        if self._fetch_q is None or cid in self._fetch_scheduled:
            return
        self._fetch_scheduled.add(cid)
        self._fetch_q.put_nowait(cid)

    async def _fetch_loop(self) -> None:
        """Drain từng channel tuần tự; lỗi giữ checkpoint để poll kế tiếp retry."""
        assert self._fetch_q is not None
        while True:
            cid = await self._fetch_q.get()
            try:
                await self._process_fetch_job(cid)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log.warning(
                    "⚠️ [Rx] backfill lỗi chat=%s: %s: %r — checkpoint giữ nguyên",
                    cid, type(exc).__name__, exc,
                )
                self._fetch_scheduled.discard(cid)

    async def _process_fetch_job(self, cid: int) -> bool:
        """Xử lý target(s) đã coalesce cho một channel; chỉ tiến state khi xong."""
        try:
            while cid in self._pending_targets:
                target = self._pending_targets[cid]
                checkpoint = self._state.get(cid)
                if checkpoint is None:
                    if self._bootstrap_checkpoint is not None:
                        checkpoint = self._bootstrap_checkpoint(cid, 0, 0)
                    else:
                        checkpoint = (0, 0)
                    self._state[cid] = checkpoint
                    if target <= checkpoint:
                        self._pending_targets.pop(cid, None)
                        return True
                    # No known safe baseline: fail safe by backfilling from ID 0.
                if target <= checkpoint:
                    self._pending_targets.pop(cid, None)
                    return True

                if not await self._drain_to_target(cid, checkpoint, target):
                    return False

                # Chỉ tới đây khi iterator đã kết thúc bình thường.
                self._state[cid] = target
                self._log.info(
                    "📥 [Rx] backfill hoàn tất chat=%s checkpoint=%s target=%s",
                    cid, checkpoint[0], target[0],
                )
                if self._pending_targets.get(cid, target) <= target:
                    self._pending_targets.pop(cid, None)
                    return True
                # Poll đã nâng target trong lúc await; tiếp tục từ state vừa hoàn tất.
            return True
        finally:
            # Nếu thất bại, để poll tiếp theo schedule lại; không retry nóng gây bão.
            self._fetch_scheduled.discard(cid)

    def _prepare_backfill_envelope(
        self, cid: int, message: Any, *, edited: bool, local_keys: set[tuple]
    ) -> tuple[OfferResult, Optional[Envelope], Optional[tuple]]:
        """Build an envelope without RAM handoff; DB transaction decides durability.

        Deliberately does not reject a key already in _seen: a push may only be
        queued in RAM, not persisted. The SQLite UNIQUE key resolves that race.
        """
        if message is None or int(cid) not in self._chat_set:
            return OfferResult.INVALID, None, None
        mid = getattr(message, "id", None)
        if mid is None:
            return OfferResult.INVALID, None, None
        validator = self._accept_backfill or self._accept
        try:
            if validator is not None and not validator(message):
                self._st["rejected"] += 1
                return OfferResult.FILTERED, None, None
        except Exception as exc:
            self._log.error("❌ [Rx] accept_backfill lỗi chat=%s message=%s: %r", cid, mid, exc)
            return OfferResult.SINK_FAILED, None, None

        edit_dt = getattr(message, "edit_date", None)
        edit_ts = int(edit_dt.timestamp()) if (edited and edit_dt) else 0
        key = (int(cid), int(mid), edit_ts, bool(edited))
        if key in local_keys:
            self._st["dup"] += 1
            return OfferResult.DUPLICATE, None, key
        local_keys.add(key)

        msg_date = getattr(message, "date", None)
        if msg_date is not None:
            if msg_date.tzinfo is None:
                msg_date = msg_date.replace(tzinfo=timezone.utc)
            age = max(0.0, (datetime.now(timezone.utc) - msg_date).total_seconds())
        else:
            age = -1.0
        env = Envelope(int(cid), message, bool(edited), "poll", time.perf_counter(), age)
        return OfferResult.ACCEPTED, env, key

    def _mark_backfill_committed(self, items: list[tuple[Envelope, tuple]]) -> None:
        """Only call after SQLite confirms rows+cursor committed."""
        for env, key in items:
            if key in self._seen:
                self._seen.move_to_end(key)
                self._st["dup"] += 1
            else:
                self._seen[key] = None
                self._st["poll"] += 1
                if env.age_s >= 0:
                    self._st["age_sum"] += env.age_s
                    self._st["age_n"] += 1
                    self._st["age_max"] = max(self._st["age_max"], env.age_s)
            while len(self._seen) > self._cfg.dedup_size:
                self._seen.popitem(last=False)

    async def _commit_backfill_batch(
        self, cid: int, items: list[tuple[Envelope, tuple]], checkpoint: tuple[int, int]
    ) -> bool:
        envelopes = [env for env, _key in items]
        if self._commit_backfill is not None:
            try:
                committed = self._commit_backfill(cid, envelopes, checkpoint)
                if inspect.isawaitable(committed):
                    committed = await committed
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log.error(
                    "❌ [Rx] commit backfill lỗi chat=%s cursor=%s: %r", cid, checkpoint, exc
                )
                return False
            if not committed:
                return False
            self._mark_backfill_committed(items)
            self._state[cid] = checkpoint
            return True

        # Compatibility fallback for stand-alone users. Production main_script
        # injects commit_backfill so a durable checkpoint is always atomic.
        for env in envelopes:
            result = self._offer_detailed(
                cid, env.message, edited=env.edited, source="poll"
            )
            if result == OfferResult.SINK_FAILED:
                return False
        self._state[cid] = checkpoint
        return True

    async def _drain_to_target(
        self, cid: int, checkpoint: tuple[int, int], target: tuple[int, int]
    ) -> bool:
        """Fetch (checkpoint, target] ascending and commit bounded durable batches."""
        checkpoint_id, _checkpoint_edit_ts = checkpoint
        target_id, target_edit_ts = target
        timeout = max(0.1, float(self._cfg.fetch_timeout))

        try:
            if target_id == checkpoint_id:
                # Edit of the current top: fetch exactly one ID and commit it with edit_ts.
                result = await asyncio.wait_for(
                    self._client.get_messages(cid, ids=[target_id]), timeout=timeout
                )
                if isinstance(result, (list, tuple)):
                    messages = [message for message in result if message]
                else:
                    messages = [result] if result else []
                items: list[tuple[Envelope, tuple]] = []
                keys: set[tuple] = set()
                for message in messages:
                    disposition, env, key = self._prepare_backfill_envelope(
                        cid, message, edited=True, local_keys=keys
                    )
                    if disposition == OfferResult.SINK_FAILED:
                        return False
                    if env is not None and key is not None:
                        items.append((env, key))
                # Also persist the cursor if the message was removed or filtered.
                return await self._commit_backfill_batch(cid, items, target)

            # limit=None làm Telethon tự đặt wait_time=1s (limit=inf > 3000) và luôn gọi
            # thêm 1 request để biết "đã hết" → mỗi tin poll bị trễ ~1s (ingress_to_durable_ms
            # ≈ 1000). Số tin trong (checkpoint, target] không thể vượt target-checkpoint nên
            # đặt limit đúng cận đó: iterator dừng ngay sau tin cuối; nếu có ID bị xoá thì
            # vẫn gọi thêm 1 request (không mất tin).
            iterator = self._client.iter_messages(
                cid,
                limit=max(1, target_id - checkpoint_id),
                reverse=True,
                min_id=checkpoint_id,
                max_id=target_id + 1,
                wait_time=0,
            )
            cursor = checkpoint_id
            last_seen_id = checkpoint_id
            scanned_in_batch = 0
            batch: list[tuple[Envelope, tuple]] = []
            local_keys: set[tuple] = set()
            batch_size = max(1, int(self._cfg.backfill_batch_size))
            try:
                while True:
                    try:
                        message = await asyncio.wait_for(
                            iterator.__anext__(), timeout=timeout
                        )
                    except StopAsyncIteration:
                        break

                    message_id = int(getattr(message, "id", 0) or 0)
                    if message_id <= checkpoint_id:
                        continue
                    if message_id > target_id:
                        raise RuntimeError(
                            f"history iterator crossed target: id={message_id} target={target_id}"
                        )
                    if message_id < last_seen_id:
                        raise RuntimeError(
                            f"history iterator is not ascending: previous={last_seen_id} id={message_id}"
                        )
                    if message_id == last_seen_id:
                        continue

                    disposition, env, key = self._prepare_backfill_envelope(
                        cid, message, edited=False, local_keys=local_keys
                    )
                    if disposition == OfferResult.SINK_FAILED:
                        return False
                    if env is not None and key is not None:
                        batch.append((env, key))
                    cursor = message_id
                    last_seen_id = message_id
                    scanned_in_batch += 1

                    if scanned_in_batch >= batch_size:
                        # Persist filtered-only batches too: cursor describes scanned IDs,
                        # not only accepted rows. Never hold SQLite transaction during fetch.
                        if not await self._commit_backfill_batch(
                            cid, batch, (cursor, 0)
                        ):
                            return False
                        batch = []
                        local_keys = set()
                        scanned_in_batch = 0

                # The final commit pins the exact poll snapshot, including edit_ts.
                # It is required even with an empty batch (deleted/filtered tail/gaps).
                return await self._commit_backfill_batch(
                    cid, batch, (target_id, target_edit_ts)
                )
            finally:
                close = getattr(iterator, "aclose", None)
                if close is not None:
                    try:
                        await close()
                    except Exception:
                        pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            flood_seconds = self._adapter.flood_seconds(exc)
            if flood_seconds:
                self._pause_until = max(
                    self._pause_until, self._clock() + max(1, flood_seconds)
                )
                self._log.warning(
                    "⚠️ [Rx] FloodWait %ss khi backfill chat=%s; giữ checkpoint",
                    flood_seconds, cid,
                )
            else:
                self._log.warning(
                    "⚠️ [Rx] backfill lỗi chat=%s checkpoint=%s target=%s: %s: %r",
                    cid, checkpoint, target, type(exc).__name__, exc,
                )
            return False


# ═══════════════════════════════════════════════════════════════
# IngressStage — ghi hộp thư bền theo lô
# ═══════════════════════════════════════════════════════════════
class IngressStage:
    """Nhận Envelope, gom thành lô, ghi bền bằng MỘT commit, rồi giao cho on_row.

    - ``build_row(env)``  -> tuple đối số cho DurableInbox.enqueue_many
    - ``persist(rows)``   -> list[int|0|None] (id mới / 0 = trùng / None = lỗi DB);
      chạy trong executor để không chặn event loop
    - ``on_row(env, id)`` -> hàm (đồng bộ hoặc async) đưa id vào message_queue
    """

    def __init__(
        self,
        *,
        build_row: Callable[[Envelope], tuple],
        persist: Callable[[list], list],
        on_row: Callable[[Envelope, int], Any],
        persist_checkpointed: Optional[Callable[[list, int, int, int], Any]] = None,
        executor: Any = None,
        max_batch: int = 64,
        retries: int = 3,
        log: Optional[logging.Logger] = None,
    ):
        self._build_row = build_row
        self._persist = persist
        self._persist_checkpointed = persist_checkpointed
        self._on_row = on_row
        self._executor = executor
        self._max_batch = max(1, int(max_batch))
        self._retries = max(1, int(retries))
        self._log = log or _log
        self._q: "deque[Envelope]" = deque()
        self._wake = asyncio.Event()
        self._busy = False
        self.batches = 0
        self.rows_written = 0
        self.duplicates = 0
        self.lost = 0

    def put(self, env: Envelope) -> None:
        self._q.append(env)
        self._wake.set()

    @property
    def pending(self) -> int:
        return len(self._q) + (1 if self._busy else 0)

    async def run(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self._q:
                batch = [self._q.popleft() for _ in range(min(len(self._q), self._max_batch))]
                self._busy = True
                try:
                    await self._flush(batch)
                except asyncio.CancelledError:
                    # trả lại để drain() lúc tắt máy còn xử lý được
                    self._q.extendleft(reversed(batch))
                    raise
                except Exception as exc:
                    self._log.error("❌ [Ingress] flush lỗi: %r", exc)
                finally:
                    self._busy = False

    async def commit_backfill_batch(
        self, chat_id: int, envelopes: list[Envelope], checkpoint: tuple[int, int]
    ) -> bool:
        """Persist batch+cursor atomically, then expose new row IDs to workers."""
        if self._persist_checkpointed is None:
            self._log.error("❌ [Ingress] thiếu persist_checkpointed; không tiến cursor poll")
            return False
        try:
            rows = [self._build_row(env) for env in envelopes]
        except Exception as exc:
            self._log.error("❌ [Ingress] build backfill row lỗi: %r", exc)
            return False

        loop = asyncio.get_running_loop()
        results = None
        for attempt in range(self._retries):
            try:
                results = await loop.run_in_executor(
                    self._executor,
                    self._persist_checkpointed,
                    rows,
                    int(chat_id),
                    int(checkpoint[0]),
                    int(checkpoint[1]),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log.warning("⚠️ [Ingress] atomic backfill persist lỗi: %r", exc)
                results = None

            if results is not None and len(results) == len(envelopes) and not any(
                row_id is None for row_id in results
            ):
                break
            if attempt + 1 < self._retries:
                self._log.warning(
                    "⚠️ [Ingress] atomic checkpoint chưa xác nhận; retry %s/%s",
                    attempt + 1, self._retries - 1,
                )
                await asyncio.sleep(min(0.5, 0.05 * (2 ** attempt)))

        if results is None or len(results) != len(envelopes) or any(r is None for r in results):
            self._log.error(
                "❌ [Ingress] atomic backfill chưa commit chat=%s checkpoint=%s",
                chat_id, checkpoint,
            )
            return False

        self.batches += 1
        for env, row_id in zip(envelopes, results):
            if int(row_id) == 0:
                self.duplicates += 1
            else:
                self.rows_written += 1
                await self._deliver(env, int(row_id))
        return True

    async def _flush(self, batch: list) -> None:
        loop = asyncio.get_running_loop()
        pending = batch
        for attempt in range(self._retries):
            rows = [self._build_row(e) for e in pending]
            try:
                results = await loop.run_in_executor(self._executor, self._persist, rows)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log.error("❌ [Ingress] persist lỗi: %r", exc)
                results = [None] * len(pending)
            self.batches += 1
            retry: list = []
            for env, rid in zip(pending, results):
                if rid is None:
                    retry.append(env)
                elif rid == 0:
                    self.duplicates += 1
                else:
                    self.rows_written += 1
                    await self._deliver(env, int(rid))
            if not retry:
                return
            pending = retry
            if attempt + 1 < self._retries:
                self._log.warning(
                    "⚠️ [Ingress] ghi DB chưa được %s tin, thử lại %s/%s",
                    len(pending), attempt + 1, self._retries - 1,
                )
                await asyncio.sleep(min(0.5, 0.05 * (2 ** attempt)))
        for env in pending:
            self.lost += 1
            self._log.error(
                "❌ [Ingress] không ghi được DB chat=%s message=%s",
                env.chat_id, getattr(env.message, "id", "?"),
            )

    async def _deliver(self, env: Envelope, row_id: int) -> None:
        try:
            result = self._on_row(env, row_id)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log.error("❌ [Ingress] on_row lỗi row=%s: %r", row_id, exc)

    async def drain(self, timeout: float = 5.0) -> bool:
        """Chờ ghi nốt các tin đã nhận (dùng khi tắt máy). True nếu hết sạch."""
        deadline = time.monotonic() + timeout
        while self.pending and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        return not self.pending
