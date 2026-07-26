from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet

from moviemax.console_store import ConsoleStore, StaleVersionError
from moviemax.models import OutboxEvent, Screening


@pytest.fixture
def encryption_key() -> str:
    return Fernet.generate_key().decode("ascii")


@pytest.fixture
def store(tmp_path, encryption_key) -> ConsoleStore:
    return ConsoleStore(tmp_path / "console.sqlite3", encryption_key)


def screening(
    *,
    sequence: str = "1",
    free_seats: int = 0,
    control_yn: str = "N",
) -> Screening:
    second = sequence != "1"
    return Screening(
        company_code="A420",
        site_no="0013",
        site_name="용산아이파크몰",
        movie_no="30001323",
        movie_name="오디세이",
        screening_date="20260810",
        screen_no="018",
        screen_name="IMAX관",
        sequence=sequence,
        start_time="1330" if second else "1000",
        end_time="1632" if second else "1302",
        format_name="IMAX LASER 2D",
        screen_grade_code="0301",
        total_seats=624,
        free_seats=free_seats,
        control_yn=control_yn,
        booking_url="https://cgv.co.kr/cnm/movieBook/movie?siteNo=0013",
    )


def test_sqlite_pragmas_health_and_explicit_connection_close(store) -> None:
    with store._connection() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")

    assert store.health_check() == {
        "quick_check": "ok",
        "foreign_keys": True,
        "journal_mode": "wal",
        "busy_timeout": 5000,
    }


def test_default_target_is_idempotent_and_target_update_is_versioned(store) -> None:
    first = store.ensure_default_target()
    second = store.ensure_default_target()

    assert first["id"] == second["id"]
    assert first["auto_track_new"] is True
    assert len(store.list_targets()) == 1
    assert store.get_target(first["id"]) == first

    updated = store.update_target(
        first["id"],
        expected_version=first["version"],
        notify_new=False,
        poll_interval_seconds=90,
    )
    assert updated["version"] == first["version"] + 1
    assert updated["notify_new"] is False
    assert updated["poll_interval_seconds"] == 90

    with pytest.raises(StaleVersionError, match="stale"):
        store.update_target(
            first["id"],
            expected_version=first["version"],
            notify_new=True,
        )


def test_create_refresh_due_and_target_runtime_state(store) -> None:
    target = store.create_target(
        {
            "site_no": "0001",
            "site_name": "강남",
            "movie_no": "movie-2",
            "movie_name": "두 번째 영화",
            "format_keyword": "IMAX",
            "auto_track_new": False,
        }
    )
    assert [item["id"] for item in store.due_targets()] == [target["id"]]

    started = store.mark_target_started(
        target["id"],
        expected_version=target["version"],
    )
    assert started["state"] == "running"
    assert started["version"] == target["version"]

    future = datetime.now(UTC) + timedelta(hours=1)
    succeeded = store.mark_target_success(
        target["id"],
        future,
        expected_version=target["version"],
    )
    assert succeeded["state"] == "idle"
    assert succeeded["consecutive_failures"] == 0
    assert store.due_targets() == []

    refreshed = store.request_refresh(
        target["id"],
        expected_version=target["version"],
    )
    assert refreshed["refresh_requested_at"] is not None
    assert [item["id"] for item in store.due_targets()] == [target["id"]]

    failed = store.mark_target_failure(
        target["id"],
        "CGV blocked",
        future,
        expected_version=target["version"],
    )
    assert failed["state"] == "error"
    assert failed["consecutive_failures"] == 1
    assert failed["last_error"] == "CGV blocked"


def test_first_poll_is_baseline_then_new_screening_is_notified_and_auto_tracked(
    store,
) -> None:
    target = store.ensure_default_target(auto_track_new=True, notify_new=True)
    baseline_item = screening(free_seats=3)

    baseline = store.apply_snapshot(target["id"], target["version"], [baseline_item])
    assert baseline == {
        "initialized_before_poll": False,
        "screening_count": 1,
        "discovered_screening_count": 1,
        "changed_screening_count": 0,
        "new_screening_count": 0,
        "booking_opened_count": 0,
        "seat_increase_count": 0,
        "seat_decrease_count": 0,
        "auto_tracked_count": 0,
    }
    listed = store.list_screenings(target["id"], include_history=True)
    assert listed[0]["watched"] is False
    assert listed[0]["revision"] == 1
    assert [row["revision"] for row in listed[0]["history"]] == [1]
    assert store.pending_events() == []

    unchanged = store.apply_snapshot(
        target["id"],
        target["version"],
        [baseline_item],
    )
    assert unchanged["changed_screening_count"] == 0
    assert (
        store.list_screenings(target["id"], include_history=True)[0]["history"]
        == listed[0]["history"]
    )

    new_item = screening(sequence="2", free_seats=10)
    changed = store.apply_snapshot(
        target["id"],
        target["version"],
        [baseline_item, new_item],
    )
    assert changed["new_screening_count"] == 1
    assert changed["auto_tracked_count"] == 1
    pending = store.pending_events()
    assert len(pending) == 1
    assert pending[0].kind == "new_screenings"
    assert pending[0].event_key.endswith(":1:new_screenings")
    screenings = store.list_screenings(target["id"])
    assert [item["watched"] for item in screenings] == [False, True]


