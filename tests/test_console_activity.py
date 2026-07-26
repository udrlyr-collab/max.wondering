from __future__ import annotations

import sqlite3
from dataclasses import replace

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from moviemax.console_config import ConsoleSettings
from moviemax.console_store import ConsoleStore
from moviemax.console_web import create_app
from tests.test_console_store import screening


def _settings(tmp_path) -> ConsoleSettings:
    return ConsoleSettings(
        database_path=tmp_path / "console.sqlite3",
        encryption_key=Fernet.generate_key().decode("ascii"),
        public_origin="https://max.wondering.kr",
        allowed_hosts=("testserver",),
        seed_default_target=False,
    )


def test_detection_activity_is_complete_and_cursor_stable(tmp_path) -> None:
    settings = _settings(tmp_path)
    store = ConsoleStore(settings.database_path, settings.encryption_key)
    target = store.ensure_default_target(auto_track_new=False)
    original = screening(free_seats=10)
    store.apply_snapshot(target["id"], target["version"], [original])
    screening_id = store.list_screenings(target["id"])[0]["id"]
    store.set_watch(screening_id, True)

    decreased = replace(original, free_seats=4)
    decrease_summary = store.apply_snapshot(
        target["id"],
        target["version"],
        [decreased],
    )
    assert decrease_summary["changed_screening_count"] == 1
    assert decrease_summary["seat_increase_count"] == 0
    assert decrease_summary["seat_decrease_count"] == 0
    assert store.pending_events() == []

    increased = replace(decreased, free_seats=6)
    store.apply_snapshot(target["id"], target["version"], [increased])
    updated = replace(increased, screen_name="IMAX관 리뉴얼")
    store.apply_snapshot(target["id"], target["version"], [updated])

    history = store.list_screenings(target["id"], include_history=True)[0]["history"]
    assert [row["free_seats"] for row in history] == [10, 4, 6, 6]
    assert history[1]["payload"]["booking_url"] == original.booking_url
    assert history[3]["payload"]["screen_name"] == "IMAX관 리뉴얼"

    first_page = store.list_activity_page(limit=2, target_id=target["id"])
    assert [item["revision"] for item in first_page["items"]] == [4, 3]
    assert first_page["has_more"] is True
    assert first_page["next_cursor"] == first_page["items"][-1]["id"]

    latest = first_page["items"][0]
    assert latest["kind"] == "screening_updated"
    assert latest["screening_id"] == screening_id
    assert latest["screening"]["screen_name"] == "IMAX관 리뉴얼"
    assert latest["previous_screening"]["screen_name"] == "IMAX관"
    assert latest["changes"] == [
        {
            "field": "screen_name",
            "before": "IMAX관",
            "after": "IMAX관 리뉴얼",
        }
    ]
    assert latest["details_complete"] is True
    assert latest["booking_url"] == original.booking_url

    increase = first_page["items"][1]
    assert increase["kind"] == "seat_increases"
    assert increase["changes"] == [
        {"field": "free_seats", "before": 4, "after": 6, "delta": 2}
    ]
    assert increase["notification"]["kind"] == "seat_increases"
    assert increase["notification"]["status"] == "pending"
    pending_payload = next(
        event.payload["changes"][0]
        for event in store.pending_events()
        if event.kind == "seat_increases"
    )
    assert pending_payload["screening"]["booking_url"] == original.booking_url
    assert pending_payload["previous_screening"]["free_seats"] == 4

    store.apply_snapshot(
        target["id"],
        target["version"],
        [replace(updated, total_seats=620)],
    )
    second_page = store.list_activity_page(
        limit=2,
        cursor=first_page["next_cursor"],
        target_id=target["id"],
    )
    assert [item["revision"] for item in second_page["items"]] == [2, 1]
    assert second_page["has_more"] is False
    assert {item["id"] for item in first_page["items"]}.isdisjoint(
        item["id"] for item in second_page["items"]
    )

    decreases = store.list_activity_page(
        kind="seat_decreases",
        target_id=target["id"],
    )
    assert len(decreases["items"]) == 1
    decrease = decreases["items"][0]
    assert decrease["revision"] == 2
    assert decrease["changes"] == [
        {"field": "free_seats", "before": 10, "after": 4, "delta": -6}
    ]
    assert decrease["notification"] is None

    alerts = store.list_activity_page(
        target_id=target["id"],
        notifications_only=True,
    )
    assert [item["kind"] for item in alerts["items"]] == ["seat_increases"]


