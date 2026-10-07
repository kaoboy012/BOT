"""
📊 DATABASE MANAGEMENT (v4.0 - OPTIMIZED)
- WAL mode: đọc/ghi song song không block nhau
- Connection pool riêng cho async context
- Batch write: gom nhiều record ghi 1 lần thay vì từng dòng
- Prepared statements cache
"""

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from logger_setup import logger


class CodeDatabase:
    """Quản lý database SQLite - tối ưu tốc độ cao"""

    def __init__(self, db_path: str = "data/code_history.db", stats_batch_size: int = 50):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.stats_batch_size = max(1, int(stats_batch_size))
        self._pending_account_stats: dict[str, dict] = {}
        self._pending_website_stats: dict[str, dict] = {}
        self._pending_stats_events = 0
        # Timeout kết nối khớp PRAGMA busy_timeout=30000 bên dưới (cùng 30s).
        self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self._optimize_connection()
        self._init_tables()


    def _optimize_connection(self):
        """Bật các pragma tăng tốc SQLite đáng kể"""
        pragmas = [
            "PRAGMA journal_mode=WAL",        # Cho phép đọc/ghi song song
            "PRAGMA synchronous=NORMAL",       # Nhanh hơn FULL, vẫn an toàn
            "PRAGMA cache_size=-32000",        # 32MB cache trong RAM
            "PRAGMA temp_store=MEMORY",        # Temp tables trong RAM
            "PRAGMA mmap_size=268435456",      # 256MB memory-mapped I/O
            "PRAGMA wal_autocheckpoint=5000",  # Giảm checkpoint giữa các burst ghi
            "PRAGMA busy_timeout=30000",       # Tự retry 30s khi bị lock — khớp timeout kết nối
        ]
        for pragma in pragmas:
            try:
                self.conn.execute(pragma)
            except Exception:
                pass
        self.conn.commit()

    def _init_tables(self):
        """Tạo bảng và index"""
        try:
            with self._lock:
                self.conn.execute("""
                    CREATE TABLE IF NOT EXISTS code_submission (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        code TEXT NOT NULL,
                        account TEXT NOT NULL,
                        website TEXT NOT NULL,
                        status TEXT,
                        result TEXT,
                        submitted_at TIMESTAMP,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(code, account, website)
                    )
                """)
                # DEDUP VINH VIEN: 1 code chi xu ly 1 lan / domain
                # Older versions used UNIQUE(code, account), overwriting a
                # result when the same account/code was submitted to two sites.
                table_sql = self.conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='code_submission'"
                ).fetchone()[0]
                if "unique(code,account)" in "".join(table_sql.lower().split()):
                    self.conn.execute("BEGIN IMMEDIATE")
                    try:
                        self.conn.execute("ALTER TABLE code_submission RENAME TO code_submission_old")
                        self.conn.execute("""CREATE TABLE code_submission (
                            id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL,
                            account TEXT NOT NULL, website TEXT NOT NULL, status TEXT,
                            result TEXT, submitted_at TIMESTAMP,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            UNIQUE(code, account, website))""")
                        self.conn.execute("INSERT INTO code_submission SELECT * FROM code_submission_old")
                        self.conn.execute("DROP TABLE code_submission_old")
                        self.conn.commit()
                    except BaseException:
                        self.conn.rollback()
                        raise
                self.conn.execute("""
                    CREATE TABLE IF NOT EXISTS used_codes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        domain TEXT NOT NULL,
                        code TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(domain, code)
                    )
                """)
                self.conn.execute("""
                    CREATE TABLE IF NOT EXISTS submission_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        code TEXT NOT NULL,
                        account TEXT NOT NULL,
                        website TEXT NOT NULL,
                        status TEXT,
                        result TEXT,
                        attempt INTEGER,
                        submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                self.conn.execute("""
                    CREATE TABLE IF NOT EXISTS account_stats (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        account TEXT NOT NULL UNIQUE,
                        total_submitted INTEGER DEFAULT 0,
                        total_success INTEGER DEFAULT 0,
                        total_failed INTEGER DEFAULT 0,
                        last_submit TIMESTAMP,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                self.conn.execute("""
                    CREATE TABLE IF NOT EXISTS website_stats (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        website TEXT NOT NULL UNIQUE,
                        total_submitted INTEGER DEFAULT 0,
                        total_success INTEGER DEFAULT 0,
                        total_failed INTEGER DEFAULT 0,
                        last_submit TIMESTAMP,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                self.conn.execute("CREATE INDEX IF NOT EXISTS idx_code ON code_submission(code)")
                self.conn.execute("CREATE INDEX IF NOT EXISTS idx_account ON submission_log(account)")
                self.conn.execute("CREATE INDEX IF NOT EXISTS idx_website ON submission_log(website)")
                self.conn.execute("CREATE INDEX IF NOT EXISTS idx_submitted_at ON submission_log(submitted_at)")
                self.conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_submission_status_time "
                    "ON submission_log(status, submitted_at)"
                )
                self.conn.execute("""
                    CREATE TABLE IF NOT EXISTS account_rotation (
                        domain TEXT PRIMARY KEY,
                        rotation_date TEXT NOT NULL,
                        cursor INTEGER NOT NULL DEFAULT 0,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                self.conn.execute("""
                    CREATE TABLE IF NOT EXISTS account_exhausted (
                        domain TEXT NOT NULL,
                        account TEXT NOT NULL,
                        rotation_date TEXT NOT NULL,
                        marked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (domain, account, rotation_date)
                    )
                """)
                # Covering index cho get_account_rotation(): vừa lọc theo
                # domain/ngày vừa trả account mà không cần lookup lại bảng.
                # Xóa index cũ (domain, rotation_date) để không nhân đôi chi
                # phí ghi khi số account/domain tăng cao.
                self.conn.execute("DROP INDEX IF EXISTS idx_account_exhausted_day")
                self.conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_account_exhausted_lookup "
                    "ON account_exhausted(domain, rotation_date, account)"
                )
                self.conn.commit()
            logger.info("✅ Database tables khởi tạo xong (WAL mode)")
        except Exception as e:
            logger.error(f"❌ Lỗi tạo tables: {e}")
            raise

    def get_account_rotation(self, domain: str, rotation_date: str):
        """Load the persistent round-robin cursor and exhausted accounts."""
        with self._lock:
            try:
                row = self.conn.execute(
                    "SELECT cursor FROM account_rotation "
                    "WHERE domain = ? AND rotation_date = ?",
                    (domain, rotation_date),
                ).fetchone()
                exhausted = self.conn.execute(
                    "SELECT account FROM account_exhausted "
                    "WHERE domain = ? AND rotation_date = ?",
                    (domain, rotation_date),
                ).fetchall()
                return (int(row[0]) if row else 0, {str(item[0]) for item in exhausted})
            except Exception as e:
                logger.error("❌ Lỗi đọc account rotation: %s", e)
                return 0, set()

    def save_account_cursor(self, domain: str, cursor: int, rotation_date: str | None = None):
        """Persist the next account index for the current rotation day."""
        day = rotation_date or datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            try:
                self.conn.execute(
                    "INSERT INTO account_rotation(domain, rotation_date, cursor, updated_at) "
                    "VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
                    "ON CONFLICT(domain) DO UPDATE SET "
                    "rotation_date=excluded.rotation_date, cursor=excluded.cursor, "
                    "updated_at=CURRENT_TIMESTAMP",
                    (domain, day, int(cursor)),
                )
                self.conn.commit()
            except Exception as e:
                logger.error("❌ Lỗi lưu account cursor: %s", e)
                self.conn.rollback()

    def mark_account_exhausted(self, domain: str, account: str, rotation_date: str):
        """Persist that an account reached its daily site limit."""
        with self._lock:
            try:
                self.conn.execute(
                    "INSERT OR IGNORE INTO account_exhausted(domain, account, rotation_date) "
                    "VALUES (?, ?, ?)",
                    (domain, account, rotation_date),
                )
                self.conn.commit()
            except Exception as e:
                logger.error("❌ Lỗi lưu account exhausted: %s", e)
                self.conn.rollback()

    def _queue_stats_locked(self, account: str, website: str, status: str, now: datetime) -> None:
        """Accumulate dashboard counters without a commit per submission."""
        account_row = self._pending_account_stats.setdefault(
            account, {"total": 0, "success": 0, "failed": 0, "last_submit": now}
        )
        website_row = self._pending_website_stats.setdefault(
            website, {"total": 0, "success": 0, "failed": 0, "last_submit": now}
        )
        for row in (account_row, website_row):
            row["total"] += 1
            row["last_submit"] = now
            if status == "SUCCESS":
                row["success"] += 1
            elif status == "FAILED":
                row["failed"] += 1
        self._pending_stats_events += 1

    def _flush_stats_locked(self) -> int:
        if not self._pending_stats_events:
            return 0
        account_rows = list(self._pending_account_stats.items())
        website_rows = list(self._pending_website_stats.items())
        events = self._pending_stats_events
        try:
            self.conn.executemany(
                """INSERT INTO account_stats
                   (account, total_submitted, total_success, total_failed, last_submit)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(account) DO UPDATE SET
                     total_submitted=account_stats.total_submitted + excluded.total_submitted,
                     total_success=account_stats.total_success + excluded.total_success,
                     total_failed=account_stats.total_failed + excluded.total_failed,
                     last_submit=excluded.last_submit""",
                [(key, row["total"], row["success"], row["failed"], row["last_submit"])
                 for key, row in account_rows],
            )
            self.conn.executemany(
                """INSERT INTO website_stats
                   (website, total_submitted, total_success, total_failed, last_submit)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(website) DO UPDATE SET
                     total_submitted=website_stats.total_submitted + excluded.total_submitted,
                     total_success=website_stats.total_success + excluded.total_success,
                     total_failed=website_stats.total_failed + excluded.total_failed,
                     last_submit=excluded.last_submit""",
                [(key, row["total"], row["success"], row["failed"], row["last_submit"])
                 for key, row in website_rows],
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        self._pending_account_stats.clear()
        self._pending_website_stats.clear()
        self._pending_stats_events = 0
        return events


    def record_submission(self, code: str, account: str, website: str,
                          status: str, result: str = None, attempt: int = 1):
        """Ghi submission - thread-safe, non-blocking với WAL"""
        with self._lock:
            try:
                now = datetime.now()
                self.conn.execute("""
                    INSERT INTO submission_log (code, account, website, status, result, attempt)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (code, account, website, status, result, attempt))

                self.conn.execute("""
                    INSERT OR REPLACE INTO code_submission
                    (code, account, website, status, result, submitted_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (code, account, website, status, result, now))

                self.conn.commit()
                self._queue_stats_locked(account, website, status, now)
                if self._pending_stats_events >= self.stats_batch_size:
                    self._flush_stats_locked()
                logger.debug(f"💾 [{account}] Code {code}: {status}")

            except sqlite3.IntegrityError as e:
                logger.warning(f"⚠️ IntegrityError không mong đợi trong record_submission: {e}")
                try:
                    self.conn.rollback()
                except Exception:
                    pass
            except Exception as e:
                logger.error(f"❌ Lỗi record submission: {e}")
                try:
                    self.conn.rollback()
                except Exception:
                    pass

    # DEDUP VĨNH VIỄN - 1 code chỉ xử lý 1 lần / domain.
    # Không tự động xóa used_codes trong vacuum; chỉ clear thủ công khi cần.
    def is_code_used(self, domain: str, code: str) -> bool:
        with self._lock:
            try:
                row = self.conn.execute(
                    "SELECT 1 FROM used_codes WHERE domain = ? AND code = ?",
                    (domain, code.upper())
                ).fetchone()
                return row is not None
            except Exception as e:
                # Fail-safe: nếu DB lỗi, bỏ qua code thay vì mạo hiểm submit trùng.
                logger.error(
                    f"❌ Lỗi check used_codes, coi như đã dùng để tránh trùng: {e}"
                )
                return True

    def unused_codes(self, domain: str, codes) -> set[str]:
        """Return codes not yet used for ``domain`` in one SQLite round-trip.

        The message path commonly receives several codes from one Telegram
        post. Checking them one by one through the executor added avoidable
        event-loop/executor latency. On database failure this fails closed and
        returns an empty set, preserving the existing no-duplicate guarantee.
        """
        normalized = list(dict.fromkeys(
            str(code or "").strip().upper() for code in (codes or []) if str(code or "").strip()
        ))
        if not normalized:
            return set()
        placeholders = ",".join("?" for _ in normalized)
        with self._lock:
            try:
                rows = self.conn.execute(
                    f"SELECT code FROM used_codes WHERE domain = ? AND code IN ({placeholders})",
                    (domain, *normalized),
                ).fetchall()
                used = {str(row[0]).upper() for row in rows}
                return {code for code in normalized if code not in used}
            except Exception as e:
                logger.error("❌ Lỗi batch check used_codes, bỏ qua toàn bộ batch để tránh submit trùng: %s", e)
                return set()

    def mark_code_used(self, domain: str, code: str) -> bool:
        """True = vua mark thanh cong (code moi). False = code da dung truoc do."""
        with self._lock:
            try:
                self.conn.execute(
                    "INSERT INTO used_codes (domain, code) VALUES (?, ?)",
                    (domain, code.upper())
                )
                self.conn.commit()
                return True
            except sqlite3.IntegrityError:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                return False
            except Exception as e:
                logger.error(f"❌ Lỗi mark_code_used: {e}")
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                return False

    # ✅ Clear dedup vĩnh viễn cho 1 domain (fix code bị dính) — công cụ bảo
    # trì thủ công, gọi tay khi cần (không nằm trong luồng tự động của bot),
    # ví dụ: get_database().clear_domain_dedup("qq88.com") từ 1 script riêng.

    def vacuum(self):
        """Dọn log cũ và chạy VACUUM thật; giữ used_codes để dedup vĩnh viễn."""
        try:
            with self._lock:
                self.conn.execute(
                    "DELETE FROM submission_log WHERE submitted_at < datetime('now', '-30 days')"
                )
                self.conn.commit()
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self.conn.execute("VACUUM")
                # In WAL mode, checkpoint again so the compacted image reaches the .db file.
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            logger.info("✅ Database vacuum + cleanup cũ xong")
        except Exception as e:
            logger.warning(f"⚠️ DB vacuum error: {e}")

    def close(self):
        with self._lock:
            flush_error = None
            try:
                self._flush_stats_locked()
            except Exception as e:
                flush_error = e
                logger.error(f"❌ Lỗi flush batch khi đóng database: {e}")
            finally:
                try:
                    self.conn.close()
                    logger.info("✅ Database đã đóng")
                except Exception as e:
                    logger.error(f"❌ Lỗi close database: {e}")
            if flush_error is not None:
                # Không làm crash luồng shutdown; critical submission data đã
                # commit trước khi stats được xếp hàng. Batch dashboard sẽ mất
                # nếu flush thất bại, nhưng lỗi đã được ghi log rõ ràng.
                return False
            return True


_db_instance = None

def init_database(db_path: str = "data/code_history.db") -> CodeDatabase:
    global _db_instance
    if _db_instance is None:
        _db_instance = CodeDatabase(db_path)
    elif Path(_db_instance.db_path).expanduser().resolve() != Path(db_path).expanduser().resolve():
        raise RuntimeError(
            "Database singleton đã được khởi tạo với path khác: "
            f"{_db_instance.db_path!r} != {db_path!r}"
        )
    return _db_instance