def test_control_y_to_n_creates_booking_opened_event(store) -> None:
    target = store.ensure_default_target(auto_track_new=False)
    prepared = screening(free_seats=20, control_yn="Y")
    store.apply_snapshot(target["id"], target["version"], [prepared])

    opened = store.apply_snapshot(
        target["id"],
        target["version"],
        [replace(prepared, control_yn="N")],
    )

    assert opened["booking_opened_count"] == 1
    assert opened["seat_increase_count"] == 0
    pending = store.pending_events()
    assert len(pending) == 1
    assert pending[0].kind == "booking_opened"
    assert pending[0].event_key.endswith(":2:booking_opened")


def test_booking_state_transition_does_not_duplicate_as_seat_change(store) -> None:
    target = store.ensure_default_target(auto_track_new=False)
    prepared = screening(free_seats=0, control_yn="Y")
    store.apply_snapshot(target["id"], target["version"], [prepared])
    screening_id = store.list_screenings(target["id"])[0]["id"]
    store.set_watch(screening_id, True, seat_change_threshold=3)

    opened = store.apply_snapshot(
        target["id"],
        target["version"],
        [replace(prepared, control_yn="N", free_seats=20)],
    )

    assert opened["booking_opened_count"] == 1
    assert opened["seat_increase_count"] == 0
    assert opened["seat_decrease_count"] == 0
    assert [event.kind for event in store.pending_events()] == ["booking_opened"]

    booking_event = store.pending_events()[0]
    store.mark_sent(booking_event.id)
    closed = store.apply_snapshot(
        target["id"],
        target["version"],
        [replace(prepared, control_yn="Y", free_seats=0)],
    )

    assert closed["seat_increase_count"] == 0
    assert closed["seat_decrease_count"] == 0
    assert store.pending_events() == []
    activity = store.list_activity_page(target_id=target["id"])["items"]
    assert activity[0]["kind"] == "booking_closed"
    assert activity[0]["notification"] is None
    assert activity[1]["kind"] == "booking_opened"
    assert activity[1]["notification"]["kind"] == "booking_opened"


def test_notify_new_off_suppresses_booking_opened_and_missing_rows_become_inactive(
    store,
) -> None:
    target = store.ensure_default_target(notify_new=False)
    prepared = screening(free_seats=20, control_yn="Y")
    store.apply_snapshot(target["id"], target["version"], [prepared])
    store.apply_snapshot(
        target["id"],
        target["version"],
        [replace(prepared, control_yn="N")],
    )
    assert store.pending_events() == []

    store.apply_snapshot(target["id"], target["version"], [])
    assert store.list_screenings(target["id"]) == []

    store.apply_snapshot(
        target["id"],
        target["version"],
        [replace(prepared, control_yn="N")],
    )
    restored = store.list_screenings(target["id"])
    assert len(restored) == 1
    assert restored[0]["active"] is True
    assert store.pending_events() == []


def test_watched_repeated_zero_to_one_transitions_are_distinct(store) -> None:
    target = store.ensure_default_target(auto_track_new=False)
    zero = screening(free_seats=0)
    store.apply_snapshot(target["id"], target["version"], [zero])
    screening_id = store.list_screenings(target["id"])[0]["id"]
    assert store.set_watch(screening_id, True)["enabled"] is True

    first_summary = store.apply_snapshot(
        target["id"],
        target["version"],
        [replace(zero, free_seats=1)],
    )
    assert first_summary["seat_increase_count"] == 1
    first = store.pending_events()[0]
    assert isinstance(first, OutboxEvent)
    store.mark_sent(first.id)

    decreased = store.apply_snapshot(
        target["id"],
        target["version"],
        [zero],
    )
    assert decreased["seat_increase_count"] == 0
    assert decreased["seat_decrease_count"] == 0
    assert store.pending_events() == []
    second_summary = store.apply_snapshot(
        target["id"],
        target["version"],
        [replace(zero, free_seats=1)],
    )
    assert second_summary["seat_increase_count"] == 1
    second = store.pending_events()[0]
    assert second.event_key != first.event_key
    assert second.event_key.endswith(":4:seat_increases")

    identical = store.apply_snapshot(
        target["id"],
        target["version"],
        [replace(zero, free_seats=1)],
    )
    assert identical["changed_screening_count"] == 0
    assert len(store.pending_events()) == 1
    history = store.list_screenings(target["id"], include_history=True)[0]["history"]
    assert [row["free_seats"] for row in history] == [0, 1, 0, 1]

    store.mark_sent(second.id)
    store.set_watch(screening_id, False)
    unwatched = store.apply_snapshot(
        target["id"],
        target["version"],
        [replace(zero, free_seats=2)],
    )
    assert unwatched["seat_increase_count"] == 0
    assert store.pending_events() == []


