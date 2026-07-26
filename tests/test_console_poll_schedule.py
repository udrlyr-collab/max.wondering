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

    store = ConsoleStore(database, Fernet.generate_key().decode("ascii"))

    assert store.list_targets()[0]["poll_jitter_seconds"] == 5
    with sqlite3.connect(database) as connection:
        columns = {
            row[1]: row
            for row in connection.execute("PRAGMA table_info(watch_targets)")
        }
    assert columns["poll_jitter_seconds"][4] == "5"


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
        "poll_interval_seconds": 75,
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

    assert created_response.status_code == 201
    created = created_response.json()["target"]
    assert created["poll_interval_seconds"] == 75
    assert created["poll_jitter_seconds"] == 17
    assert invalid_response.status_code == 422
