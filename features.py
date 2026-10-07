"""
✨ THÊM CÁC FEATURES MỚI
Config validation, Statistics, Memory monitoring, Version check
"""

from __future__ import annotations

import asyncio
import signal
import time
from logger_setup import logger

# ==========================================
# 📌 VERSION CHECK
# ==========================================

BOT_VERSION = "11.0"
BOT_BUILD_DATE = "2026-06-27"

def print_version_info():
    """In thong tin version - 1 dong gon"""
    try:
        import telethon
        try:
            from importlib.metadata import version as pkg_version
            browser_lib_version = pkg_version("playwright")
        except Exception:
            browser_lib_version = "unknown"

        logger.info(
            f"📦 Bot v{BOT_VERSION} ({BOT_BUILD_DATE}) | "
            f"Telethon {telethon.__version__} | Playwright {browser_lib_version}"
        )
    except Exception as e:
        logger.error(f"❌ Loi lay version: {e}")


# ==========================================
# 🛡️ GRACEFUL SHUTDOWN HANDLER
# ==========================================

class GracefulShutdownHandler:
    """Xu ly tat bot an toan:
    - Ctrl+C  → SIGINT
    - Bam X CMD → CTRL_CLOSE_EVENT (Windows console handler)
    - Shutdown/Logoff → CTRL_SHUTDOWN_EVENT / CTRL_LOGOFF_EVENT
    Cho toi da 8 giay de cleanup roi moi thoat.
    """

    def __init__(self):
        self.shutdown_initiated = False
        self.shutdown_complete = False
        self._event = None
        self._loop  = None
        self._stop_callback = None

    def set_stop_callback(self, callback):
        """Register an async/sync callback that releases the main wait."""
        self._stop_callback = callback

    def setup(self, bot_state):
        import threading
        self._event = threading.Event()

        try:
            import asyncio as _asyncio
            self._loop = _asyncio.get_event_loop()
        except Exception:
            self._loop = None

        def signal_handler(signum, frame):
            self._do_shutdown(bot_state, reason="SIGINT/SIGTERM")

        try:
            signal.signal(signal.SIGINT,  signal_handler)
            signal.signal(signal.SIGTERM, signal_handler)
        except Exception:
            pass

        try:
            import ctypes, ctypes.wintypes

            CTRL_C_EVENT        = 0
            CTRL_BREAK_EVENT    = 1
            CTRL_CLOSE_EVENT    = 2
            CTRL_LOGOFF_EVENT   = 5
            CTRL_SHUTDOWN_EVENT = 6

            HANDLER_FUNC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

            _CTRL_EVENT_NAMES = {
                CTRL_C_EVENT: "CTRL_C_EVENT (Ctrl+C)",
                CTRL_BREAK_EVENT: "CTRL_BREAK_EVENT (Ctrl+Break)",
                CTRL_CLOSE_EVENT: "CTRL_CLOSE_EVENT (đóng cửa sổ CMD / bấm X)",
                CTRL_LOGOFF_EVENT: "CTRL_LOGOFF_EVENT (logoff / mất session RDP)",
                CTRL_SHUTDOWN_EVENT: "CTRL_SHUTDOWN_EVENT (Windows shutdown/restart)",
            }

            def _win_handler(ctrl_type):
                if ctrl_type in (CTRL_C_EVENT, CTRL_BREAK_EVENT,
                                 CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT):
                    event_name = _CTRL_EVENT_NAMES.get(ctrl_type, f"unknown_ctrl_type={ctrl_type}")
                    self._do_shutdown(bot_state, reason=event_name)
                    self._event.wait(timeout=8)
                    return True
                return False

            self._win_handler_ref = HANDLER_FUNC(_win_handler)
            ctypes.windll.kernel32.SetConsoleCtrlHandler(self._win_handler_ref, True)
            logger.info("✅ Graceful shutdown handler setup (Ctrl+C + bam X CMD)")
        except Exception:
            logger.info("✅ Graceful shutdown handler setup (signal only)")

    def _do_shutdown(self, bot_state, reason=""):
        if self.shutdown_initiated:
            logger.warning("⚠️ Shutdown lan 2 → Force exit!")
            import os; os._exit(1)
            return

        self.shutdown_initiated = True
        logger.info("=" * 60)
        logger.info(f"🛑 TAT BOT AN TOAN ({reason})")
        logger.info("⏳ Dang don dep... cho toi da 8 giay...")
        logger.info("=" * 60)

        bot_state.is_running = False
        # Do not stop the asyncio loop here. ``main()`` owns the loop and
        # must leave its Telegram wait normally so its async ``finally`` can
        # drain queues, flush durable writes, and close resources. Stopping
        # the loop from a signal handler can make asyncio.run() raise before
        # that cleanup coroutine gets a chance to finish.
        if self._loop and self._loop.is_running() and self._stop_callback:
            try:
                def _invoke_stop():
                    try:
                        result = self._stop_callback()
                        if asyncio.iscoroutine(result):
                            asyncio.create_task(result, name="shutdown-stop-callback")
                    except Exception as exc:
                        logger.warning("⚠️ Không thể yêu cầu dừng async service: %s", exc)

                self._loop.call_soon_threadsafe(_invoke_stop)
            except Exception as exc:
                logger.warning("⚠️ Không thể lên lịch dừng async service: %s", exc)

    def notify_cleanup_done(self):
        self.shutdown_complete = True
        if self._event:
            self._event.set()


shutdown_handler = GracefulShutdownHandler()

def get_shutdown_handler() -> GracefulShutdownHandler:
    return shutdown_handler


# ==========================================
# 🤖 ASYNC ADMIN COMMANDS (/status, /stats, ...)
# ==========================================

