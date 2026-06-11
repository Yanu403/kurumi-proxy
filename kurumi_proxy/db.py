from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kurumi_proxy.config import Settings


PROVIDER = "codebuddy"


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return utc_now().isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class Connection:
    id: str
    provider: str
    name: str
    api_key: str
    priority: int
    is_active: bool
    last_used_at: str | None
    consecutive_use_count: int
    cooldown_until: str | None
    model_locks: dict[str, str]
    backoff_level: int
    last_error: str | None
    last_error_at: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Connection":
        raw_locks = row["model_locks"] or "{}"
        try:
            model_locks = json.loads(raw_locks)
            if not isinstance(model_locks, dict):
                model_locks = {}
        except json.JSONDecodeError:
            model_locks = {}
        return cls(
            id=row["id"],
            provider=row["provider"],
            name=row["name"],
            api_key=row["api_key"],
            priority=int(row["priority"]),
            is_active=bool(row["is_active"]),
            last_used_at=row["last_used_at"],
            consecutive_use_count=int(row["consecutive_use_count"] or 0),
            cooldown_until=row["cooldown_until"],
            model_locks=model_locks,
            backoff_level=int(row["backoff_level"] or 0),
            last_error=row["last_error"],
            last_error_at=row["last_error_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def safe_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "name": self.name,
            "priority": self.priority,
            "is_active": self.is_active,
            "last_used_at": self.last_used_at,
            "consecutive_use_count": self.consecutive_use_count,
            "cooldown_until": self.cooldown_until,
            "model_locks": self.model_locks,
            "backoff_level": self.backoff_level,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class ConnectionStore:
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
                CREATE TABLE IF NOT EXISTS connections (
                    id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL DEFAULT 'codebuddy',
                    name TEXT NOT NULL,
                    api_key TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 100,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    last_used_at TEXT,
                    consecutive_use_count INTEGER NOT NULL DEFAULT 0,
                    cooldown_until TEXT,
                    model_locks TEXT NOT NULL DEFAULT '{}',
                    backoff_level INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    last_error_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS usage_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    connection_id TEXT,
                    connection_name TEXT,
                    api_key_name TEXT,
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
            count = db.execute("SELECT COUNT(*) AS count FROM connections").fetchone()["count"]
            if count == 0 and self.settings.codebuddy_api_key:
                now = iso_now()
                db.execute(
                    """
                    INSERT INTO connections
                    (id, provider, name, api_key, priority, is_active, model_locks, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "env-default",
                        PROVIDER,
                        "env-default",
                        self.settings.codebuddy_api_key,
                        100,
                        1,
                        "{}",
                        now,
                        now,
                    ),
                )
            db.commit()
        self._initialized = True

    def list_connections(self, include_inactive: bool = True) -> list[Connection]:
        self.init()
        query = "SELECT * FROM connections"
        if not include_inactive:
            query += " WHERE is_active=1"
        query += " ORDER BY priority ASC, name ASC"
        with self.connect() as db:
            return [Connection.from_row(row) for row in db.execute(query).fetchall()]

    def get_connection(self, connection_id: str) -> Connection | None:
        self.init()
        with self.connect() as db:
            row = db.execute("SELECT * FROM connections WHERE id=?", (connection_id,)).fetchone()
        return Connection.from_row(row) if row else None

    def create_connection(self, *, name: str, api_key: str, priority: int = 100, is_active: bool = True) -> Connection:
        self.init()
        connection_id = f"conn-{uuid.uuid4().hex[:16]}"
        now = iso_now()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO connections
                (id, provider, name, api_key, priority, is_active, model_locks, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (connection_id, PROVIDER, name, api_key, priority, int(is_active), "{}", now, now),
            )
            db.commit()
        created = self.get_connection(connection_id)
        assert created is not None
        return created

    def update_connection(self, connection_id: str, **updates: Any) -> Connection | None:
        self.init()
        allowed = {"name", "api_key", "priority", "is_active"}
        fields: list[str] = []
        values: list[Any] = []
        for key, value in updates.items():
            if key not in allowed or value is None:
                continue
            fields.append(f"{key}=?")
            values.append(int(value) if key == "is_active" else value)
        if not fields:
            return self.get_connection(connection_id)
        fields.append("updated_at=?")
        values.append(iso_now())
        values.append(connection_id)
        with self.connect() as db:
            db.execute(f"UPDATE connections SET {', '.join(fields)} WHERE id=?", values)
            db.commit()
        return self.get_connection(connection_id)

    def deactivate_connection(self, connection_id: str) -> Connection | None:
        return self.update_connection(connection_id, is_active=False)

    def reset_connection(self, connection_id: str) -> Connection | None:
        self.init()
        with self.connect() as db:
            db.execute(
                """
                UPDATE connections
                SET cooldown_until=NULL, model_locks='{}', backoff_level=0,
                    last_error=NULL, last_error_at=NULL, updated_at=?
                WHERE id=?
                """,
                (iso_now(), connection_id),
            )
            db.commit()
        return self.get_connection(connection_id)

    def mark_success(self, connection: Connection) -> None:
        self.init()
        previous = self.get_connection(connection.id)
        count = (previous.consecutive_use_count if previous else connection.consecutive_use_count) + 1
        with self.connect() as db:
            db.execute(
                """
                UPDATE connections
                SET last_used_at=?, consecutive_use_count=?, backoff_level=0,
                    last_error=NULL, last_error_at=NULL, updated_at=?
                WHERE id=?
                """,
                (iso_now(), count, iso_now(), connection.id),
            )
            db.commit()

    def mark_failure(
        self,
        connection: Connection,
        *,
        model: str,
        error: str,
        category: str,
    ) -> None:
        self.init()
        current = self.get_connection(connection.id) or connection
        now = utc_now()
        locks = dict(current.model_locks)
        cooldown_until: str | None = current.cooldown_until
        backoff = current.backoff_level

        if category in {"quota", "credit"}:
            locks["__all"] = (now + timedelta(hours=24)).isoformat()
            backoff = max(backoff, 1)
        elif category == "auth":
            locks["__all"] = (now + timedelta(hours=1)).isoformat()
            backoff = max(backoff, 1)
        elif category in {"rate_limit", "overload"}:
            backoff = min(backoff + 1, 4)
            delay = min(60 * (2 ** (backoff - 1)), 15 * 60)
            locks[model] = (now + timedelta(seconds=delay)).isoformat()
            cooldown_until = (now + timedelta(seconds=delay)).isoformat()
        else:
            backoff = min(backoff + 1, 4)
            locks[model] = (now + timedelta(minutes=2)).isoformat()

        with self.connect() as db:
            db.execute(
                """
                UPDATE connections
                SET cooldown_until=?, model_locks=?, backoff_level=?, last_error=?,
                    last_error_at=?, updated_at=?
                WHERE id=?
                """,
                (cooldown_until, json.dumps(locks, sort_keys=True), backoff, error[:500], iso_now(), iso_now(), connection.id),
            )
            db.commit()

    def record_usage(
        self,
        *,
        model: str,
        connection: Connection | None,
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
        api_key_name: str | None = None,
    ) -> None:
        self.init()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO usage_history
                (timestamp, provider, model, connection_id, connection_name, api_key_name, endpoint,
                 prompt_tokens, completion_tokens, total_tokens, status, error, duration_ms,
                 rtk_before_bytes, rtk_after_bytes, rtk_saved_bytes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    iso_now(),
                    PROVIDER,
                    model,
                    connection.id if connection else None,
                    connection.name if connection else None,
                    api_key_name,
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
                SELECT substr(timestamp, 1, 10) AS day, model, connection_id, connection_name,
                       COUNT(*) AS requests,
                       SUM(prompt_tokens) AS prompt_tokens,
                       SUM(completion_tokens) AS completion_tokens,
                       SUM(total_tokens) AS total_tokens,
                       SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors,
                       SUM(COALESCE(rtk_saved_bytes, 0)) AS rtk_saved_bytes
                FROM usage_history
                WHERE timestamp >= ?
                GROUP BY day, model, connection_id, connection_name
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
                    "SELECT COUNT(*) requests, SUM(prompt_tokens) prompt_tokens, SUM(completion_tokens) completion_tokens, SUM(total_tokens) total_tokens FROM usage_history WHERE substr(timestamp,1,10)=?",
                    (today,),
                ).fetchone(),
                "last_7d": db.execute(
                    "SELECT COUNT(*) requests, SUM(prompt_tokens) prompt_tokens, SUM(completion_tokens) completion_tokens, SUM(total_tokens) total_tokens FROM usage_history WHERE timestamp>=?",
                    (since_7d,),
                ).fetchone(),
                "all_time": db.execute(
                    "SELECT COUNT(*) requests, SUM(prompt_tokens) prompt_tokens, SUM(completion_tokens) completion_tokens, SUM(total_tokens) total_tokens FROM usage_history",
                ).fetchone(),
            }
            last_rows = db.execute(
                """
                SELECT connection_id,
                       MAX(CASE WHEN status='success' THEN timestamp END) AS last_success_at,
                       MAX(CASE WHEN status='error' THEN timestamp END) AS last_error_at
                FROM usage_history
                GROUP BY connection_id
                """
            ).fetchall()
        last_by_connection = {row["connection_id"]: dict(row) for row in last_rows}

        def clean(row: sqlite3.Row) -> dict[str, int]:
            return {
                "requests": int(row["requests"] or 0),
                "prompt_tokens": int(row["prompt_tokens"] or 0),
                "completion_tokens": int(row["completion_tokens"] or 0),
                "total_tokens": int(row["total_tokens"] or 0),
            }

        connections = []
        for connection in self.list_connections(include_inactive=True):
            item = connection.safe_dict()
            item.update(last_by_connection.get(connection.id, {}))
            connections.append(item)

        return {
            "credit_balance_known": False,
            "note": "CodeBuddy credit balance is not exposed here; totals are local estimates.",
            "totals": {name: clean(row) for name, row in totals.items()},
            "connections": connections,
        }
