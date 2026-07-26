from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import pytest
from cryptography.fernet import Fernet

from moviemax.config import Settings
from moviemax.console_config import ConsoleSettings
from moviemax.console_store import ConsoleStore
from moviemax.console_worker import ConsoleWorker, console_worker_health
from moviemax.telegram import TelegramError
from tests.test_console_store import screening


class FakeTargetCgvClient:
    created_settings: ClassVar[list[Settings]] = []
    schedules: ClassVar[dict[str, list]] = {}
    date_calls: ClassVar[list[str]] = []
    schedule_calls: ClassVar[list[tuple[str, str]]] = []

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.created_settings.append(settings)

    def get_screening_dates(self, movie_no: str) -> list[str]:
        self.date_calls.append(movie_no)
        return sorted(self.schedules)

    def get_imax_screenings(self, movie_no: str, screening_date: str) -> list:
        self.schedule_calls.append((movie_no, screening_date))
        return self.schedules[screening_date]


class FakeTelegramSender:
    sent: ClassVar[list[tuple[str, str, str]]] = []
    error: ClassVar[Exception | None] = None

    def __init__(self, token: str, chat_id: str, _timeout: float) -> None:
        self.token = token
        self.chat_id = chat_id

    def send_message(self, message: str) -> None:
        if self.error is not None:
            raise self.error
        self.sent.append((self.token, self.chat_id, message))


@pytest.fixture
def worker_context(tmp_path):
    settings = ConsoleSettings(
        database_path=tmp_path / "console.sqlite3",
        encryption_key=Fernet.generate_key().decode("ascii"),
        public_origin="https://max.wondering.kr",
        allowed_hosts=("testserver",),
        worker_tick_seconds=1,
        seed_default_target=False,
    )
    base_settings = Settings(
        request_gap_seconds=0,
        poll_interval_seconds=60,
        backoff_max_seconds=900,
        telegram_max_attempts=3,
        telegram_retry_base_seconds=1,
    )
    store = ConsoleStore(settings.database_path, settings.encryption_key)
    worker = ConsoleWorker(settings, base_settings=base_settings, store=store)
    return settings, base_settings, store, worker


def test_fetch_target_uses_target_specific_cgv_client(
    worker_context,
    monkeypatch,
) -> None:
    _settings, _base, store, worker = worker_context
    target = store.create_target(
        site_no="0001",
        site_name="강남",
        movie_no="movie-2",
        movie_name="두 번째 영화",
        format_keyword="IMAX",
        screen_grade_code="0301",
    )
    FakeTargetCgvClient.created_settings.clear()
    FakeTargetCgvClient.date_calls.clear()
    FakeTargetCgvClient.schedule_calls.clear()
    FakeTargetCgvClient.schedules = {
        "20260810": [screening(free_seats=1)],
        "20260811": [screening(sequence="2", free_seats=2)],
    }
    monkeypatch.setattr("moviemax.console_worker.CgvClient", FakeTargetCgvClient)

    fetched = worker.fetch_target(target)

    assert len(fetched) == 2
    configured = FakeTargetCgvClient.created_settings[0]
    assert configured.site_no == "0001"
    assert configured.site_name == "강남"
    assert configured.movie_no == "movie-2"
    assert FakeTargetCgvClient.date_calls == ["movie-2"]
    assert FakeTargetCgvClient.schedule_calls == [
        ("movie-2", "20260810"),
        ("movie-2", "20260811"),
    ]
    assert configured.movie_name == "두 번째 영화"


def test_worker_polling_creates_and_delivers_outbox_without_network(
    worker_context,
    monkeypatch,
) -> None:
    _settings, _base, store, worker = worker_context
    target = store.ensure_default_target(auto_track_new=True, notify_new=True)
    snapshots = [
        [screening(free_seats=0)],
        [screening(free_seats=0), screening(sequence="2", free_seats=8)],
    ]
    monkeypatch.setattr(worker, "fetch_target", lambda _target: snapshots.pop(0))

    baseline = worker.process_target(target)
    assert baseline is not None
    assert baseline["initialized_before_poll"] is False
    assert baseline["new_screening_count"] == 0
    assert store.pending_events() == []
    after_baseline = store.get_target(target["id"])
    assert after_baseline["state"] == "idle"
    assert after_baseline["last_success_at"] is not None

    discovered = worker.process_target(after_baseline)
    assert discovered is not None
    assert discovered["new_screening_count"] == 1
    assert discovered["auto_tracked_count"] == 1
    assert len(store.pending_events()) == 1
    assert store.pending_events()[0].kind == "new_screenings"
    assert store.list_screenings(target["id"])[1]["watched"] is True

    token = "1234567890:worker-secret-token"
    store.save_telegram_config(bot_token=token, chat_id="-100123")
    FakeTelegramSender.sent.clear()
    FakeTelegramSender.error = None
    monkeypatch.setattr(
        "moviemax.console_worker.TelegramClient",
        FakeTelegramSender,
    )

    assert worker.deliver_pending() == (1, 0, 0)
    assert len(FakeTelegramSender.sent) == 1
    assert FakeTelegramSender.sent[0][0:2] == (token, "-100123")
    assert "새 예매 회차" in FakeTelegramSender.sent[0][2]
    assert store.pending_events() == []
    assert store.list_outbox(status="sent")[0]["status"] == "sent"


