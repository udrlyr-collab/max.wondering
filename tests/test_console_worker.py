from __future__ import annotations

import json
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import pytest
from cryptography.fernet import Fernet

from moviemax.config import Settings
from moviemax.console_config import ConsoleSettings
from moviemax.console_store import ConsoleStore
from moviemax.console_worker import ConsoleWorker, console_worker_health
from moviemax.telegram import TelegramError
from moviemax.web_push import WebPushError
from tests.test_console_store import screening, web_push_subscription


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

    def get_screenings(self, movie_no: str, screening_date: str) -> list:
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


class FakeWebPushSender:
    sent: ClassVar[list[tuple[dict[str, Any], dict[str, Any]]]] = []
    attempted: ClassVar[list[dict[str, Any]]] = []
    error: ClassVar[WebPushError | None] = None

    def __init__(self, _private_key: str, _subject: str, _timeout: float) -> None:
        pass

    def send(
        self,
        subscription_info: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        self.attempted.append(subscription_info)
        if self.error is not None:
            raise self.error
        self.sent.append((subscription_info, payload))


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
        format_code="44",
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
    assert configured.format_code == "44"
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


def test_web_push_delivers_without_telegram_and_keeps_channel_state_separate(
    worker_context,
    monkeypatch,
) -> None:
    _settings, _base, store, worker = worker_context
    target = store.ensure_default_target(auto_track_new=False)
    store.save_web_push_subscription(**web_push_subscription())
    original = screening(free_seats=1)
    store.apply_snapshot(target["id"], target["version"], [original])
    screening_id = store.list_screenings(target["id"])[0]["id"]
    store.set_watch(screening_id, True)
    store.apply_snapshot(
        target["id"],
        target["version"],
        [screening(free_seats=3)],
    )

    FakeWebPushSender.sent.clear()
    FakeWebPushSender.attempted.clear()
    FakeWebPushSender.error = None
    monkeypatch.setattr(
        "moviemax.console_worker.WebPushClient",
        FakeWebPushSender,
    )

    assert worker.deliver_pending() == (0, 0, 0)
    assert worker.deliver_pending_web_push() == (1, 0, 0)
    assert len(FakeWebPushSender.sent) == 1
    payload = FakeWebPushSender.sent[0][1]
    assert payload["title"] == "잔여석 +2 · 오디세이"
    assert payload["url"].startswith("https://max.wondering.kr/booking?url=")
    assert len(store.list_web_push_deliveries(status="sent")) == 1
    assert len(store.pending_events()) == 1


def test_expired_web_push_subscription_is_removed_without_dead_letter(
    worker_context,
    monkeypatch,
) -> None:
    _settings, _base, store, worker = worker_context
    target = store.ensure_default_target(auto_track_new=False)
    store.save_web_push_subscription(**web_push_subscription())
    original = screening(free_seats=1)
    store.apply_snapshot(target["id"], target["version"], [original])
    screening_id = store.list_screenings(target["id"])[0]["id"]
    store.set_watch(screening_id, True)
    store.apply_snapshot(
        target["id"],
        target["version"],
        [screening(free_seats=2)],
    )
    FakeWebPushSender.error = WebPushError(
        "expired",
        expired=True,
        status_code=410,
    )
    FakeWebPushSender.attempted.clear()
    monkeypatch.setattr(
        "moviemax.console_worker.WebPushClient",
        FakeWebPushSender,
    )

    assert worker.deliver_pending_web_push() == (0, 1, 0)
    assert store.web_push_status()["subscription_count"] == 0
    assert store.list_web_push_deliveries() == []
    FakeWebPushSender.error = None


def test_retryable_web_push_failure_spends_only_one_request_per_dispatch(
    worker_context,
    monkeypatch,
) -> None:
    _settings, _base, store, worker = worker_context
    target = store.ensure_default_target(auto_track_new=False)
    store.save_web_push_subscription(**web_push_subscription())
    first = screening(sequence="1", free_seats=1)
    second = screening(sequence="2", free_seats=1)
    store.apply_snapshot(target["id"], target["version"], [first, second])
    for item in store.list_screenings(target["id"]):
        store.set_watch(item["id"], True)
    store.apply_snapshot(
        target["id"],
        target["version"],
        [replace(first, free_seats=2), replace(second, free_seats=2)],
    )
    assert len(store.pending_web_push_deliveries()) == 2

    FakeWebPushSender.sent.clear()
    FakeWebPushSender.attempted.clear()
    FakeWebPushSender.error = WebPushError("timeout", retryable=True)
    monkeypatch.setattr(
        "moviemax.console_worker.WebPushClient",
        FakeWebPushSender,
    )

    assert worker.deliver_pending_web_push() == (0, 1, 0)
    assert len(FakeWebPushSender.attempted) == 1
    deliveries = store.list_web_push_deliveries(status="pending")
    assert sorted(item["attempts"] for item in deliveries) == [0, 1]
    assert store.web_push_status()["subscription_count"] == 1
    FakeWebPushSender.error = None


def test_permanent_web_push_failure_disables_subscription_without_failing_worker(
    worker_context,
    monkeypatch,
) -> None:
    settings, _base, store, worker = worker_context
    target = store.ensure_default_target(auto_track_new=False)
    subscription = web_push_subscription()
    store.save_web_push_subscription(**subscription)
    first = screening(sequence="1", free_seats=1)
    second = screening(sequence="2", free_seats=1)
    store.apply_snapshot(target["id"], target["version"], [first, second])
    for item in store.list_screenings(target["id"]):
        store.set_watch(item["id"], True)
    store.apply_snapshot(
        target["id"],
        target["version"],
        [replace(first, free_seats=2), replace(second, free_seats=2)],
    )

    FakeWebPushSender.sent.clear()
    FakeWebPushSender.attempted.clear()
    FakeWebPushSender.error = WebPushError(
        "forbidden",
        retryable=False,
        status_code=403,
    )
    monkeypatch.setattr(
        "moviemax.console_worker.WebPushClient",
        FakeWebPushSender,
    )

    assert worker.deliver_pending_web_push() == (0, 1, 2)
    assert len(FakeWebPushSender.attempted) == 1
    assert store.web_push_status()["subscription_count"] == 0
    assert store.pending_web_push_deliveries() == []
    assert len(store.list_web_push_deliveries(status="dead")) == 2

    # Automatic browser-state synchronization must not reactivate the same
    # endpoint. The user has to remove it and create a fresh subscription.
    store.save_web_push_subscription(**subscription)
    assert store.web_push_status()["subscription_count"] == 0
    store.apply_snapshot(
        target["id"],
        target["version"],
        [replace(first, free_seats=3), replace(second, free_seats=3)],
    )
    assert len(store.list_web_push_deliveries()) == 2

    heartbeat = worker.heartbeat()
    assert heartbeat["status"] == "ok"
    assert heartbeat["dead_letters"] == 0
    assert heartbeat["web_push_dead_letters"] == 1
    assert console_worker_health(settings)["heartbeat"] == heartbeat
    FakeWebPushSender.error = None


def test_target_delete_waits_for_in_flight_web_push_dispatch(
    worker_context,
    monkeypatch,
) -> None:
    _settings, _base, store, worker = worker_context
    target = store.ensure_default_target(auto_track_new=False)
    store.save_web_push_subscription(**web_push_subscription())
    original = screening(free_seats=1)
    store.apply_snapshot(target["id"], target["version"], [original])
    screening_id = store.list_screenings(target["id"])[0]["id"]
    store.set_watch(screening_id, True)
    store.apply_snapshot(
        target["id"],
        target["version"],
        [replace(original, free_seats=2)],
    )

    send_started = threading.Event()
    release_send = threading.Event()
    send_completed = threading.Event()
    delete_completed = threading.Event()
    thread_errors: list[BaseException] = []

    class BlockingWebPushSender:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def send(self, _subscription: dict[str, Any], _payload: dict[str, Any]) -> None:
            send_started.set()
            if not release_send.wait(2):
                raise RuntimeError("test did not release Web Push sender")
            send_completed.set()

    monkeypatch.setattr(
        "moviemax.console_worker.WebPushClient",
        BlockingWebPushSender,
    )

    def dispatch() -> None:
        try:
            worker.deliver_pending_web_push()
        except BaseException as exc:  # noqa: BLE001 - surfaced in the test thread
            thread_errors.append(exc)

    def delete() -> None:
        try:
            store.delete_target(target["id"], expected_version=target["version"])
            delete_completed.set()
        except BaseException as exc:  # noqa: BLE001 - surfaced in the test thread
            thread_errors.append(exc)

    dispatch_thread = threading.Thread(target=dispatch)
    dispatch_thread.start()
    assert send_started.wait(2)
    delete_thread = threading.Thread(target=delete)
    delete_thread.start()

    assert not delete_completed.wait(0.1)
    release_send.set()
    dispatch_thread.join(2)
    delete_thread.join(2)

    assert thread_errors == []
    assert send_completed.is_set()
    assert delete_completed.is_set()
    assert store.get_target(target["id"]) is None
    assert store.list_web_push_deliveries() == []


def test_target_delete_waits_for_in_flight_telegram_dispatch(
    worker_context,
    monkeypatch,
) -> None:
    _settings, _base, store, worker = worker_context
    target = store.ensure_default_target(auto_track_new=False)
    original = screening(free_seats=1)
    store.apply_snapshot(target["id"], target["version"], [original])
    screening_id = store.list_screenings(target["id"])[0]["id"]
    store.set_watch(screening_id, True)
    store.apply_snapshot(
        target["id"],
        target["version"],
        [replace(original, free_seats=2)],
    )
    store.save_telegram_config(
        bot_token="1234567890:worker-secret-token",
        chat_id="-100123",
    )

    send_started = threading.Event()
    release_send = threading.Event()
    send_completed = threading.Event()
    delete_completed = threading.Event()
    thread_errors: list[BaseException] = []

    class BlockingTelegramSender:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def send_message(self, _message: str) -> None:
            send_started.set()
            if not release_send.wait(2):
                raise RuntimeError("test did not release Telegram sender")
            send_completed.set()

    monkeypatch.setattr(
        "moviemax.console_worker.TelegramClient",
        BlockingTelegramSender,
    )

    def dispatch() -> None:
        try:
            worker.deliver_pending()
        except BaseException as exc:  # noqa: BLE001 - surfaced in the test thread
            thread_errors.append(exc)

    def delete() -> None:
        try:
            store.delete_target(target["id"], expected_version=target["version"])
            delete_completed.set()
        except BaseException as exc:  # noqa: BLE001 - surfaced in the test thread
            thread_errors.append(exc)

    dispatch_thread = threading.Thread(target=dispatch)
    dispatch_thread.start()
    assert send_started.wait(2)
    delete_thread = threading.Thread(target=delete)
    delete_thread.start()

    assert not delete_completed.wait(0.1)
    release_send.set()
    dispatch_thread.join(2)
    delete_thread.join(2)

    assert thread_errors == []
    assert send_completed.is_set()
    assert delete_completed.is_set()
    assert store.get_target(target["id"]) is None
    assert store.list_outbox(target_id=target["id"]) == []


def test_prefetched_web_push_is_not_sent_after_target_delete_commits(
    worker_context,
    monkeypatch,
) -> None:
    _settings, _base, store, worker = worker_context
    target = store.ensure_default_target(auto_track_new=False)
    store.save_web_push_subscription(**web_push_subscription())
    original = screening(free_seats=1)
    store.apply_snapshot(target["id"], target["version"], [original])
    screening_id = store.list_screenings(target["id"])[0]["id"]
    store.set_watch(screening_id, True)
    store.apply_snapshot(
        target["id"],
        target["version"],
        [replace(original, free_seats=2)],
    )

    prefetched = threading.Event()
    release_prefetch = threading.Event()
    thread_errors: list[BaseException] = []
    original_pending = store.pending_web_push_deliveries

    def paused_pending(*args: Any, **kwargs: Any) -> list:
        deliveries = original_pending(*args, **kwargs)
        prefetched.set()
        if not release_prefetch.wait(2):
            raise RuntimeError("test did not release prefetched delivery")
        return deliveries

    monkeypatch.setattr(store, "pending_web_push_deliveries", paused_pending)
    FakeWebPushSender.sent.clear()
    FakeWebPushSender.attempted.clear()
    FakeWebPushSender.error = None
    monkeypatch.setattr(
        "moviemax.console_worker.WebPushClient",
        FakeWebPushSender,
    )

    def dispatch() -> None:
        try:
            worker.deliver_pending_web_push()
        except BaseException as exc:  # noqa: BLE001 - surfaced in the test thread
            thread_errors.append(exc)

    dispatch_thread = threading.Thread(target=dispatch)
    dispatch_thread.start()
    assert prefetched.wait(2)
    store.delete_target(target["id"], expected_version=target["version"])
    release_prefetch.set()
    dispatch_thread.join(2)

    assert thread_errors == []
    assert FakeWebPushSender.attempted == []
    assert store.get_target(target["id"]) is None


def test_target_deleted_during_poll_is_a_normal_worker_cancellation(
    worker_context,
    monkeypatch,
) -> None:
    _settings, _base, store, worker = worker_context
    target = store.ensure_default_target()

    def delete_during_fetch(_target: dict[str, Any]) -> list:
        store.delete_target(target["id"], expected_version=target["version"])
        return [screening()]

    monkeypatch.setattr(worker, "fetch_target", delete_during_fetch)

    assert worker.process_target(target) is None
    assert store.get_target(target["id"]) is None
    assert store.due_targets() == []
