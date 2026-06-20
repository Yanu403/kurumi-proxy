from __future__ import annotations

import sqlite3
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kurumi_proxy.config import Settings


PROVIDER = "merlin"


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return utc_now().isoformat()


class UsageStore:
    """SQLite-backed usage history tracker."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.path = Path(settings.kurumi_proxy_db_path)
        self._initialized = False

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with suppress(OSError):
            self.path.parent.chmod(0o700)
        db = sqlite3.connect(self.path)
        with suppress(OSError):
            self.path.chmod(0o600)
        db.row_factory = sqlite3.Row
        return db

    def init(self) -> None:
        if self._initialized:
            return
        with self.connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS usage_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    error TEXT,
                    duration_ms INTEGER,
                    rtk_before_bytes INTEGER,
                    rtk_after_bytes INTEGER,
                    rtk_saved_bytes INTEGER
                )
                """
            )
            db.commit()
        self._initialized = True

    def record_usage(
        self,
        *,
        model: str,
        endpoint: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        status: str,
        error: str | None = None,
        duration_ms: int | None = None,
        rtk_before_bytes: int | None = None,
        rtk_after_bytes: int | None = None,
        rtk_saved_bytes: int | None = None,
    ) -> None:
        self.init()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO usage_history
                (timestamp, provider, model, endpoint,
                 prompt_tokens, completion_tokens, total_tokens, status, error, duration_ms,
                 rtk_before_bytes, rtk_after_bytes, rtk_saved_bytes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    iso_now(),
                    PROVIDER,
                    model,
                    endpoint,
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    status,
                    error[:500] if error else None,
                    duration_ms,
                    rtk_before_bytes,
                    rtk_after_bytes,
                    rtk_saved_bytes,
                ),
            )
            db.commit()

    def usage_summary(self, days: int = 7) -> dict[str, Any]:
        self.init()
        days = max(1, min(days, 3650))
        since = (utc_now() - timedelta(days=days)).isoformat()
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT substr(timestamp, 1, 10) AS day, model,
                       COUNT(*) AS requests,
                       SUM(prompt_tokens) AS prompt_tokens,
                       SUM(completion_tokens) AS completion_tokens,
                       SUM(total_tokens) AS total_tokens,
                       SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors,
                       SUM(COALESCE(rtk_saved_bytes, 0)) AS rtk_saved_bytes
                FROM usage_history
                WHERE timestamp >= ?
                GROUP BY day, model
                ORDER BY day DESC, requests DESC
                """,
                (since,),
            ).fetchall()
        return {
            "days": days,
            "items": [dict(row) for row in rows],
        }

    def quota_summary(self) -> dict[str, Any]:
        self.init()
        today = utc_now().date().isoformat()
        since_7d = (utc_now() - timedelta(days=7)).isoformat()
        with self.connect() as db:
            totals = {
                "today": db.execute(
                    "SELECT COUNT(*) requests, SUM(prompt_tokens) prompt_tokens, "
                    "SUM(completion_tokens) completion_tokens, SUM(total_tokens) total_tokens "
                    "FROM usage_history WHERE substr(timestamp,1,10)=?",
                    (today,),
                ).fetchone(),
                "last_7d": db.execute(
                    "SELECT COUNT(*) requests, SUM(prompt_tokens) prompt_tokens, "
                    "SUM(completion_tokens) completion_tokens, SUM(total_tokens) total_tokens "
                    "FROM usage_history WHERE timestamp>=?",
                    (since_7d,),
                ).fetchone(),
                "all_time": db.execute(
                    "SELECT COUNT(*) requests, SUM(prompt_tokens) prompt_tokens, "
                    "SUM(completion_tokens) completion_tokens, SUM(total_tokens) total_tokens "
                    "FROM usage_history",
                ).fetchone(),
            }

        def clean(row: sqlite3.Row) -> dict[str, int]:
            return {
                "requests": int(row["requests"] or 0),
                "prompt_tokens": int(row["prompt_tokens"] or 0),
                "completion_tokens": int(row["completion_tokens"] or 0),
                "total_tokens": int(row["total_tokens"] or 0),
            }

        return {
            "totals": {name: clean(row) for name, row in totals.items()},
        }