def test_worker_poll_failure_is_backed_off_and_isolated(
    worker_context,
    monkeypatch,
) -> None:
    _settings, _base, store, worker = worker_context
    target = store.ensure_default_target()

    def fail_fetch(_target: dict[str, Any]) -> list:
        raise RuntimeError("CGV unavailable")

    monkeypatch.setattr(worker, "fetch_target", fail_fetch)

    assert worker.process_target(target) is None
    failed = store.get_target(target["id"])
    assert failed["state"] == "error"
    assert failed["consecutive_failures"] == 1
    assert "CGV unavailable" in failed["last_error"]
    assert datetime.fromisoformat(failed["next_poll_at"]) > datetime.now(UTC)
    assert store.list_screenings(target["id"]) == []


def test_stale_poll_releases_running_target(worker_context) -> None:
    _settings, _base, store, worker = worker_context
    target = store.ensure_default_target()
    store.mark_target_started(target["id"], expected_version=target["version"])
    updated = store.update_target(
        target["id"],
        {"notify_new": False},
        expected_version=target["version"],
    )
    assert updated["state"] == "running"

    assert worker.process_target(target) is None

    released = store.get_target(target["id"])
    assert released["state"] == "idle"
    assert released["next_poll_at"] is not None
    assert store.due_targets()[0]["id"] == target["id"]


def test_worker_retryable_telegram_failure_stays_pending(
    worker_context,
    monkeypatch,
) -> None:
    _settings, _base, store, worker = worker_context
    target = store.ensure_default_target()
    store.apply_snapshot(target["id"], target["version"], [screening()])
    store.apply_snapshot(
        target["id"],
        target["version"],
        [screening(), screening(sequence="2", free_seats=5)],
    )
    store.save_telegram_config(
        bot_token="1234567890:worker-secret-token",
        chat_id="-100123",
    )
    FakeTelegramSender.sent.clear()
    FakeTelegramSender.error = TelegramError(
        "rate limited",
        retryable=True,
        retry_after_seconds=120,
    )
    monkeypatch.setattr(
        "moviemax.console_worker.TelegramClient",
        FakeTelegramSender,
    )

    assert worker.deliver_pending() == (0, 1, 0)
    assert store.pending_events() == []
    pending = store.list_outbox(status="pending")[0]
    assert pending["attempts"] == 1
    assert "rate limited" in pending["last_error"]
    assert datetime.fromisoformat(pending["next_attempt_at"]) > datetime.now(UTC)


def test_worker_permanent_telegram_failure_is_dead_lettered(
    worker_context,
    monkeypatch,
) -> None:
    settings, _base, store, worker = worker_context
    target = store.ensure_default_target()
    store.apply_snapshot(target["id"], target["version"], [screening()])
    store.apply_snapshot(
        target["id"],
        target["version"],
        [screening(), screening(sequence="2", free_seats=5)],
    )
    store.save_telegram_config(
        bot_token="1234567890:worker-secret-token",
        chat_id="-100123",
    )
    FakeTelegramSender.error = TelegramError("forbidden", retryable=False)
    monkeypatch.setattr(
        "moviemax.console_worker.TelegramClient",
        FakeTelegramSender,
    )

    assert worker.deliver_pending() == (0, 1, 1)
    assert store.pending_events() == []
    assert store.list_outbox(status="dead")[0]["status"] == "dead"
    heartbeat = worker.heartbeat()
    assert heartbeat["status"] == "degraded"
    assert heartbeat["dead_letters"] == 1
    with pytest.raises(RuntimeError, match="dead-letter"):
        console_worker_health(settings)


def test_worker_heartbeat_health_happy_and_stale_paths(worker_context) -> None:
    settings, _base, store, worker = worker_context
    store.ensure_default_target()

    heartbeat = worker.heartbeat()
    assert heartbeat["status"] == "ok"
    health = console_worker_health(settings)
    assert health["heartbeat"] == heartbeat
    assert health["age_seconds"] < 5
    assert health["database"]["journal_mode"] == "wal"

    stale = {
        **heartbeat,
        "timestamp": (datetime.now(UTC) - timedelta(seconds=61)).isoformat(),
    }
    store.set_metadata(
        "console_worker_heartbeat",
        json.dumps(stale, ensure_ascii=False),
    )
    with pytest.raises(RuntimeError, match="stale"):
        console_worker_health(settings)