def test_notifications_only_skips_newer_nonqualifying_activity(tmp_path) -> None:
    settings = _settings(tmp_path)
    store = ConsoleStore(settings.database_path, settings.encryption_key)
    target = store.ensure_default_target(auto_track_new=False)
    item = screening(free_seats=0)
    store.apply_snapshot(target["id"], target["version"], [item])
    screening_id = store.list_screenings(target["id"])[0]["id"]
    store.set_watch(screening_id, True, seat_change_threshold=5)

    qualifying = replace(item, free_seats=5)
    store.apply_snapshot(target["id"], target["version"], [qualifying])
    current = qualifying
    for free_seats in range(6, 16):
        current = replace(current, free_seats=free_seats)
        store.apply_snapshot(target["id"], target["version"], [current])

    full = store.list_activity_page(limit=1, target_id=target["id"])
    assert full["items"][0]["revision"] == 12
    assert full["items"][0]["notification"] is None

    alerts = store.list_activity_page(
        limit=1,
        target_id=target["id"],
        notifications_only=True,
    )
    assert len(alerts["items"]) == 1
    assert alerts["items"][0]["revision"] == 2
    assert alerts["items"][0]["notification"]["seat_delta"] == 5
    assert alerts["has_more"] is False

    app = create_app(settings, store=store, catalog_client=object())
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/activity",
            params={
                "limit": 1,
                "target_id": target["id"],
                "notifications_only": True,
            },
        )
    assert response.status_code == 200
    assert response.json()["items"][0]["notification"]["seat_delta"] == 5


def test_legacy_decrease_outbox_is_neither_delivered_nor_a_recent_alert(
    tmp_path,
) -> None:
    settings = _settings(tmp_path)
    store = ConsoleStore(settings.database_path, settings.encryption_key)
    target = store.ensure_default_target(auto_track_new=False)
    original = screening(free_seats=10)
    store.apply_snapshot(target["id"], target["version"], [original])
    screening_id = store.list_screenings(target["id"])[0]["id"]
    decreased = replace(original, free_seats=4)
    store.apply_snapshot(target["id"], target["version"], [decreased])

    with store._connection(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO console_outbox(
                target_id, screening_id, revision, event_key,
                kind, payload_json, created_at
            ) VALUES (?, ?, 2, ?, 'seat_decreases', '{}', ?)
            """,
            (
                target["id"],
                screening_id,
                "legacy-decrease",
                "2026-08-10T00:00:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO console_outbox(
                target_id, screening_id, revision, event_key,
                kind, payload_json, created_at, dead_lettered_at
            ) VALUES (?, ?, 2, ?, 'seat_decreases', '{}', ?, ?)
            """,
            (
                target["id"],
                screening_id,
                "legacy-decrease-dead",
                "2026-08-10T00:00:01+00:00",
                "2026-08-10T00:00:02+00:00",
            ),
        )

    full = store.list_activity_page(target_id=target["id"])["items"]
    assert full[0]["kind"] == "seat_decreases"
    assert full[0]["notification"]["kind"] == "seat_decreases"
    assert store.pending_events() == []
    assert (
        store.list_activity_page(
            target_id=target["id"],
            notifications_only=True,
        )["items"]
        == []
    )

    reopened = ConsoleStore(settings.database_path, settings.encryption_key)
    cleaned = reopened.list_activity_page(target_id=target["id"])["items"]
    assert cleaned[0]["kind"] == "seat_decreases"
    assert cleaned[0]["notification"] is None
    assert all(item["kind"] != "seat_decreases" for item in reopened.list_outbox())


def test_activity_api_filters_and_serves_separate_page(tmp_path) -> None:
    settings = _settings(tmp_path)
    store = ConsoleStore(settings.database_path, settings.encryption_key)
    target = store.ensure_default_target(auto_track_new=False)
    item = screening(free_seats=3)
    store.apply_snapshot(target["id"], target["version"], [item])
    screening_id = store.list_screenings(target["id"])[0]["id"]
    app = create_app(settings, store=store, catalog_client=object())

    with TestClient(app) as client:
        activity = client.get(
            "/api/v1/activity",
            params={
                "limit": 1,
                "target_id": target["id"],
                "screening_id": screening_id,
                "kind": "new_screenings",
            },
        )
        invalid = client.get("/api/v1/activity", params={"limit": 101})
        page = client.get("/activity")
        screenings = client.get(f"/api/v1/targets/{target['id']}/screenings")
        bootstrap = client.get("/api/v1/bootstrap")

    assert activity.status_code == 200
    assert activity.json()["items"][0]["booking_url"] == item.booking_url
    assert activity.json()["next_cursor"] is None
    assert activity.json()["has_more"] is False
    assert invalid.status_code == 422
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert screenings.json()["screenings"][0]["booking_url"] == item.booking_url
    assert bootstrap.json()["activity"][0]["screening_id"] == screening_id


