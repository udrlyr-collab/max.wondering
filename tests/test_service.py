from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from moviemax.cgv import CgvError
from moviemax.config import Settings
from moviemax.service import MonitorService, healthcheck
from moviemax.state import StateStore
from moviemax.telegram import TelegramError
from tests.test_state import screening


class FakeCgv:
    def __init__(self, snapshots: list[list]) -> None:
        self.snapshots = snapshots
        self.index = 0

    def resolve_movie_no(self) -> str:
        return "30001323"

    def get_screening_dates(self, _movie_no: str) -> list[str]:
        return ["20260810"]

    def get_screenings(self, _movie_no: str, _date: str) -> list:
        snapshot = self.snapshots[min(self.index, len(self.snapshots) - 1)]
        self.index += 1
        return snapshot


class FakeTelegram:
    def __init__(
        self,
        *,
        fail_on_call: int | None = None,
        error: Exception | None = None,
    ) -> None:
        self.fail_on_call = fail_on_call
        self.error = error or RuntimeError("temporary failure")
        self.calls = 0
        self.messages: list[str] = []

    def send_message(self, message: str) -> None:
        self.calls += 1
        if self.fail_on_call == self.calls:
            raise self.error
        self.messages.append(message)


def settings(tmp_path) -> Settings:
    return Settings(
        state_db_path=tmp_path / "state.sqlite3",
        heartbeat_path=tmp_path / "heartbeat.json",
        request_gap_seconds=0,
    )


def test_service_baselines_then_sends_new_and_increased_seat_alerts(tmp_path) -> None:
    configured = settings(tmp_path)
    telegram = FakeTelegram()
    cgv = FakeCgv(
        [
            [screening()],
            [screening(), screening(sequence="2", free_seats=10)],
            [
                replace(screening(), free_seats=2),
                screening(sequence="2", free_seats=10),
            ],
        ]
    )
    service = MonitorService(
        configured,
        cgv=cgv,
        store=StateStore(configured.state_db_path),
        telegram=telegram,
    )

    assert service.poll_once().notifications_sent == 0
    assert service.poll_once().notifications_sent == 1
    assert "새 예매 회차" in telegram.messages[-1]
    assert service.poll_once().notifications_sent == 1
    assert "잔여석 증가" in telegram.messages[-1]
    assert configured.heartbeat_path.is_file()


def test_dead_letter_can_be_requeued_and_resumes_after_delivered_parts(
    tmp_path,
    monkeypatch,
) -> None:
    configured = replace(settings(tmp_path), telegram_max_attempts=1)
    store = StateStore(configured.state_db_path)
    store.apply_snapshot(
        [screening(free_seats=8)],
        notify_on_initial_state=True,
    )
    monkeypatch.setattr(
        "moviemax.service.render_event_messages",
        lambda _event: ["part-1", "part-2", "part-3"],
    )
    failed_telegram = FakeTelegram(
        fail_on_call=2,
        error=TelegramError("forbidden", retryable=False),
    )
    failing = MonitorService(
        configured,
        store=store,
        telegram=failed_telegram,
    )

    assert failing.deliver_pending() == (0, 1, 1)
    assert failed_telegram.messages == ["part-1"]
    assert store.pending_events() == []
    assert store.outbox_health() == {"pending": 0, "max_attempts": 0, "dead": 1}

    assert store.requeue_dead() == 1
    requeued = store.pending_events()
    assert len(requeued) == 1
    assert requeued[0].attempts == 0
    assert requeued[0].delivered_parts == 1

    recovered_telegram = FakeTelegram()
    recovered = MonitorService(
        configured,
        store=StateStore(configured.state_db_path),
        telegram=recovered_telegram,
    )
    assert recovered.deliver_pending() == (1, 0, 0)
    assert recovered_telegram.messages == ["part-2", "part-3"]
    assert store.pending_events() == []
    assert store.outbox_health() == {"pending": 0, "max_attempts": 0, "dead": 0}


def test_healthcheck_missing_database_does_not_create_it(tmp_path) -> None:
    configured = settings(tmp_path)
    assert not configured.state_db_path.exists()

    with pytest.raises(RuntimeError, match="State database is missing"):
        healthcheck(configured)

    assert not configured.state_db_path.exists()
    assert not configured.state_db_path.with_suffix(".sqlite3-wal").exists()
    assert not configured.state_db_path.with_suffix(".sqlite3-shm").exists()


def test_pending_notification_is_delivered_even_when_cgv_fails(tmp_path) -> None:
    class FailingCgv:
        def resolve_movie_no(self) -> str:
            raise CgvError("CGV unavailable")

    configured = settings(tmp_path)
    store = StateStore(configured.state_db_path)
    store.apply_snapshot([screening(free_seats=5)], notify_on_initial_state=True)
    telegram = FakeTelegram()
    service = MonitorService(
        configured,
        cgv=FailingCgv(),
        store=store,
        telegram=telegram,
    )

    with pytest.raises(CgvError, match="unavailable"):
        service.poll_once()

    assert len(telegram.messages) == 1
    assert store.pending_events() == []


def test_healthcheck_rejects_latest_error_heartbeat(tmp_path) -> None:
    configured = settings(tmp_path)
    StateStore(configured.state_db_path).apply_snapshot(
        [screening()],
        notify_on_initial_state=False,
    )
    configured.heartbeat_path.write_text(
        json.dumps(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "status": "error",
                "details": {"error": "CGV unavailable"},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="latest monitor poll failed"):
        healthcheck(configured)
