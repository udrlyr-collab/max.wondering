from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

from cryptography.fernet import Fernet, InvalidToken

from moviemax.models import OutboxEvent, Screening
from moviemax.polling import jittered_delay_seconds, normalize_poll_jitter_seconds


class StaleVersionError(RuntimeError):
    """Raised when a write was based on an obsolete target/config version."""


MAX_SEAT_CHANGE_THRESHOLD = (1 << 53) - 1


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _timestamp(value: datetime | str | None = None) -> str:
    if value is None:
        parsed = datetime.now(UTC)
    elif isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ConsoleStore:
    """Persistent state for the single-admin MovieMax console."""

    ACTIVITY_KINDS: ClassVar[frozenset[str]] = frozenset(
        {
            "new_screenings",
            "booking_opened",
            "booking_closed",
            "seat_increases",
            "seat_decreases",
            "total_seats_changed",
            "screening_updated",
        }
    )

    _TARGET_FIELDS: ClassVar[set[str]] = {
        "company_code",
        "site_no",
        "site_name",
        "movie_no",
        "movie_name",
        "format_code",
        "format_keyword",
        "screen_grade_code",
        "enabled",
        "notify_new",
        "auto_track_new",
        "poll_interval_seconds",
        "poll_jitter_seconds",
        "next_poll_at",
    }
    _UPDATABLE_TARGET_FIELDS: ClassVar[set[str]] = {
        "site_name",
        "movie_name",
        "enabled",
        "notify_new",
        "auto_track_new",
        "poll_interval_seconds",
        "poll_jitter_seconds",
    }

    def __init__(self, path: Path | str, encryption_key: str) -> None:
        try:
            self._fernet = Fernet(encryption_key.encode("ascii"))
        except (UnicodeEncodeError, ValueError) as exc:
            raise ValueError("encryption_key must be a valid Fernet key") from exc
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    @contextmanager
    def _connection(
        self,
        *,
        immediate: bool = False,
    ) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            if immediate:
                connection.execute("BEGIN IMMEDIATE")
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
                CREATE TABLE IF NOT EXISTS watch_targets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    company_code TEXT NOT NULL,
                    site_no TEXT NOT NULL,
                    site_name TEXT NOT NULL,
                    movie_no TEXT NOT NULL,
                    movie_name TEXT NOT NULL,
                    format_code TEXT NOT NULL DEFAULT '',
                    format_keyword TEXT NOT NULL,
                    screen_grade_code TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
                    notify_new INTEGER NOT NULL DEFAULT 1 CHECK(notify_new IN (0, 1)),
                    auto_track_new INTEGER NOT NULL DEFAULT 0
                        CHECK(auto_track_new IN (0, 1)),
                    initialized INTEGER NOT NULL DEFAULT 0
                        CHECK(initialized IN (0, 1)),
                    state TEXT NOT NULL DEFAULT 'idle',
                    poll_interval_seconds INTEGER NOT NULL DEFAULT 60
                        CHECK(poll_interval_seconds >= 30),
                    poll_jitter_seconds INTEGER NOT NULL DEFAULT 5
                        CHECK(poll_jitter_seconds BETWEEN 0 AND 300),
                    next_poll_at TEXT,
                    refresh_requested_at TEXT,
                    last_started_at TEXT,
                    last_success_at TEXT,
                    last_failure_at TEXT,
                    last_error TEXT,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(
                        company_code, site_no, movie_no,
                        format_code, format_keyword, screen_grade_code
                    )
                );

                CREATE TABLE IF NOT EXISTS console_screenings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_id INTEGER NOT NULL REFERENCES watch_targets(id)
                        ON DELETE CASCADE,
                    screening_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    UNIQUE(target_id, screening_key)
                );

                CREATE TABLE IF NOT EXISTS screening_watches (
                    screening_id INTEGER PRIMARY KEY
                        REFERENCES console_screenings(id) ON DELETE CASCADE,
                    enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
                    seat_change_threshold INTEGER NOT NULL DEFAULT 1
                        CHECK(seat_change_threshold >= 1),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS seat_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    screening_id INTEGER NOT NULL
                        REFERENCES console_screenings(id) ON DELETE CASCADE,
                    revision INTEGER NOT NULL,
                    free_seats INTEGER NOT NULL,
                    total_seats INTEGER NOT NULL,
                    control_yn TEXT NOT NULL,
                    payload_json TEXT,
                    observed_at TEXT NOT NULL,
                    UNIQUE(screening_id, revision)
                );

                CREATE TABLE IF NOT EXISTS console_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_id INTEGER REFERENCES watch_targets(id) ON DELETE SET NULL,
                    screening_id INTEGER REFERENCES console_screenings(id)
                        ON DELETE SET NULL,
                    revision INTEGER,
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

                CREATE TABLE IF NOT EXISTS telegram_config (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    bot_token_ciphertext BLOB NOT NULL,
                    chat_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS console_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_watch_targets_due
                    ON watch_targets(enabled, next_poll_at, id);
                CREATE INDEX IF NOT EXISTS idx_console_screenings_target
                    ON console_screenings(target_id, id);
                CREATE INDEX IF NOT EXISTS idx_seat_history_screening
                    ON seat_history(screening_id, revision);
                CREATE INDEX IF NOT EXISTS idx_console_outbox_pending
                    ON console_outbox(
                        sent_at, dead_lettered_at, next_attempt_at, id
                    );
                """
            )
            target_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(watch_targets)"
                ).fetchall()
            }
            if "poll_jitter_seconds" not in target_columns:
                connection.execute(
                    "ALTER TABLE watch_targets "
                    "ADD COLUMN poll_jitter_seconds INTEGER NOT NULL DEFAULT 5 "
                    "CHECK(poll_jitter_seconds BETWEEN 0 AND 300)"
                )
            if "format_code" not in target_columns:
                connection.execute(
                    "ALTER TABLE watch_targets "
                    "ADD COLUMN format_code TEXT NOT NULL DEFAULT ''"
                )

            screening_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(console_screenings)"
                ).fetchall()
            }
            if "active" not in screening_columns:
                connection.execute(
                    "ALTER TABLE console_screenings "
                    "ADD COLUMN active INTEGER NOT NULL DEFAULT 1"
                )

            watch_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(screening_watches)"
                ).fetchall()
            }
            if "seat_change_threshold" not in watch_columns:
                connection.execute(
                    "ALTER TABLE screening_watches "
                    "ADD COLUMN seat_change_threshold INTEGER NOT NULL DEFAULT 1 "
                    "CHECK(seat_change_threshold >= 1)"
                )

            history_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(seat_history)"
                ).fetchall()
            }
            if "payload_json" not in history_columns:
                connection.execute(
                    "ALTER TABLE seat_history ADD COLUMN payload_json TEXT"
                )
                connection.execute(
                    """
                    UPDATE seat_history
                    SET payload_json = (
                        SELECT s.payload_json
                        FROM console_screenings AS s
                        WHERE s.id = seat_history.screening_id
                          AND s.revision = seat_history.revision
                    )
                    WHERE payload_json IS NULL
                    """
                )

            # Seat decreases remain in seat_history, but are no longer alerts.
            # Remove only undelivered legacy jobs so they cannot remain pending
            # indefinitely or be delivered by an older worker after deployment.
            connection.execute(
                """
                DELETE FROM console_outbox
                WHERE kind = 'seat_decreases'
                  AND sent_at IS NULL
                """
            )

    @staticmethod
    def _target_from_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for field in ("enabled", "notify_new", "auto_track_new", "initialized"):
            result[field] = bool(result[field])
        return result

    @staticmethod
    def _target_values(values: Mapping[str, Any]) -> dict[str, Any]:
        unknown = set(values) - ConsoleStore._TARGET_FIELDS
        if unknown:
            raise ValueError(
                f"unsupported target field(s): {', '.join(sorted(unknown))}"
            )
        result = {
            "company_code": str(values.get("company_code", "A420")).strip(),
            "site_no": str(values.get("site_no", "")).strip(),
            "site_name": str(
                values.get("site_name", values.get("site_no", ""))
            ).strip(),
            "movie_no": str(values.get("movie_no", "")).strip(),
            "movie_name": str(
                values.get("movie_name", values.get("movie_no", ""))
            ).strip(),
            "format_code": str(values.get("format_code", "")).strip(),
            "format_keyword": str(values.get("format_keyword", "IMAX")).strip(),
            "screen_grade_code": str(values.get("screen_grade_code", "")).strip(),
            "enabled": bool(values.get("enabled", True)),
            "notify_new": bool(values.get("notify_new", True)),
            "auto_track_new": bool(values.get("auto_track_new", False)),
            "poll_interval_seconds": int(values.get("poll_interval_seconds", 60)),
            "poll_jitter_seconds": normalize_poll_jitter_seconds(
                int(values.get("poll_jitter_seconds", 5))
            ),
            "next_poll_at": (
                _timestamp(values["next_poll_at"])
                if values.get("next_poll_at") is not None
                else None
            ),
        }
        if result["format_code"] and not result["screen_grade_code"]:
            # Older databases still have a UNIQUE constraint that ends with
            # screen_grade_code. Mirroring the exact format code here lets two
            # otherwise equally named formats coexist without rebuilding the
            # parent table and its foreign-key children.
            result["screen_grade_code"] = result["format_code"]
        required = ("company_code", "site_no", "site_name", "movie_no", "movie_name")
        if any(not result[field] for field in required):
            raise ValueError("target company, site, and movie fields are required")
        if (
            not result["format_code"]
            and not result["format_keyword"]
            and not result["screen_grade_code"]
        ):
            raise ValueError(
                "target format code, keyword, or screen grade code is required"
            )
        if result["poll_interval_seconds"] < 30:
            raise ValueError("poll_interval_seconds must be at least 30")
        return result

    @staticmethod
    def _insert_target(
        connection: sqlite3.Connection,
        values: Mapping[str, Any],
        *,
        ignore_conflict: bool = False,
    ) -> int | None:
        now = _now()
        action = "INSERT OR IGNORE" if ignore_conflict else "INSERT"
        cursor = connection.execute(
            f"""
            {action} INTO watch_targets(
                company_code, site_no, site_name, movie_no, movie_name,
                format_code, format_keyword, screen_grade_code, enabled, notify_new,
                auto_track_new, state, poll_interval_seconds,
                poll_jitter_seconds, next_poll_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                values["company_code"],
                values["site_no"],
                values["site_name"],
                values["movie_no"],
                values["movie_name"],
                values["format_code"],
                values["format_keyword"],
                values["screen_grade_code"],
                int(values["enabled"]),
                int(values["notify_new"]),
                int(values["auto_track_new"]),
                "idle" if values["enabled"] else "disabled",
                values["poll_interval_seconds"],
                values["poll_jitter_seconds"],
                values["next_poll_at"],
                now,
                now,
            ),
        )
        return int(cursor.lastrowid) if cursor.rowcount else None

    def ensure_default_target(self, **overrides: Any) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "company_code": "A420",
            "site_no": "0013",
            "site_name": "용산아이파크몰",
            "movie_no": "30001323",
            "movie_name": "오디세이",
            "format_code": "",
            "format_keyword": "IMAX",
            "screen_grade_code": "0301",
            "enabled": True,
            "notify_new": True,
            "auto_track_new": True,
            "poll_interval_seconds": 60,
            "poll_jitter_seconds": 5,
        }
        defaults.update(overrides)
        values = self._target_values(defaults)
        with self._connection(immediate=True) as connection:
            target_id = self._insert_target(connection, values, ignore_conflict=True)
            if target_id is None:
                row = connection.execute(
                    """
                    SELECT * FROM watch_targets
                    WHERE company_code = ? AND site_no = ? AND movie_no = ?
                      AND format_code = ? AND format_keyword = ?
                      AND screen_grade_code = ?
                    """,
                    (
                        values["company_code"],
                        values["site_no"],
                        values["movie_no"],
                        values["format_code"],
                        values["format_keyword"],
                        values["screen_grade_code"],
                    ),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM watch_targets WHERE id = ?",
                    (target_id,),
                ).fetchone()
        if row is None:  # pragma: no cover - guarded by the insert/select transaction
            raise RuntimeError("default target could not be created")
        return self._target_from_row(row)

    def create_target(
        self,
        values: Mapping[str, Any] | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        supplied = dict(values or {})
        supplied.update(fields)
        normalized = self._target_values(supplied)
        try:
            with self._connection(immediate=True) as connection:
                if normalized["format_code"]:
                    duplicate = connection.execute(
                        """
                        SELECT 1 FROM watch_targets
                        WHERE company_code = ? AND site_no = ? AND movie_no = ?
                          AND format_code = ?
                        LIMIT 1
                        """,
                        (
                            normalized["company_code"],
                            normalized["site_no"],
                            normalized["movie_no"],
                            normalized["format_code"],
                        ),
                    ).fetchone()
                    if duplicate is not None:
                        raise ValueError("an equivalent target already exists")
                target_id = self._insert_target(connection, normalized)
                row = connection.execute(
                    "SELECT * FROM watch_targets WHERE id = ?",
                    (target_id,),
                ).fetchone()
        except sqlite3.IntegrityError as exc:
            raise ValueError("an equivalent target already exists") from exc
        if row is None:  # pragma: no cover - lastrowid is selected in one transaction
            raise RuntimeError("target could not be created")
        return self._target_from_row(row)

    def list_targets(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM watch_targets ORDER BY id"
            ).fetchall()
        return [self._target_from_row(row) for row in rows]

    def get_target(self, target_id: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM watch_targets WHERE id = ?",
                (target_id,),
            ).fetchone()
        return self._target_from_row(row) if row is not None else None

    def update_target(
        self,
        target_id: int,
        changes: Mapping[str, Any] | None = None,
        *,
        expected_version: int | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        supplied = dict(changes or {})
        supplied.update(fields)
        unknown = set(supplied) - self._UPDATABLE_TARGET_FIELDS
        if unknown:
            raise ValueError(
                f"unsupported target update(s): {', '.join(sorted(unknown))}"
            )
        with self._connection(immediate=True) as connection:
            current = connection.execute(
                "SELECT * FROM watch_targets WHERE id = ?",
                (target_id,),
            ).fetchone()
            if current is None:
                raise KeyError(f"target {target_id} does not exist")
            current_version = int(current["version"])
            if expected_version is not None and expected_version != current_version:
                raise StaleVersionError("target version is stale")
            if not supplied:
                return self._target_from_row(current)

            normalized: dict[str, Any] = {}
            for field, value in supplied.items():
                if field in {"enabled", "notify_new", "auto_track_new"}:
                    normalized[field] = int(bool(value))
                elif field == "poll_interval_seconds":
                    interval = int(value)
                    if interval < 30:
                        raise ValueError("poll_interval_seconds must be at least 30")
                    normalized[field] = interval
                elif field == "poll_jitter_seconds":
                    normalized[field] = normalize_poll_jitter_seconds(int(value))
                else:
                    text = str(value).strip()
                    if not text:
                        raise ValueError(f"{field} cannot be empty")
                    normalized[field] = text

            changed_timing = bool(
                {"poll_interval_seconds", "poll_jitter_seconds"} & normalized.keys()
            )
            now = datetime.now(UTC)
            if "enabled" in normalized:
                normalized["state"] = "idle" if normalized["enabled"] else "disabled"
                if normalized["enabled"]:
                    normalized["next_poll_at"] = now.isoformat()
            elif changed_timing and bool(current["enabled"]):
                interval = int(
                    normalized.get(
                        "poll_interval_seconds",
                        current["poll_interval_seconds"],
                    )
                )
                jitter = int(
                    normalized.get(
                        "poll_jitter_seconds",
                        current["poll_jitter_seconds"],
                    )
                )
                delay = jittered_delay_seconds(interval, jitter)
                normalized["next_poll_at"] = (
                    now + timedelta(seconds=delay)
                ).isoformat()
            normalized["updated_at"] = now.isoformat()
            assignments = ", ".join(f"{name} = ?" for name in normalized)
            parameters = list(normalized.values())
            parameters.extend((target_id, current_version))
            cursor = connection.execute(
                f"""
                UPDATE watch_targets
                SET {assignments}, version = version + 1
                WHERE id = ? AND version = ?
                """,
                parameters,
            )
            if cursor.rowcount != 1:
                raise StaleVersionError("target version changed during update")
            row = connection.execute(
                "SELECT * FROM watch_targets WHERE id = ?",
                (target_id,),
            ).fetchone()
        return self._target_from_row(row)

    def request_refresh(
        self,
        target_id: int,
        *,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        requested_at = _now()
        with self._connection(immediate=True) as connection:
            row = connection.execute(
                "SELECT version FROM watch_targets WHERE id = ?",
                (target_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"target {target_id} does not exist")
            if expected_version is not None and int(row["version"]) != expected_version:
                raise StaleVersionError("target version is stale")
            connection.execute(
                """
                UPDATE watch_targets
                SET refresh_requested_at = ?, next_poll_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (requested_at, requested_at, requested_at, target_id),
            )
            updated = connection.execute(
                "SELECT * FROM watch_targets WHERE id = ?",
                (target_id,),
            ).fetchone()
        return self._target_from_row(updated)

    def due_targets(
        self,
        now: datetime | str | None = None,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if limit < 1:
            return []
        at = _timestamp(now)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM watch_targets
                WHERE enabled = 1
                  AND (next_poll_at IS NULL OR next_poll_at <= ?)
                ORDER BY
                    CASE WHEN refresh_requested_at IS NULL THEN 1 ELSE 0 END,
                    next_poll_at,
                    id
                LIMIT ?
                """,
                (at, limit),
            ).fetchall()
        return [self._target_from_row(row) for row in rows]

    def _check_target_version(
        self,
        connection: sqlite3.Connection,
        target_id: int,
        expected_version: int | None,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM watch_targets WHERE id = ?",
            (target_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"target {target_id} does not exist")
        if expected_version is not None and int(row["version"]) != expected_version:
            raise StaleVersionError("target version is stale")
        return row

    def mark_target_started(
        self,
        target_id: int,
        *,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        started_at = _now()
        with self._connection(immediate=True) as connection:
            self._check_target_version(connection, target_id, expected_version)
            connection.execute(
                """
                UPDATE watch_targets
                SET state = 'running', last_started_at = ?,
                    refresh_requested_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (started_at, started_at, target_id),
            )
            row = connection.execute(
                "SELECT * FROM watch_targets WHERE id = ?",
                (target_id,),
            ).fetchone()
        return self._target_from_row(row)

    def release_stale_target(self, target_id: int) -> None:
        """Return a stale in-flight poll to a schedulable state."""
        now = _now()
        with self._connection(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE watch_targets
                SET state = CASE WHEN enabled = 1 THEN 'idle' ELSE 'disabled' END,
                    next_poll_at = CASE WHEN enabled = 1 THEN ? ELSE next_poll_at END,
                    updated_at = ?
                WHERE id = ? AND state = 'running'
                """,
                (now, now, target_id),
            )
            if cursor.rowcount not in {0, 1}:  # pragma: no cover - primary key guard
                raise RuntimeError("unexpected stale target update count")

    def _default_next_poll(
        self,
        connection: sqlite3.Connection,
        target_id: int,
        *,
        after: datetime | None = None,
    ) -> str:
        row = connection.execute(
            """
            SELECT poll_interval_seconds, poll_jitter_seconds
            FROM watch_targets WHERE id = ?
            """,
            (target_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"target {target_id} does not exist")
        delay = jittered_delay_seconds(int(row[0]), int(row[1]))
        return ((after or datetime.now(UTC)) + timedelta(seconds=delay)).isoformat()

    def mark_target_success(
        self,
        target_id: int,
        next_poll_at: datetime | str | None = None,
        *,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        succeeded = datetime.now(UTC)
        succeeded_at = succeeded.isoformat()
        with self._connection(immediate=True) as connection:
            self._check_target_version(connection, target_id, expected_version)
            next_at = (
                _timestamp(next_poll_at)
                if next_poll_at is not None
                else self._default_next_poll(
                    connection,
                    target_id,
                    after=succeeded,
                )
            )
            connection.execute(
                """
                UPDATE watch_targets
                SET state = 'idle', last_success_at = ?, last_error = NULL,
                    consecutive_failures = 0, next_poll_at = ?,
                    refresh_requested_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (succeeded_at, next_at, succeeded_at, target_id),
            )
            row = connection.execute(
                "SELECT * FROM watch_targets WHERE id = ?",
                (target_id,),
            ).fetchone()
        return self._target_from_row(row)

    def mark_target_failure(
        self,
        target_id: int,
        error: str,
        next_poll_at: datetime | str | None = None,
        *,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        failed = datetime.now(UTC)
        failed_at = failed.isoformat()
        with self._connection(immediate=True) as connection:
            self._check_target_version(connection, target_id, expected_version)
            next_at = (
                _timestamp(next_poll_at)
                if next_poll_at is not None
                else self._default_next_poll(
                    connection,
                    target_id,
                    after=failed,
                )
            )
            connection.execute(
                """
                UPDATE watch_targets
                SET state = 'error', last_failure_at = ?, last_error = ?,
                    consecutive_failures = consecutive_failures + 1,
                    next_poll_at = ?, refresh_requested_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (failed_at, error[:500], next_at, failed_at, target_id),
            )
            row = connection.execute(
                "SELECT * FROM watch_targets WHERE id = ?",
                (target_id,),
            ).fetchone()
        return self._target_from_row(row)

    @staticmethod
    def _record_history(
        connection: sqlite3.Connection,
        screening_id: int,
        revision: int,
        screening: Screening,
        observed_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO seat_history(
                screening_id, revision, free_seats, total_seats,
                control_yn, payload_json, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                screening_id,
                revision,
                screening.free_seats,
                screening.total_seats,
                screening.control_yn,
                _canonical_json(screening.to_dict()),
                observed_at,
            ),
        )

    @staticmethod
    def _set_watch(
        connection: sqlite3.Connection,
        screening_id: int,
        enabled: bool,
        observed_at: str,
        seat_change_threshold: int | None = None,
    ) -> None:
        if seat_change_threshold is not None and not (
            1 <= seat_change_threshold <= MAX_SEAT_CHANGE_THRESHOLD
        ):
            raise ValueError("seat_change_threshold is outside the supported range")
        if seat_change_threshold is None:
            connection.execute(
                """
                INSERT INTO screening_watches(
                    screening_id, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(screening_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """,
                (screening_id, int(enabled), observed_at, observed_at),
            )
        else:
            connection.execute(
                """
                INSERT INTO screening_watches(
                    screening_id, enabled, seat_change_threshold,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(screening_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    seat_change_threshold = excluded.seat_change_threshold,
                    updated_at = excluded.updated_at
                """,
                (
                    screening_id,
                    int(enabled),
                    seat_change_threshold,
                    observed_at,
                    observed_at,
                ),
            )

    @staticmethod
    def _watch_threshold(
        connection: sqlite3.Connection,
        screening_id: int,
    ) -> int | None:
        row = connection.execute(
            """
            SELECT enabled, seat_change_threshold
            FROM screening_watches
            WHERE screening_id = ?
            """,
            (screening_id,),
        ).fetchone()
        if row is None or not bool(row["enabled"]):
            return None
        return int(row["seat_change_threshold"])

    @staticmethod
    def _enqueue(
        connection: sqlite3.Connection,
        *,
        target_id: int,
        screening_id: int,
        revision: int,
        kind: str,
        payload: Mapping[str, Any],
        created_at: str,
    ) -> bool:
        event_key = f"{target_id}:{screening_id}:{revision}:{kind}"
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO console_outbox(
                target_id, screening_id, revision, event_key,
                kind, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                target_id,
                screening_id,
                revision,
                event_key,
                kind,
                _canonical_json(payload),
                created_at,
            ),
        )
        return cursor.rowcount == 1

    def apply_snapshot(
        self,
        target_id: int,
        expected_version: int,
        screenings: Iterable[Screening],
    ) -> dict[str, Any]:
        current: dict[str, Screening] = {}
        for screening in screenings:
            if not isinstance(screening, Screening):
                raise TypeError("screenings must contain Screening instances")
            current[screening.key] = screening

        observed_at = _now()
        discovered = 0
        changed = 0
        new_events = 0
        booking_events = 0
        seat_increase_events = 0
        seat_decrease_events = 0
        auto_tracked = 0

        with self._connection(immediate=True) as connection:
            target_row = self._check_target_version(
                connection,
                target_id,
                expected_version,
            )
            if not bool(target_row["enabled"]):
                raise RuntimeError("cannot apply a snapshot to a disabled target")
            initialized = bool(target_row["initialized"])
            notify_new = bool(target_row["notify_new"])
            auto_track_new = bool(target_row["auto_track_new"])

            for screening_key in sorted(current):
                screening = current[screening_key]
                payload_json = _canonical_json(screening.to_dict())
                previous = connection.execute(
                    """
                    SELECT id, payload_json, revision, first_seen_at
                    FROM console_screenings
                    WHERE target_id = ? AND screening_key = ?
                    """,
                    (target_id, screening_key),
                ).fetchone()

                if previous is None:
                    discovered += 1
                    revision = 1
                    cursor = connection.execute(
                        """
                        INSERT INTO console_screenings(
                            target_id, screening_key, payload_json, revision, active,
                            first_seen_at, last_seen_at
                        ) VALUES (?, ?, ?, ?, 1, ?, ?)
                        """,
                        (
                            target_id,
                            screening_key,
                            payload_json,
                            revision,
                            observed_at,
                            observed_at,
                        ),
                    )
                    screening_id = int(cursor.lastrowid)
                    self._record_history(
                        connection,
                        screening_id,
                        revision,
                        screening,
                        observed_at,
                    )
                    if initialized and auto_track_new:
                        self._set_watch(connection, screening_id, True, observed_at)
                        auto_tracked += 1
                    if (
                        initialized
                        and notify_new
                        and screening.is_sale_open
                        and self._enqueue(
                            connection,
                            target_id=target_id,
                            screening_id=screening_id,
                            revision=revision,
                            kind="new_screenings",
                            payload={
                                "screenings": [screening.to_dict()],
                                "changes": [
                                    {
                                        "screening": screening.to_dict(),
                                        "previous_screening": None,
                                    }
                                ],
                            },
                            created_at=observed_at,
                        )
                    ):
                        new_events += 1
                    continue

                screening_id = int(previous["id"])
                if str(previous["payload_json"]) == payload_json:
                    connection.execute(
                        """
                        UPDATE console_screenings
                        SET active = 1, last_seen_at = ?
                        WHERE id = ?
                        """,
                        (observed_at, screening_id),
                    )
                    continue

                previous_payload = json.loads(str(previous["payload_json"]))
                comparable_previous = {
                    key: value
                    for key, value in previous_payload.items()
                    if key != "booking_url"
                }
                comparable_current = {
                    key: value
                    for key, value in screening.to_dict().items()
                    if key != "booking_url"
                }
                if comparable_previous == comparable_current:
                    revision = int(previous["revision"])
                    connection.execute(
                        """
                        UPDATE console_screenings
                        SET payload_json = ?, active = 1, last_seen_at = ?
                        WHERE id = ?
                        """,
                        (payload_json, observed_at, screening_id),
                    )
                    connection.execute(
                        """
                        UPDATE seat_history
                        SET payload_json = ?
                        WHERE screening_id = ? AND revision = ?
                        """,
                        (payload_json, screening_id, revision),
                    )
                    continue

                changed += 1
                old_screening = Screening.from_dict(previous_payload)
                revision = int(previous["revision"]) + 1
                connection.execute(
                    """
                    UPDATE console_screenings
                    SET payload_json = ?, revision = ?, active = 1, last_seen_at = ?
                    WHERE id = ?
                    """,
                    (payload_json, revision, observed_at, screening_id),
                )
                self._record_history(
                    connection,
                    screening_id,
                    revision,
                    screening,
                    observed_at,
                )

                if (
                    notify_new
                    and not old_screening.is_sale_open
                    and screening.is_sale_open
                    and self._enqueue(
                        connection,
                        target_id=target_id,
                        screening_id=screening_id,
                        revision=revision,
                        kind="booking_opened",
                        payload={
                            "screenings": [screening.to_dict()],
                            "changes": [
                                {
                                    "screening": screening.to_dict(),
                                    "previous_screening": old_screening.to_dict(),
                                }
                            ],
                        },
                        created_at=observed_at,
                    )
                ):
                    booking_events += 1
                seat_delta = screening.free_seats - old_screening.free_seats
                seat_threshold = self._watch_threshold(connection, screening_id)
                if (
                    seat_delta > 0
                    and old_screening.is_sale_open
                    and screening.is_sale_open
                    and seat_threshold is not None
                    and seat_delta >= seat_threshold
                    and self._enqueue(
                        connection,
                        target_id=target_id,
                        screening_id=screening_id,
                        revision=revision,
                        kind="seat_increases",
                        payload={
                            "seat_change_threshold": seat_threshold,
                            "seat_delta": seat_delta,
                            "changes": [
                                {
                                    "screening": screening.to_dict(),
                                    "previous_screening": old_screening.to_dict(),
                                    "previous_free_seats": old_screening.free_seats,
                                }
                            ],
                        },
                        created_at=observed_at,
                    )
                ):
                    seat_increase_events += 1

            current_keys = sorted(current)
            if current_keys:
                placeholders = ",".join("?" for _ in current_keys)
                connection.execute(
                    f"""
                    UPDATE console_screenings
                    SET active = 0
                    WHERE target_id = ? AND screening_key NOT IN ({placeholders})
                    """,
                    (target_id, *current_keys),
                )
            else:
                connection.execute(
                    "UPDATE console_screenings SET active = 0 WHERE target_id = ?",
                    (target_id,),
                )

            connection.execute(
                """
                UPDATE watch_targets
                SET initialized = 1, updated_at = ?
                WHERE id = ?
                """,
                (observed_at, target_id),
            )

        return {
            "initialized_before_poll": initialized,
            "screening_count": len(current),
            "discovered_screening_count": discovered,
            "changed_screening_count": changed,
            "new_screening_count": new_events,
            "booking_opened_count": booking_events,
            "seat_increase_count": seat_increase_events,
            "seat_decrease_count": seat_decrease_events,
            "auto_tracked_count": auto_tracked,
        }

    def list_screenings(
        self,
        target_id: int,
        *,
        include_history: bool = False,
    ) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT s.*, COALESCE(w.enabled, 0) AS watched,
                       COALESCE(w.seat_change_threshold, 1)
                           AS seat_change_threshold
                FROM console_screenings AS s
                LEFT JOIN screening_watches AS w ON w.screening_id = s.id
                WHERE s.target_id = ? AND s.active = 1
                ORDER BY s.id
                """,
                (target_id,),
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                payload = json.loads(str(row["payload_json"]))
                item: dict[str, Any] = {
                    "id": int(row["id"]),
                    "target_id": int(row["target_id"]),
                    "screening_key": str(row["screening_key"]),
                    "revision": int(row["revision"]),
                    "first_seen_at": str(row["first_seen_at"]),
                    "last_seen_at": str(row["last_seen_at"]),
                    "active": bool(row["active"]),
                    "watched": bool(row["watched"]),
                    "seat_change_threshold": int(row["seat_change_threshold"]),
                    "payload": payload,
                }
                item.update(payload)
                if include_history:
                    history_rows = connection.execute(
                        """
                        SELECT revision, free_seats, total_seats,
                               control_yn, payload_json, observed_at
                        FROM seat_history
                        WHERE screening_id = ?
                        ORDER BY revision
                        """,
                        (row["id"],),
                    ).fetchall()
                    item["history"] = []
                    for history in history_rows:
                        history_item = dict(history)
                        raw_payload = history_item.pop("payload_json")
                        history_item["payload"] = (
                            json.loads(str(raw_payload))
                            if raw_payload is not None
                            else None
                        )
                        item["history"].append(history_item)
                result.append(item)
        result.sort(
            key=lambda item: (
                str(item.get("screening_date", "")),
                str(item.get("start_time", "")),
                int(item["id"]),
            )
        )
        return result

    @staticmethod
    def _activity_payload(
        row: sqlite3.Row,
        prefix: str,
        latest_payload: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        raw_payload = row[f"{prefix}_payload_json"]
        if raw_payload is not None:
            return json.loads(str(raw_payload)), True
        payload = {
            field: latest_payload[field]
            for field in (
                "company_code",
                "site_no",
                "site_name",
                "movie_no",
                "movie_name",
                "screening_date",
                "screen_no",
                "screen_name",
                "sequence",
                "start_time",
                "end_time",
                "format_name",
                "screen_grade_code",
                "booking_url",
            )
            if field in latest_payload
        }
        payload.update(
            {
                "free_seats": int(row[f"{prefix}_free_seats"]),
                "total_seats": int(row[f"{prefix}_total_seats"]),
                "control_yn": str(row[f"{prefix}_control_yn"]),
            }
        )
        return payload, False

    @staticmethod
    def _activity_kind(
        current: Mapping[str, Any],
        previous: Mapping[str, Any] | None,
    ) -> str:
        if previous is None:
            return "new_screenings"
        previous_control = str(previous.get("control_yn") or "")
        current_control = str(current.get("control_yn") or "")
        if previous_control == "Y" and current_control != "Y":
            return "booking_opened"
        if previous_control != "Y" and current_control == "Y":
            return "booking_closed"
        previous_free = int(previous.get("free_seats") or 0)
        current_free = int(current.get("free_seats") or 0)
        if current_free > previous_free:
            return "seat_increases"
        if current_free < previous_free:
            return "seat_decreases"
        if int(current.get("total_seats") or 0) != int(
            previous.get("total_seats") or 0
        ):
            return "total_seats_changed"
        return "screening_updated"

    @staticmethod
    def _activity_changes(
        current: Mapping[str, Any],
        previous: Mapping[str, Any] | None,
        *,
        details_complete: bool,
    ) -> list[dict[str, Any]]:
        if previous is None:
            return [{"field": "exists", "before": False, "after": True}]
        if details_complete:
            fields = set(current) | set(previous)
        else:
            fields = set(current) & set(previous)
        priority = {
            "control_yn": 0,
            "free_seats": 1,
            "total_seats": 2,
            "screening_date": 3,
            "start_time": 4,
            "end_time": 5,
            "screen_no": 6,
            "screen_name": 7,
            "sequence": 8,
            "format_name": 9,
            "booking_url": 10,
        }
        changes: list[dict[str, Any]] = []
        for field in sorted(fields, key=lambda item: (priority.get(item, 100), item)):
            before = previous.get(field)
            after = current.get(field)
            if before == after:
                continue
            change: dict[str, Any] = {
                "field": field,
                "before": before,
                "after": after,
            }
            if (
                field in {"free_seats", "total_seats"}
                and isinstance(before, int)
                and isinstance(after, int)
            ):
                change["delta"] = after - before
            changes.append(change)
        return changes

    @classmethod
    def _activity_from_row(cls, row: sqlite3.Row) -> dict[str, Any]:
        latest_payload = json.loads(str(row["latest_payload_json"]))
        current, current_complete = cls._activity_payload(
            row,
            "current",
            latest_payload,
        )
        if row["previous_revision"] is None:
            previous = None
            previous_complete = True
        else:
            previous, previous_complete = cls._activity_payload(
                row,
                "previous",
                latest_payload,
            )
        details_complete = current_complete and previous_complete
        kind = cls._activity_kind(current, previous)
        changes = cls._activity_changes(
            current,
            previous,
            details_complete=details_complete,
        )

        notification: dict[str, Any] | None = None
        if row["notification_id"] is not None:
            notification_payload = json.loads(str(row["notification_payload_json"]))
            if row["notification_sent_at"] is not None:
                notification_status = "sent"
            elif row["notification_dead_lettered_at"] is not None:
                notification_status = "dead"
            else:
                notification_status = "pending"
            notification = {
                "id": int(row["notification_id"]),
                "kind": str(row["notification_kind"]),
                "status": notification_status,
                "created_at": str(row["notification_created_at"]),
                "sent_at": row["notification_sent_at"],
                "attempts": int(row["notification_attempts"]),
                "delivered_parts": int(row["notification_delivered_parts"]),
                "next_attempt_at": row["notification_next_attempt_at"],
                "dead_lettered_at": row["notification_dead_lettered_at"],
                "last_error": row["notification_last_error"],
                "seat_change_threshold": notification_payload.get(
                    "seat_change_threshold"
                ),
                "seat_delta": notification_payload.get("seat_delta"),
            }

        if kind in {"seat_increases", "seat_decreases"}:
            payload = {
                "changes": [
                    {
                        "screening": current,
                        "previous_screening": previous,
                        "previous_free_seats": (
                            previous.get("free_seats") if previous else None
                        ),
                    }
                ]
            }
        else:
            payload = {"screenings": [current]}

        return {
            "id": int(row["history_id"]),
            "target_id": int(row["target_id"]),
            "screening_id": int(row["screening_id"]),
            "screening_key": str(row["screening_key"]),
            "revision": int(row["current_revision"]),
            "kind": kind,
            "observed_at": str(row["observed_at"]),
            "created_at": str(row["observed_at"]),
            "screening": current,
            "previous_screening": previous,
            "changes": changes,
            "details_complete": details_complete,
            "booking_url": str(current.get("booking_url") or ""),
            "notification": notification,
            "payload": payload,
            "sent_at": notification["sent_at"] if notification else None,
            "dead_lettered_at": (
                notification["dead_lettered_at"] if notification else None
            ),
            "last_error": notification["last_error"] if notification else None,
        }

    def list_activity_page(
        self,
        *,
        limit: int = 50,
        cursor: int | None = None,
        target_id: int | None = None,
        screening_id: int | None = None,
        kind: str | None = None,
        notifications_only: bool = False,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if cursor is not None and cursor < 1:
            raise ValueError("cursor must be positive")
        if target_id is not None and target_id < 1:
            raise ValueError("target_id must be positive")
        if screening_id is not None and screening_id < 1:
            raise ValueError("screening_id must be positive")
        if kind is not None and kind not in self.ACTIVITY_KINDS:
            raise ValueError("unsupported activity kind")

        items: list[dict[str, Any]] = []
        scan_cursor = cursor
        chunk_size = max(100, (limit + 1) * 2)
        with self._connection() as connection:
            while len(items) <= limit:
                clauses: list[str] = []
                parameters: list[Any] = []
                if scan_cursor is not None:
                    clauses.append("h.id < ?")
                    parameters.append(scan_cursor)
                if target_id is not None:
                    clauses.append("s.target_id = ?")
                    parameters.append(target_id)
                if screening_id is not None:
                    clauses.append("h.screening_id = ?")
                    parameters.append(screening_id)
                if notifications_only:
                    clauses.append(
                        """
                        EXISTS (
                            SELECT 1
                            FROM console_outbox AS alert
                            WHERE alert.screening_id = h.screening_id
                              AND alert.revision = h.revision
                              AND alert.kind != 'seat_decreases'
                        )
                        """
                    )
                where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
                parameters.append(chunk_size)
                rows = connection.execute(
                    f"""
                    SELECT
                        h.id AS history_id,
                        h.screening_id,
                        h.revision AS current_revision,
                        h.free_seats AS current_free_seats,
                        h.total_seats AS current_total_seats,
                        h.control_yn AS current_control_yn,
                        h.payload_json AS current_payload_json,
                        h.observed_at,
                        p.revision AS previous_revision,
                        p.free_seats AS previous_free_seats,
                        p.total_seats AS previous_total_seats,
                        p.control_yn AS previous_control_yn,
                        p.payload_json AS previous_payload_json,
                        s.target_id,
                        s.screening_key,
                        s.payload_json AS latest_payload_json,
                        o.id AS notification_id,
                        o.kind AS notification_kind,
                        o.payload_json AS notification_payload_json,
                        o.created_at AS notification_created_at,
                        o.sent_at AS notification_sent_at,
                        o.attempts AS notification_attempts,
                        o.delivered_parts AS notification_delivered_parts,
                        o.next_attempt_at AS notification_next_attempt_at,
                        o.dead_lettered_at AS notification_dead_lettered_at,
                        o.last_error AS notification_last_error
                    FROM seat_history AS h
                    JOIN console_screenings AS s ON s.id = h.screening_id
                    LEFT JOIN seat_history AS p
                      ON p.screening_id = h.screening_id
                     AND p.revision = h.revision - 1
                    LEFT JOIN console_outbox AS o ON o.id = (
                        SELECT MAX(candidate.id)
                        FROM console_outbox AS candidate
                        WHERE candidate.screening_id = h.screening_id
                          AND candidate.revision = h.revision
                    )
                    {where}
                    ORDER BY h.id DESC
                    LIMIT ?
                    """,
                    parameters,
                ).fetchall()
                if not rows:
                    break
                for row in rows:
                    scan_cursor = int(row["history_id"])
                    item = self._activity_from_row(row)
                    if kind is None or item["kind"] == kind:
                        items.append(item)
                        if len(items) > limit:
                            break
                if len(items) > limit or len(rows) < chunk_size:
                    break

        has_more = len(items) > limit
        page_items = items[:limit]
        return {
            "items": page_items,
            "next_cursor": (
                int(page_items[-1]["id"]) if has_more and page_items else None
            ),
            "has_more": has_more,
        }

    def set_watch(
        self,
        screening_id: int,
        enabled: bool,
        seat_change_threshold: int | None = None,
    ) -> dict[str, Any]:
        if seat_change_threshold is not None and not (
            1 <= seat_change_threshold <= MAX_SEAT_CHANGE_THRESHOLD
        ):
            raise ValueError("seat_change_threshold is outside the supported range")
        observed_at = _now()
        with self._connection(immediate=True) as connection:
            row = connection.execute(
                "SELECT target_id FROM console_screenings WHERE id = ?",
                (screening_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"screening {screening_id} does not exist")
            self._set_watch(
                connection,
                screening_id,
                enabled,
                observed_at,
                seat_change_threshold,
            )
            watch_row = connection.execute(
                """
                SELECT seat_change_threshold
                FROM screening_watches
                WHERE screening_id = ?
                """,
                (screening_id,),
            ).fetchone()
        return {
            "screening_id": screening_id,
            "target_id": int(row["target_id"]),
            "enabled": bool(enabled),
            "seat_change_threshold": int(watch_row["seat_change_threshold"]),
        }

    def set_watched_thresholds(
        self,
        target_id: int,
        seat_change_threshold: int,
    ) -> dict[str, Any]:
        if not 1 <= seat_change_threshold <= MAX_SEAT_CHANGE_THRESHOLD:
            raise ValueError("seat_change_threshold is outside the supported range")
        observed_at = _now()
        with self._connection(immediate=True) as connection:
            target = connection.execute(
                "SELECT id FROM watch_targets WHERE id = ?",
                (target_id,),
            ).fetchone()
            if target is None:
                raise KeyError(f"target {target_id} does not exist")
            cursor = connection.execute(
                """
                UPDATE screening_watches
                SET seat_change_threshold = ?, updated_at = ?
                WHERE enabled = 1
                  AND screening_id IN (
                      SELECT id
                      FROM console_screenings
                      WHERE target_id = ?
                  )
                """,
                (seat_change_threshold, observed_at, target_id),
            )
        return {
            "target_id": target_id,
            "seat_change_threshold": seat_change_threshold,
            "updated_count": cursor.rowcount,
        }

    def pending_events(
        self,
        limit: int = 50,
        *,
        now: datetime | str | None = None,
    ) -> list[OutboxEvent]:
        if limit < 1:
            return []
        at = _timestamp(now)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, event_key, kind, payload_json,
                       attempts, delivered_parts
                FROM console_outbox
                WHERE sent_at IS NULL
                  AND dead_lettered_at IS NULL
                  AND kind != 'seat_decreases'
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY id
                LIMIT ?
                """,
                (at, limit),
            ).fetchall()
        return [OutboxEvent.from_row(row) for row in rows]

    @staticmethod
    def _require_changed(cursor: sqlite3.Cursor, event_id: int) -> None:
        if cursor.rowcount != 1:
            raise KeyError(f"pending outbox event {event_id} does not exist")

    def mark_part_delivered(self, event_id: int, delivered_parts: int) -> None:
        if delivered_parts < 0:
            raise ValueError("delivered_parts cannot be negative")
        with self._connection(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE console_outbox
                SET delivered_parts = MAX(delivered_parts, ?),
                    last_error = NULL, next_attempt_at = NULL
                WHERE id = ? AND sent_at IS NULL AND dead_lettered_at IS NULL
                """,
                (delivered_parts, event_id),
            )
            self._require_changed(cursor, event_id)

    def mark_sent(self, event_id: int) -> None:
        with self._connection(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE console_outbox
                SET sent_at = ?, last_error = NULL, next_attempt_at = NULL
                WHERE id = ? AND sent_at IS NULL AND dead_lettered_at IS NULL
                """,
                (_now(), event_id),
            )
            self._require_changed(cursor, event_id)

    def mark_failed(
        self,
        event_id: int,
        error: str,
        retry_after_seconds: int,
    ) -> None:
        if retry_after_seconds < 0:
            raise ValueError("retry_after_seconds cannot be negative")
        next_attempt = (
            datetime.now(UTC) + timedelta(seconds=retry_after_seconds)
        ).isoformat()
        with self._connection(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE console_outbox
                SET attempts = attempts + 1, last_error = ?, next_attempt_at = ?
                WHERE id = ? AND sent_at IS NULL AND dead_lettered_at IS NULL
                """,
                (error[:500], next_attempt, event_id),
            )
            self._require_changed(cursor, event_id)

    def mark_dead(self, event_id: int, error: str) -> None:
        with self._connection(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE console_outbox
                SET attempts = attempts + 1, last_error = ?,
                    dead_lettered_at = ?, next_attempt_at = NULL
                WHERE id = ? AND sent_at IS NULL AND dead_lettered_at IS NULL
                """,
                (error[:500], _now(), event_id),
            )
            self._require_changed(cursor, event_id)

    def requeue_dead(self, event_id: int | None = None) -> int:
        with self._connection(immediate=True) as connection:
            if event_id is None:
                cursor = connection.execute(
                    """
                    UPDATE console_outbox
                    SET dead_lettered_at = NULL, next_attempt_at = NULL,
                        attempts = 0, last_error = NULL
                    WHERE sent_at IS NULL AND dead_lettered_at IS NOT NULL
                    """
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE console_outbox
                    SET dead_lettered_at = NULL, next_attempt_at = NULL,
                        attempts = 0, last_error = NULL
                    WHERE id = ? AND sent_at IS NULL
                      AND dead_lettered_at IS NOT NULL
                    """,
                    (event_id,),
                )
            return int(cursor.rowcount)

    def list_outbox(
        self,
        *,
        status: str | None = None,
        target_id: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if status not in {None, "pending", "sent", "dead"}:
            raise ValueError("status must be pending, sent, or dead")
        clauses: list[str] = []
        parameters: list[Any] = []
        if status == "pending":
            clauses.append("sent_at IS NULL AND dead_lettered_at IS NULL")
        elif status == "sent":
            clauses.append("sent_at IS NOT NULL")
        elif status == "dead":
            clauses.append("dead_lettered_at IS NOT NULL")
        if target_id is not None:
            clauses.append("target_id = ?")
            parameters.append(target_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(max(0, limit))
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM console_outbox
                {where}
                ORDER BY id DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(str(item.pop("payload_json")))
            if item["sent_at"] is not None:
                item["status"] = "sent"
            elif item["dead_lettered_at"] is not None:
                item["status"] = "dead"
            else:
                item["status"] = "pending"
            result.append(item)
        return result

    def save_telegram_config(
        self,
        *,
        bot_token: str | None = None,
        chat_id: str,
        enabled: bool = True,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        normalized_chat = str(chat_id).strip()
        if not normalized_chat:
            raise ValueError("chat_id is required")
        now = _now()
        with self._connection(immediate=True) as connection:
            current = connection.execute(
                "SELECT * FROM telegram_config WHERE id = 1"
            ).fetchone()
            if (
                current is not None
                and expected_version is not None
                and int(current["version"]) != expected_version
            ):
                raise StaleVersionError("Telegram config version is stale")
            if current is None and expected_version not in {None, 0}:
                raise StaleVersionError("Telegram config does not exist yet")

            if bot_token is None:
                if current is None:
                    raise ValueError(
                        "bot_token is required for the first configuration"
                    )
                ciphertext = bytes(current["bot_token_ciphertext"])
            else:
                normalized_token = bot_token.strip()
                if not normalized_token:
                    raise ValueError("bot_token cannot be empty")
                ciphertext = self._fernet.encrypt(normalized_token.encode("utf-8"))

            if current is None:
                connection.execute(
                    """
                    INSERT INTO telegram_config(
                        id, bot_token_ciphertext, chat_id, enabled,
                        version, created_at, updated_at
                    ) VALUES (1, ?, ?, ?, 1, ?, ?)
                    """,
                    (ciphertext, normalized_chat, int(enabled), now, now),
                )
            else:
                connection.execute(
                    """
                    UPDATE telegram_config
                    SET bot_token_ciphertext = ?, chat_id = ?, enabled = ?,
                        version = version + 1, updated_at = ?
                    WHERE id = 1
                    """,
                    (ciphertext, normalized_chat, int(enabled), now),
                )
        config = self.get_telegram_config()
        if config is None:  # pragma: no cover - insert/update and read use the same DB
            raise RuntimeError("Telegram config could not be saved")
        return config

    def get_telegram_config(
        self,
        *,
        include_token: bool = False,
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM telegram_config WHERE id = 1"
            ).fetchone()
        if row is None:
            return None
        result: dict[str, Any] = {
            "chat_id": str(row["chat_id"]),
            "enabled": bool(row["enabled"]),
            "version": int(row["version"]),
            "token_configured": True,
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }
        if include_token:
            try:
                result["bot_token"] = self._fernet.decrypt(
                    bytes(row["bot_token_ciphertext"])
                ).decode("utf-8")
            except (InvalidToken, UnicodeDecodeError) as exc:
                raise RuntimeError("Telegram token cannot be decrypted") from exc
        return result

    def set_metadata(self, key: str, value: str) -> None:
        normalized_key = key.strip()
        if not normalized_key:
            raise ValueError("metadata key cannot be empty")
        with self._connection(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO console_metadata(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (normalized_key, str(value)),
            )

    def get_metadata(self, key: str) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT value FROM console_metadata WHERE key = ?",
                (key,),
            ).fetchone()
        return str(row["value"]) if row is not None else None

    def health_check(self) -> dict[str, Any]:
        with self._connection(immediate=True) as connection:
            quick_check = connection.execute("PRAGMA quick_check").fetchone()
            if quick_check is None or quick_check[0] != "ok":
                raise RuntimeError("SQLite quick_check failed")
            foreign_keys = int(connection.execute("PRAGMA foreign_keys").fetchone()[0])
            journal_mode = str(
                connection.execute("PRAGMA journal_mode").fetchone()[0]
            ).lower()
            busy_timeout = int(connection.execute("PRAGMA busy_timeout").fetchone()[0])
            if foreign_keys != 1:
                raise RuntimeError("SQLite foreign keys are disabled")
            if journal_mode != "wal":
                raise RuntimeError("SQLite WAL mode is disabled")
            if busy_timeout < 5000:
                raise RuntimeError("SQLite busy timeout is too short")
            token_row = connection.execute(
                "SELECT bot_token_ciphertext FROM telegram_config WHERE id = 1"
            ).fetchone()
            if token_row is not None:
                try:
                    self._fernet.decrypt(bytes(token_row["bot_token_ciphertext"]))
                except InvalidToken as exc:
                    raise RuntimeError("Telegram token cannot be decrypted") from exc
            connection.execute("SELECT 1")
        return {
            "quick_check": "ok",
            "foreign_keys": True,
            "journal_mode": journal_mode,
            "busy_timeout": busy_timeout,
        }
