from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from moviemax.models import ChangeSummary, OutboxEvent, Screening


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class StateStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS screenings (
                    screening_key TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    free_seats INTEGER NOT NULL,
                    total_seats INTEGER NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    sent_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    delivered_parts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT,
                    dead_lettered_at TEXT,
                    last_error TEXT
                );

                """
            )
            self._ensure_column(
                connection,
                "outbox",
                "delivered_parts",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(connection, "outbox", "next_attempt_at", "TEXT")
            self._ensure_column(connection, "outbox", "dead_lettered_at", "TEXT")
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_outbox_pending
                ON outbox(sent_at, dead_lettered_at, next_attempt_at, id)
                """
            )

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        existing = {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def apply_snapshot(
        self,
        screenings: Iterable[Screening],
        *,
        notify_on_initial_state: bool,
    ) -> ChangeSummary:
        current = {screening.key: screening for screening in screenings}
        observed_at = _now()
        new_screenings: list[Screening] = []
        booking_opened: list[Screening] = []
        seat_increases: list[dict[str, Any]] = []

        with self._connection() as connection:
            initialized_row = connection.execute(
                "SELECT value FROM metadata WHERE key = 'initialized'"
            ).fetchone()
            initialized = (
                initialized_row is not None and initialized_row["value"] == "1"
            )

            for key, screening in current.items():
                previous = connection.execute(
                    """
                    SELECT payload_json, free_seats, first_seen_at
                    FROM screenings
                    WHERE screening_key = ?
                    """,
                    (key,),
                ).fetchone()
                if previous is None:
                    if screening.is_sale_open:
                        new_screenings.append(screening)
                    first_seen_at = observed_at
                else:
                    old_screening = Screening.from_dict(
                        json.loads(str(previous["payload_json"]))
                    )
                    old_free = int(previous["free_seats"])
                    if not old_screening.is_sale_open and screening.is_sale_open:
                        booking_opened.append(screening)
                    elif screening.is_sale_open and screening.free_seats > old_free:
                        seat_increases.append(
                            {
                                "screening": screening.to_dict(),
                                "previous_free_seats": old_free,
                            }
                        )
                    first_seen_at = str(previous["first_seen_at"])

                connection.execute(
                    """
                    INSERT INTO screenings (
                        screening_key, payload_json, free_seats, total_seats,
                        first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(screening_key) DO UPDATE SET
                        payload_json = excluded.payload_json,
                        free_seats = excluded.free_seats,
                        total_seats = excluded.total_seats,
                        last_seen_at = excluded.last_seen_at
                    """,
                    (
                        key,
                        _canonical_json(screening.to_dict()),
                        screening.free_seats,
                        screening.total_seats,
                        first_seen_at,
                        observed_at,
                    ),
                )

            should_notify_new = initialized or notify_on_initial_state
            if new_screenings and should_notify_new:
                self._enqueue(
                    connection,
                    "new_screenings",
                    {"screenings": [item.to_dict() for item in new_screenings]},
                    observed_at,
                )
            if booking_opened:
                self._enqueue(
                    connection,
                    "booking_opened",
                    {"screenings": [item.to_dict() for item in booking_opened]},
                    observed_at,
                )
            if seat_increases:
                self._enqueue(
                    connection,
                    "seat_increases",
                    {"changes": seat_increases},
                    observed_at,
                )

            self._set_metadata(connection, "initialized", "1")
            self._set_metadata(connection, "last_success_at", observed_at)

        return ChangeSummary(
            initialized_before_poll=initialized,
            screening_count=len(current),
            new_screening_count=len(new_screenings) if should_notify_new else 0,
            booking_opened_count=len(booking_opened),
            seat_increase_count=len(seat_increases),
        )

    @staticmethod
    def _set_metadata(
        connection: sqlite3.Connection,
        key: str,
        value: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO metadata(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    def get_metadata(self, key: str) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = ?",
                (key,),
            ).fetchone()
        return str(row["value"]) if row is not None else None

    def _enqueue(
        self,
        connection: sqlite3.Connection,
        kind: str,
        payload: dict[str, Any],
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO outbox(event_key, kind, payload_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                f"{kind}:{uuid4().hex}",
                kind,
                _canonical_json(payload),
                created_at,
            ),
        )

    def pending_events(self, limit: int = 50) -> list[OutboxEvent]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, event_key, kind, payload_json, attempts, delivered_parts
                FROM outbox
                WHERE sent_at IS NULL
                  AND dead_lettered_at IS NULL
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY id
                LIMIT ?
                """,
                (_now(), limit),
            ).fetchall()
        return [OutboxEvent.from_row(row) for row in rows]

    def mark_part_delivered(self, event_id: int, delivered_parts: int) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE outbox
                SET delivered_parts = ?, last_error = NULL, next_attempt_at = NULL
                WHERE id = ?
                """,
                (delivered_parts, event_id),
            )

    def mark_sent(self, event_id: int) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE outbox
                SET sent_at = ?, last_error = NULL, next_attempt_at = NULL
                WHERE id = ?
                """,
                (_now(), event_id),
            )

    def mark_failed(self, event_id: int, error: str, retry_after_seconds: int) -> None:
        safe_error = error[:500]
        next_attempt = (
            datetime.now(UTC) + timedelta(seconds=retry_after_seconds)
        ).isoformat()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE outbox
                SET attempts = attempts + 1, last_error = ?, next_attempt_at = ?
                WHERE id = ?
                """,
                (safe_error, next_attempt, event_id),
            )

    def mark_dead(self, event_id: int, error: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE outbox
                SET attempts = attempts + 1, last_error = ?, dead_lettered_at = ?
                WHERE id = ?
                """,
                (error[:500], _now(), event_id),
            )

    def requeue_dead(self) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE outbox
                SET dead_lettered_at = NULL, next_attempt_at = NULL,
                    attempts = 0, last_error = NULL
                WHERE sent_at IS NULL AND dead_lettered_at IS NOT NULL
                """
            )
            return int(cursor.rowcount)

    def outbox_health(self) -> dict[str, int]:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(CASE WHEN sent_at IS NULL AND dead_lettered_at IS NULL THEN 1 END) AS pending,
                    COALESCE(MAX(CASE WHEN sent_at IS NULL AND dead_lettered_at IS NULL THEN attempts END), 0) AS max_attempts,
                    COUNT(CASE WHEN dead_lettered_at IS NOT NULL THEN 1 END) AS dead
                FROM outbox
                """
            ).fetchone()
        return {
            "pending": int(row["pending"]),
            "max_attempts": int(row["max_attempts"]),
            "dead": int(row["dead"]),
        }

    def health_check(self) -> None:
        with self._connection() as connection:
            result = connection.execute("PRAGMA quick_check").fetchone()
            if result is None or result[0] != "ok":
                raise RuntimeError("SQLite quick_check failed")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("SELECT 1")
            connection.rollback()
