"""Request/stage timing instrumentation for Bot_Tele.

The logger emits one compact JSON record per request. It is intentionally
non-blocking from the event loop's perspective: timing collection is in-memory
and the existing logging handlers perform the actual output.
"""
from __future__ import annotations

import json
import time
from collections import deque
from contextvars import ContextVar
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

import logger_setup as logger_setup_module
from logger_setup import logger

_current_timer: ContextVar["RequestTimer | None"] = ContextVar("current_request_timer", default=None)

# Benchmark timing giữ trong RAM (deque giới hạn kích thước) thay vì ghi
# file log mỗi request — tự loại bỏ bản ghi cũ khi đầy, mất khi bot restart
# (chấp nhận được vì chỉ phục vụ dashboard/soi hiệu năng tức thời).
_TIMING_HISTORY_MAXLEN = 500
_timing_history: deque[dict[str, Any]] = deque(maxlen=_TIMING_HISTORY_MAXLEN)


def get_current_timer() -> "RequestTimer | None":
    return _current_timer.get()


def set_current_timer(timer: "RequestTimer | None"):
    return _current_timer.set(timer)


def reset_current_timer(token) -> None:
    _current_timer.reset(token)


@dataclass
class RequestTimer:
    request_id: str
    chat_id: Any = None
    message_id: Any = None
    kind: str = "message"
    started_perf: float = field(default_factory=time.perf_counter)
    stages: dict[str, float] = field(default_factory=dict)
    status: str = "ok"

    @classmethod
    def from_event(cls, event: Any, kind: str = "message") -> "RequestTimer":
        chat_id = getattr(event, "chat_id", None)
        message = getattr(event, "message", None)
        message_id = getattr(message, "id", None)
        request_id = f"{chat_id}:{message_id}" if message_id is not None else str(id(event))
        return cls(request_id=request_id, chat_id=chat_id, message_id=message_id, kind=kind)

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            self.stages[name] = self.stages.get(name, 0.0) + (time.perf_counter() - started)

    @asynccontextmanager
    async def stage_async(self, name: str) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            self.stages[name] = self.stages.get(name, 0.0) + (time.perf_counter() - started)

    def emit(self, status: str = "ok", **extra: Any) -> dict[str, Any]:
        record: dict[str, Any] = {
            "event": "request_timing",
            "request_id": self.request_id,
            "chat_id": self.chat_id,
            "message_id": self.message_id,
            "kind": self.kind,
            "status": status,
            "elapsed_ms": round((time.perf_counter() - self.started_perf) * 1000, 2),
            "stages_ms": {k: round(v * 1000, 2) for k, v in self.stages.items()},
        }
        record.update(extra)
        _timing_history.append(record)
        # Chỉ ghi ra file khi DEBUG_VERBOSE_MODE=true — tránh I/O đĩa mỗi
        # request ở đường mặc định.
        if getattr(logger_setup_module, "DEBUG_VERBOSE_MODE", False):
            logger.debug("⏱️ TIMING %s", json.dumps(record, ensure_ascii=False, separators=(",", ":")))
        try:
            from dashboard import report_timing_record
            report_timing_record(record)
        except Exception:
            pass
        return record

    def finish(self, status: str = "ok", **extra: Any) -> dict[str, Any]:
        self.status = status
        return self.emit(status, **extra)