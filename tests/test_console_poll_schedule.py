from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from moviemax.config import ConfigError, Settings
from moviemax.console_config import ConsoleSettings
from moviemax.console_store import ConsoleStore
from moviemax.console_web import create_app
from moviemax.console_worker import ConsoleWorker


def _console_settings(tmp_path) -> ConsoleSettings:
    return ConsoleSettings(
        database_path=tmp_path / "console.sqlite3",
        encryption_key=Fernet.generate_key().decode("ascii"),
        public_origin="https://max.wondering.kr",
        allowed_hosts=("testserver",),
        worker_tick_seconds=1,
        seed_default_target=False,
    )


def _create_target(store: ConsoleStore, **overrides) -> dict:
    values = {
        "site_no": "0013",
        "site_name": "Yongsan",
        "movie_no": "movie-1",
        "movie_name": "Odyssey",
        "format_keyword": "IMAX",
        "screen_grade_code": "0301",
    }
    values.update(overrides)
    return store.create_target(values)


def test_existing_database_migrates_poll_jitter_with_default(tmp_path) -> None:
    database = tmp_path / "legacy.sqlite3"
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE watch_targets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_code TEXT NOT NULL,
                site_no TEXT NOT NULL,
                site_name TEXT NOT NULL,
                movie_no TEXT NOT NULL,
                movie_name TEXT NOT NULL,
                format_keyword TEXT NOT NULL,
                screen_grade_code TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
                notify_new INTEGER NOT NULL DEFAULT 1 CHECK(notify_new IN (0, 1)),
                auto_track_new INTEGER NOT NULL DEFAULT 0
                    CHECK(auto_track_new IN (0, 1)),
                initialized INTEGER NOT NULL DEFAULT 0 CHECK(initialized IN (0, 1)),
                state TEXT NOT NULL DEFAULT 'idle',
                poll_interval_seconds INTEGER NOT NULL DEFAULT 60
                    CHECK(poll_interval_seconds >= 30),
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
                    format_keyword, screen_grade_code
                )
            );
            CREATE TABLE console_screenings (
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
            CREATE TABLE telegram_config (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                bot_token_ciphertext BLOB NOT NULL,
                chat_id TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
                version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            """
            INSERT INTO watch_targets(
                company_code, site_no, site_name, movie_no, movie_name,
                format_keyword, screen_grade_code, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "A420",
                "0013",
                "Yongsan",
                "movie-1",
                "Odyssey",
                "IMAX",
                "0301",
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO console_screenings(
                target_id, screening_key, payload_json, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (1, "legacy-screening", "{}", now, now),
        )
        connection.execute(
            """
            INSERT INTO telegram_config(
                id, bot_token_ciphertext, chat_id, enabled,
                version, created_at, updated_at
            ) VALUES (1, ?, ?, 1, 1, ?, ?)
            """,
            (b"legacy-token", "599123456", now, now),
        )

    store = ConsoleStore(database, Fernet.generate_key().decode("ascii"))

    assert store.list_targets()[0]["poll_jitter_seconds"] == 5
    assert store.list_targets()[0]["format_code"] == ""
    assert store.list_targets()[0]["telegram_enabled"] is True
    assert store.list_targets()[0]["telegram_chat_id"] == "599123456"
    assert store.list_targets()[0]["telegram_notify_new"] is True
    assert store.list_targets()[0]["telegram_notify_seat_increase"] is True
    assert store.list_telegram_chat_candidates() == [
        {
            "id": "599123456",
            "type": "legacy",
            "title": "기존 Telegram 수신자",
        }
    ]
    existing = store.ensure_default_target(
        site_no="0013",
        site_name="Yongsan",
        movie_no="movie-1",
        movie_name="Odyssey",
        format_keyword="IMAX",
        screen_grade_code="0301",
    )
    assert existing["id"] == 1
    assert len(store.list_targets()) == 1
    updated = store.update_target(
        existing["id"],
        expected_version=existing["version"],
        poll_interval_seconds=5,
    )
    assert updated["poll_interval_seconds"] == 5
    with sqlite3.connect(database) as connection:
        columns = {
            row[1]: row
            for row in connection.execute("PRAGMA table_info(watch_targets)")
        }
        target_schema = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name = 'watch_targets'"
        ).fetchone()[0]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        migrated_screening = connection.execute(
            "SELECT target_id, screening_key FROM console_screenings"
        ).fetchone()
    assert columns["poll_jitter_seconds"][4] == "5"
    assert columns["format_code"][4] == "''"
    assert columns["telegram_enabled"][4] == "0"
    assert columns["telegram_chat_id"][4] == "''"
    assert "CHECK(poll_interval_seconds >= 5)" in target_schema
    assert "CHECK(poll_interval_seconds >= 30)" not in target_schema
    assert migrated_screening == (1, "legacy-screening")

    first_format = store.create_target(
        site_no="0013",
        site_name="Yongsan",
        movie_no="movie-2",
        movie_name="Second movie",
        format_code="08",
        format_keyword="same display name",
        screen_grade_code="",
    )
    second_format = store.create_target(
        site_no="0013",
        site_name="Yongsan",
        movie_no="movie-2",
        movie_name="Second movie",
        format_code="44",
        format_keyword="same display name",
        screen_grade_code="",
    )
    assert first_format["screen_grade_code"] == "08"
    assert second_format["screen_grade_code"] == "44"


def test_success_and_timing_update_use_interval_plus_bounded_jitter(
    tmp_path,
    monkeypatch,
) -> None:
    settings = _console_settings(tmp_path)
    store = ConsoleStore(settings.database_path, settings.encryption_key)
    target = _create_target(
        store,
        poll_interval_seconds=60,
        poll_jitter_seconds=5,
    )
    monkeypatch.setattr(
        "moviemax.polling.random.uniform",
        lambda lower, upper: upper,
    )

    succeeded = store.mark_target_success(target["id"])
    assert datetime.fromisoformat(succeeded["next_poll_at"]) - datetime.fromisoformat(
        succeeded["last_success_at"]
    ) == timedelta(seconds=65)

    updated = store.update_target(
        target["id"],
        expected_version=target["version"],
        poll_interval_seconds=90,
        poll_jitter_seconds=7,
    )
    assert datetime.fromisoformat(updated["next_poll_at"]) - datetime.fromisoformat(
        updated["updated_at"]
    ) == timedelta(seconds=97)


def test_worker_failure_backoff_adds_target_jitter(tmp_path, monkeypatch) -> None:
    settings = _console_settings(tmp_path)
    base_settings = Settings(
        request_gap_seconds=0,
        poll_interval_seconds=60,
        poll_jitter_seconds=5,
        backoff_max_seconds=900,
    )
    store = ConsoleStore(settings.database_path, settings.encryption_key)
    worker = ConsoleWorker(settings, base_settings=base_settings, store=store)
    target = _create_target(
        store,
        poll_interval_seconds=60,
        poll_jitter_seconds=5,
    )

    def fail_fetch(_target: dict) -> list:
        raise RuntimeError("CGV unavailable")

    monkeypatch.setattr(worker, "fetch_target", fail_fetch)
    monkeypatch.setattr(
        "moviemax.polling.random.uniform",
        lambda lower, upper: upper,
    )

    first_before = datetime.now(UTC)
    assert worker.process_target(target) is None
    first_after = datetime.now(UTC)
    first = store.get_target(target["id"])
    first_next = datetime.fromisoformat(first["next_poll_at"])
    assert first_before + timedelta(seconds=65) <= first_next
    assert first_next <= first_after + timedelta(seconds=65)

    second_before = datetime.now(UTC)
    assert worker.process_target(first) is None
    second_after = datetime.now(UTC)
    second = store.get_target(target["id"])
    second_next = datetime.fromisoformat(second["next_poll_at"])
    assert second_before + timedelta(seconds=125) <= second_next
    assert second_next <= second_after + timedelta(seconds=125)


def test_poll_jitter_validation_and_target_api(tmp_path) -> None:
    Settings(poll_interval_seconds=5).validate()
    with pytest.raises(ConfigError, match="at least 5"):
        Settings(poll_interval_seconds=4).validate()
    with pytest.raises(ConfigError, match="between 0 and 300"):
        Settings(poll_jitter_seconds=301).validate()

    settings = _console_settings(tmp_path)
    store = ConsoleStore(settings.database_path, settings.encryption_key)
    app = create_app(settings, store=store, catalog_client=object())
    headers = {
        "X-MovieMax-CSRF": "1",
        "Origin": settings.public_origin,
    }
    payload = {
        "site_no": "0013",
        "site_name": "Yongsan",
        "movie_no": "movie-1",
        "movie_name": "Odyssey",
        "format_code": "48",
        "format_name": "IMAX LASER 2D",
        "poll_interval_seconds": 5,
        "poll_jitter_seconds": 17,
    }

    with TestClient(app) as client:
        created_response = client.post(
            "/api/v1/targets",
            json=payload,
            headers=headers,
        )
        invalid_response = client.post(
            "/api/v1/targets",
            json={**payload, "movie_no": "movie-2", "poll_jitter_seconds": 301},
            headers=headers,
        )
        too_fast_response = client.post(
            "/api/v1/targets",
            json={**payload, "movie_no": "movie-3", "poll_interval_seconds": 4},
            headers=headers,
        )

    assert created_response.status_code == 201
    created = created_response.json()["target"]
    assert created["poll_interval_seconds"] == 5
    assert created["poll_jitter_seconds"] == 17
    assert invalid_response.status_code == 422
    assert too_fast_response.status_code == 422