def test_activity_classifies_booking_control_and_total_seat_changes(tmp_path) -> None:
    settings = _settings(tmp_path)
    store = ConsoleStore(settings.database_path, settings.encryption_key)
    target = store.ensure_default_target(auto_track_new=False)
    prepared = screening(free_seats=0, control_yn="Y")
    store.apply_snapshot(target["id"], target["version"], [prepared])
    opened = replace(prepared, control_yn="N")
    store.apply_snapshot(target["id"], target["version"], [opened])
    closed = replace(opened, control_yn="Y")
    store.apply_snapshot(target["id"], target["version"], [closed])
    resized = replace(closed, total_seats=620)
    store.apply_snapshot(target["id"], target["version"], [resized])

    items = store.list_activity_page(limit=10)["items"]

    assert [item["kind"] for item in items] == [
        "total_seats_changed",
        "booking_closed",
        "booking_opened",
        "new_screenings",
    ]
    assert items[0]["changes"] == [
        {"field": "total_seats", "before": 624, "after": 620, "delta": -4}
    ]
    assert items[1]["changes"] == [{"field": "control_yn", "before": "N", "after": "Y"}]
    assert items[2]["changes"] == [{"field": "control_yn", "before": "Y", "after": "N"}]


def test_history_payload_migration_backfills_available_current_revision(
    tmp_path,
) -> None:
    settings = _settings(tmp_path)
    store = ConsoleStore(settings.database_path, settings.encryption_key)
    target = store.ensure_default_target(auto_track_new=False)
    original = screening(free_seats=1)
    store.apply_snapshot(target["id"], target["version"], [original])
    changed = replace(original, free_seats=2)
    store.apply_snapshot(target["id"], target["version"], [changed])

    with sqlite3.connect(settings.database_path) as connection:
        connection.executescript(
            """
            ALTER TABLE seat_history RENAME TO legacy_seat_history;
            CREATE TABLE seat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                screening_id INTEGER NOT NULL
                    REFERENCES console_screenings(id) ON DELETE CASCADE,
                revision INTEGER NOT NULL,
                free_seats INTEGER NOT NULL,
                total_seats INTEGER NOT NULL,
                control_yn TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                UNIQUE(screening_id, revision)
            );
            INSERT INTO seat_history(
                id, screening_id, revision, free_seats, total_seats,
                control_yn, observed_at
            )
            SELECT id, screening_id, revision, free_seats, total_seats,
                   control_yn, observed_at
            FROM legacy_seat_history;
            DROP TABLE legacy_seat_history;
            """
        )

    migrated = ConsoleStore(settings.database_path, settings.encryption_key)
    history = migrated.list_screenings(target["id"], include_history=True)[0]["history"]

    assert history[0]["payload"] is None
    assert history[1]["payload"]["free_seats"] == 2
    latest = migrated.list_activity_page(limit=1)["items"][0]
    assert latest["kind"] == "seat_increases"
    assert latest["details_complete"] is False
    assert latest["changes"] == [
        {"field": "free_seats", "before": 1, "after": 2, "delta": 1}
    ]


def test_booking_url_only_refreshes_metadata_without_new_revision(tmp_path) -> None:
    settings = _settings(tmp_path)
    store = ConsoleStore(settings.database_path, settings.encryption_key)
    target = store.ensure_default_target(auto_track_new=False)
    original = screening(free_seats=7)
    store.apply_snapshot(target["id"], target["version"], [original])
    updated_url = (
        "https://cgv.co.kr/cnm/movieBook/movie?"
        "movNo=30001323&scnSseq=1&scnYmd=20260810&scnsNo=018&"
        "siteNm=%EC%9A%A9%EC%82%B0%EC%95%84%EC%9D%B4%ED%8C%8C%ED%81%AC%EB%AA%B0&"
        "siteNo=0013"
    )

    summary = store.apply_snapshot(
        target["id"],
        target["version"],
        [replace(original, booking_url=updated_url)],
    )

    assert summary["changed_screening_count"] == 0
    assert summary["seat_increase_count"] == 0
    assert store.pending_events() == []
    listed = store.list_screenings(target["id"], include_history=True)[0]
    assert listed["revision"] == 1
    assert listed["booking_url"] == updated_url
    assert len(listed["history"]) == 1
    assert listed["history"][0]["payload"]["booking_url"] == updated_url
    activity = store.list_activity_page(limit=10)["items"]
    assert len(activity) == 1
    assert activity[0]["kind"] == "new_screenings"
    assert activity[0]["booking_url"] == updated_url