class CommandRegistry:
    """Đăng ký & thực thi lệnh admin dạng '/ten_lenh' hoàn toàn bất đồng bộ."""

    def __init__(self, default_timeout: float = 10.0):
        self._handlers: dict[str, callable] = {}
        self._default_timeout = default_timeout
        self._running_tasks: set = set()

    def register(self, name: str, handler, timeout: float | None = None):
        self._handlers[name.strip().lower().lstrip("/")] = (handler, timeout)

    def has(self, name: str) -> bool:
        return name.strip().lower().lstrip("/") in self._handlers

    def dispatch(self, name: str, reply_fn, ctx: dict | None = None):
        key = name.strip().lower().lstrip("/")
        entry = self._handlers.get(key)
        if entry is None:
            return False

        handler, timeout = entry
        timeout = timeout or self._default_timeout
        ctx = ctx or {}

        async def _run():
            started = time.monotonic()
            try:
                text = await asyncio.wait_for(handler(**ctx), timeout=timeout)
            except asyncio.TimeoutError:
                text = f"⏰ Lệnh /{key} quá {timeout:.0f}s chưa xong — huỷ."
                logger.warning(f"⚠️ [Command] /{key} timeout sau {timeout:.0f}s")
            except Exception as e:
                text = f"❌ Lệnh /{key} lỗi: {e}"
                logger.error(f"❌ [Command] /{key} lỗi: {e}")
            else:
                elapsed = time.monotonic() - started
                logger.debug(f"✅ [Command] /{key} xong sau {elapsed:.2f}s")

            try:
                result = reply_fn(text)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.debug(f"⚠️ [Command] /{key} không gửi được phản hồi: {e}")

        task = asyncio.create_task(_run(), name=f"admin-cmd-{key}")
        self._running_tasks.add(task)
        task.add_done_callback(self._running_tasks.discard)
        return True


command_registry = CommandRegistry()


def setup_admin_commands(client, admin_id: int, context_provider=None):
    from telethon import events

    if not admin_id:
        logger.warning(
            "⚠️ [Command] TELEGRAM_ADMIN_ID chưa cấu hình (=0) — bỏ qua đăng ký "
            "lệnh admin (vd /status). Đặt TELEGRAM_ADMIN_ID trong .env nếu muốn dùng."
        )
        return

    async def _admin_command_handler(event):
        try:
            text = (event.message.message or "").strip()
        except Exception:
            return
        if not text.startswith("/"):
            return
        if getattr(event, "sender_id", None) != admin_id and event.chat_id != admin_id:
            return

        cmd = text.split()[0][1:].split("@")[0]
        if not command_registry.has(cmd):
            return

        ctx = {}
        if context_provider is not None:
            try:
                ctx = context_provider() or {}
            except Exception as e:
                logger.debug(f"⚠️ [Command] context_provider lỗi: {e}")

        async def _reply(text: str):
            try:
                await client.send_message(event.chat_id, text)
            except Exception as e:
                logger.debug(f"⚠️ [Command] Không gửi được reply: {e}")

        command_registry.dispatch(cmd, _reply, ctx)

    client.add_event_handler(_admin_command_handler, events.NewMessage(from_users=admin_id))
    logger.info(
        f"✅ Admin commands sẵn sàng (bất đồng bộ) — lệnh đã đăng ký: "
        f"{', '.join('/' + k for k in command_registry._handlers) or '(chưa có lệnh nào)'}"
    )


async def _build_status_report(**ctx) -> str:
    """Lệnh /status — đọc số liệu từ dashboard/monitoring/queue manager."""
    lines = ["📊 TRẠNG THÁI BOT"]

    try:
        import dashboard as _dashboard
        snap = _dashboard.get_dashboard_snapshot()
        lines.append(
            f"• Submit: {snap['success']} thành công / {snap['failed']} thất bại "
            f"(tổng {snap['total']})"
        )
        lines.append(
            f"• Download: {snap['download_completed']} xong, "
            f"{snap['download_active']} đang tải"
        )
    except Exception as e:
        lines.append(f"• (Không đọc được dashboard: {e})")

    try:
        import monitoring as _monitoring
        hm = getattr(_monitoring, "_health_monitor", None)
        if hm is not None:
            snap = hm.snapshot()
            lines.append(
                f"• CPU {snap['cpu_percent']:.1f}% | RAM {snap['memory_percent']:.1f}% "
                f"| OCR paused={snap['pause_ocr']} ({snap['pause_reason'] or '-'})"
            )
    except Exception as e:
        lines.append(f"• (Không đọc được monitoring: {e})")

    # ✅ Queue stats — hiển thị queue nào đang có size > 0 hoặc có drop
    try:
        from queue_manager import get_queue_manager
        qm = get_queue_manager()
        if qm is not None:
            qs = qm.snapshot()
            if qs:
                active_lines = []
                for name, st in sorted(qs.items()):
                    if st["size"] == 0 and st["dropped"] == 0:
                        continue
                    line = f"   · {name}: {st['size']}/{st['maxsize']} ({st['pct']}%)"
                    if st["peak"] > st["size"]:
                        line += f" | peak={st['peak']}"
                    if st["dropped"] > 0:
                        line += f" | ⚠️ dropped={st['dropped']}"
                    active_lines.append(line)
                if active_lines:
                    lines.append("• Queue:")
                    lines.extend(active_lines)
                else:
                    lines.append("• Queue: (trống)")
    except Exception as e:
        lines.append(f"• (Không đọc được queue stats: {e})")

    return "\n".join(lines)


def register_default_commands():
    command_registry.register("status", _build_status_report, timeout=8.0)