def test_per_screening_threshold_applies_to_increases_but_not_history(
    store,
) -> None:
    target = store.ensure_default_target(auto_track_new=False)
    original = screening(free_seats=10)
    store.apply_snapshot(target["id"], target["version"], [original])
    screening_id = store.list_screenings(target["id"])[0]["id"]

    watch = store.set_watch(screening_id, True, seat_change_threshold=3)
    assert watch == {
        "screening_id": screening_id,
        "target_id": target["id"],
        "enabled": True,
        "seat_change_threshold": 3,
    }

    below = replace(original, free_seats=12)
    below_summary = store.apply_snapshot(target["id"], target["version"], [below])
    assert below_summary["seat_increase_count"] == 0
    assert store.pending_events() == []

    decreased = replace(below, free_seats=9)
    decrease_summary = store.apply_snapshot(
        target["id"], target["version"], [decreased]
    )
    assert decrease_summary["seat_decrease_count"] == 0
    assert store.pending_events() == []

    increased = replace(decreased, free_seats=12)
    increase_summary = store.apply_snapshot(
        target["id"], target["version"], [increased]
    )
    assert increase_summary["seat_increase_count"] == 1
    increase = store.pending_events()[0]
    assert increase.kind == "seat_increases"
    assert increase.payload["seat_change_threshold"] == 3
    assert increase.payload["seat_delta"] == 3

    listed = store.list_screenings(target["id"], include_history=True)[0]
    assert listed["seat_change_threshold"] == 3
    assert [row["free_seats"] for row in listed["history"]] == [10, 12, 9, 12]
    activity = store.list_activity_page(target_id=target["id"], kind="seat_increases")[
        "items"
    ]
    assert len(activity) == 2
    assert activity[0]["notification"]["seat_change_threshold"] == 3
    assert activity[0]["notification"]["seat_delta"] == 3
    assert activity[1]["notification"] is None


def test_disabled_watch_keeps_threshold_and_records_without_alert(store) -> None:
    target = store.ensure_default_target(auto_track_new=False)
    original = screening(free_seats=10)
    store.apply_snapshot(target["id"], target["version"], [original])
    screening_id = store.list_screenings(target["id"])[0]["id"]
    store.set_watch(screening_id, True, seat_change_threshold=4)
    disabled = store.set_watch(screening_id, False)
    assert disabled["seat_change_threshold"] == 4

    changed = store.apply_snapshot(
        target["id"], target["version"], [replace(original, free_seats=20)]
    )
    assert changed["seat_increase_count"] == 0
    assert store.pending_events() == []
    listed = store.list_screenings(target["id"], include_history=True)[0]
    assert listed["watched"] is False
    assert listed["seat_change_threshold"] == 4
    assert [row["free_seats"] for row in listed["history"]] == [10, 20]


def test_bulk_threshold_updates_only_enabled_watches_for_target(store) -> None:
    target = store.ensure_default_target(auto_track_new=False)
    store.apply_snapshot(
        target["id"],
        target["version"],
        [screening(sequence="1"), screening(sequence="2")],
    )
    first, second = store.list_screenings(target["id"])
    store.set_watch(first["id"], True, 2)
    store.set_watch(second["id"], True, 3)
    store.set_watch(second["id"], False)

    result = store.set_watched_thresholds(target["id"], 7)
    assert result == {
        "target_id": target["id"],
        "seat_change_threshold": 7,
        "updated_count": 1,
    }
    listed = store.list_screenings(target["id"])
    assert [item["seat_change_threshold"] for item in listed] == [7, 3]

    with pytest.raises(KeyError, match="target 999999"):
        store.set_watched_thresholds(999999, 2)
    with pytest.raises(ValueError, match="supported range"):
        store.set_watched_thresholds(target["id"], 0)
    with pytest.raises(ValueError, match="supported range"):
        store.set_watched_thresholds(target["id"], 1 << 53)


