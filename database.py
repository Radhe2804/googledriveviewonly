"""
SQLite Database — logs all jobs, rate limiting, user stats
"""

import sqlite3
import time
import logging
from config import Config

logger = logging.getLogger(__name__)


class Database:
    def __init__(self):
        self.db_path = Config.DB_PATH
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        # Fix #13 — warn if DB is on /tmp (ephemeral on Render, resets on redeploy)
        if self.db_path.startswith("/tmp"):
            logger.warning(
                "⚠️  DB is on /tmp — data will reset on Render redeploy! "
                "Set DB_PATH to a Render Disk path (e.g. /data/bot.db) for persistence."
            )
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id          TEXT PRIMARY KEY,
                    user_id     INTEGER NOT NULL,
                    url         TEXT NOT NULL,
                    status      TEXT DEFAULT 'started',
                    pages       INTEGER DEFAULT 0,
                    size_mb     REAL DEFAULT 0,
                    error       TEXT,
                    started_at  INTEGER NOT NULL,
                    finished_at INTEGER
                );

                CREATE TABLE IF NOT EXISTS rate_limits (
                    user_id    INTEGER NOT NULL,
                    timestamp  INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_rate_limits_user
                    ON rate_limits(user_id, timestamp);
            """)
        logger.info("Database initialized.")

    # ─────────────────────────────────────────
    # Rate Limiting
    # ─────────────────────────────────────────
    def check_rate_limit(self, user_id: int) -> bool:
        """Returns True if user is within rate limit."""
        now = int(time.time())
        one_hour_ago = now - 3600

        with self._connect() as conn:
            # Clean old entries
            conn.execute(
                "DELETE FROM rate_limits WHERE user_id = ? AND timestamp < ?",
                (user_id, one_hour_ago)
            )

            # Count recent requests
            count = conn.execute(
                "SELECT COUNT(*) FROM rate_limits WHERE user_id = ? AND timestamp > ?",
                (user_id, one_hour_ago)
            ).fetchone()[0]

            if count >= Config.MAX_JOBS_PER_HOUR:
                return False

            # Log this request
            conn.execute(
                "INSERT INTO rate_limits (user_id, timestamp) VALUES (?, ?)",
                (user_id, now)
            )
            return True

    # ─────────────────────────────────────────
    # Job Logging
    # ─────────────────────────────────────────
    def log_job_start(self, user_id: int, url: str) -> str:
        import uuid
        job_id = str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO jobs (id, user_id, url, status, started_at) VALUES (?, ?, ?, 'started', ?)",
                (job_id, user_id, url, int(time.time()))
            )
        return job_id

    def log_job_success(self, job_id: str, pages: int, size_mb: float):
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET status='success', pages=?, size_mb=?, finished_at=? WHERE id=?",
                (pages, size_mb, int(time.time()), job_id)
            )

    def log_job_failed(self, job_id: str, error: str):
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET status='failed', error=?, finished_at=? WHERE id=?",
                (error[:500], int(time.time()), job_id)
            )

    # ─────────────────────────────────────────
    # Stats
    # ─────────────────────────────────────────
    def get_user_stats(self, user_id: int) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as total_jobs, SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as total_pdfs "
                "FROM jobs WHERE user_id = ?",
                (user_id,)
            ).fetchone()
            return {
                "total_jobs": row["total_jobs"] or 0,
                "total_pdfs": row["total_pdfs"] or 0
            }

    def get_global_stats(self) -> dict:
        with self._connect() as conn:
            row = conn.execute("""
                SELECT
                    COUNT(DISTINCT user_id) as total_users,
                    COUNT(*) as total_jobs,
                    SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as successful,
                    SUM(CASE WHEN status='failed'  THEN 1 ELSE 0 END) as failed
                FROM jobs
            """).fetchone()
            return {
                "total_users": row["total_users"] or 0,
                "total_jobs": row["total_jobs"] or 0,
                "successful": row["successful"] or 0,
                "failed": row["failed"] or 0
            }