def test_watch_threshold_migration_defaults_existing_rows_to_one(
    store,
    encryption_key,
) -> None:
    target = store.ensure_default_target(auto_track_new=False)
    store.apply_snapshot(target["id"], target["version"], [screening()])
    screening_id = store.list_screenings(target["id"])[0]["id"]
    store.set_watch(screening_id, True)

    with sqlite3.connect(store.path) as connection:
        connection.executescript(
            """
            ALTER TABLE screening_watches RENAME TO current_screening_watches;
            CREATE TABLE screening_watches (
                screening_id INTEGER PRIMARY KEY
                    REFERENCES console_screenings(id) ON DELETE CASCADE,
                enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO screening_watches(
                screening_id, enabled, created_at, updated_at
            )
            SELECT screening_id, enabled, created_at, updated_at
            FROM current_screening_watches;
            DROP TABLE current_screening_watches;
            """
        )

    migrated = ConsoleStore(store.path, encryption_key)
    listed = migrated.list_screenings(target["id"])[0]
    assert listed["watched"] is True
    assert listed["seat_change_threshold"] == 1


def test_stale_snapshot_is_rejected_without_partial_state(store) -> None:
    target = store.ensure_default_target()
    updated = store.update_target(
        target["id"],
        expected_version=target["version"],
        auto_track_new=False,
    )
    assert updated["version"] != target["version"]

    with pytest.raises(StaleVersionError, match="stale"):
        store.apply_snapshot(target["id"], target["version"], [screening()])

    assert store.list_screenings(target["id"]) == []
    assert store.get_target(target["id"])["initialized"] is False


def test_console_outbox_retry_dead_letter_requeue_and_delivery_parts(store) -> None:
    target = store.ensure_default_target()
    store.apply_snapshot(target["id"], target["version"], [screening()])
    store.apply_snapshot(
        target["id"],
        target["version"],
        [screening(), screening(sequence="2", free_seats=5)],
    )
    event = store.pending_events()[0]

    store.mark_part_delivered(event.id, 2)
    store.mark_part_delivered(event.id, 1)
    assert store.pending_events()[0].delivered_parts == 2

    store.mark_failed(event.id, "rate limited", 3600)
    assert store.pending_events() == []
    pending_row = store.list_outbox(status="pending")[0]
    assert pending_row["attempts"] == 1
    assert pending_row["last_error"] == "rate limited"

    store.mark_dead(event.id, "permanent failure")
    assert store.list_outbox(status="dead")[0]["status"] == "dead"
    assert store.requeue_dead(event.id) == 1
    requeued = store.pending_events()[0]
    assert requeued.attempts == 0
    assert requeued.delivered_parts == 2

    store.mark_sent(event.id)
    assert store.pending_events() == []
    sent = store.list_outbox(status="sent")[0]
    assert sent["status"] == "sent"
    assert sent["payload"]["screenings"][0]["sequence"] == "2"


def test_telegram_token_is_encrypted_and_can_be_preserved_on_update(
    tmp_path,
    encryption_key,
) -> None:
    database = tmp_path / "console.sqlite3"
    store = ConsoleStore(database, encryption_key)
    token = "123456:super-secret-token"

    saved = store.save_telegram_config(bot_token=token, chat_id="-100123")
    assert saved["token_configured"] is True
    assert "bot_token" not in saved
    assert store.get_telegram_config(include_token=True)["bot_token"] == token

    connection = sqlite3.connect(database)
    try:
        ciphertext = connection.execute(
            "SELECT bot_token_ciphertext FROM telegram_config WHERE id = 1"
        ).fetchone()[0]
    finally:
        connection.close()
    assert token.encode("utf-8") not in bytes(ciphertext)

    updated = store.save_telegram_config(
        chat_id="-100999",
        expected_version=saved["version"],
    )
    assert updated["version"] == saved["version"] + 1
    assert store.get_telegram_config(include_token=True) == {
        **updated,
        "bot_token": token,
    }

    with pytest.raises(StaleVersionError, match="stale"):
        store.save_telegram_config(
            chat_id="-100000",
            expected_version=saved["version"],
        )

    wrong_key_store = ConsoleStore(database, Fernet.generate_key().decode("ascii"))
    with pytest.raises(RuntimeError, match="cannot be decrypted"):
        wrong_key_store.get_telegram_config(include_token=True)


def test_metadata_persists_across_store_instances(tmp_path, encryption_key) -> None:
    database = tmp_path / "console.sqlite3"
    store = ConsoleStore(database, encryption_key)
    assert store.get_metadata("worker_id") is None

    store.set_metadata("worker_id", "worker-1")

    reopened = ConsoleStore(database, encryption_key)
    assert reopened.get_metadata("worker_id") == "worker-1"
    assert reopened.health_check()["quick_check"] == "ok"
